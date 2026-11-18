#!/usr/bin/env python3
"""Auto clip cutter: finds funny moments from YouTube chat, cuts wide + vertical clips.

Modes:
  default : stream already ended / replay (chat has video timestamps)
  --live  : stream is running NOW. Records for --minutes while watching chat,
            then cuts clips from the recording.
"""
import argparse, os, re, subprocess, sys, time
from collections import Counter
from pathlib import Path

from chat_downloader import ChatDownloader

FUNNY = r"lmao|lmfao|lol+|haha+|hehe+|kekw|omegalul|lul|bruh|dead|dying|💀|😂|🤣|😭"


def normalize_url(u):
    """Turn /live/ID, /shorts/ID, youtu.be/ID links into watch?v=ID."""
    m = re.search(r"(?:youtube\.com/(?:live|shorts|embed)/|youtu\.be/)([\w-]{11})", u)
    if m:
        return "https://www.youtube.com/watch?v=" + m.group(1)
    return u


def make_dl():
    if os.path.exists("cookies.txt"):
        return ChatDownloader(cookies="cookies.txt")
    return ChatDownloader()


def is_funny(rx, m):
    return bool(rx.search(m.get("message", "")))


def read_chat_replay(url, rx, timeout):
    funny, allm = [], []
    chat = make_dl().get_chat(url, message_types=["text_message"], timeout=timeout)
    for m in chat:
        t = m.get("time_in_seconds")
        if t is None or t < 0:
            continue
        allm.append(t)
        if is_funny(rx, m):
            funny.append(t)
    return funny, allm


def find_peaks(funny, allm, bucket, top_n, gap, min_score):
    score = Counter()
    for t in funny:
        score[int(t // bucket)] += 3
    for t in allm:
        score[int(t // bucket)] += 1
    smooth = {b: score[b - 1] + score[b] + score[b + 1] for b in list(score)}
    picked = []
    for b, s in sorted(smooth.items(), key=lambda x: -x[1]):
        if s < min_score:
            break
        center = b * bucket + bucket / 2
        if all(abs(center - p) >= gap for p, _ in picked):
            picked.append((center, s))
        if len(picked) >= top_n:
            break
    return sorted(picked)


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=False).returncode


def hms(s):
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def encode(src, ss, dur, wide, vert):
    base = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(ss), "-i", str(src), "-t", str(dur)]
    run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(wide)])
    fc = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,boxblur=20:5[bg];"
          "[0:v]scale=1080:-2[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2")
    run(base + ["-filter_complex", fc, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(vert)])


def audio_peaks(src, out, bucket, top_n, gap, min_db):
    """Fallback: find loud moments (laughter/shouting) from audio."""
    import wave
    import numpy as np
    wav = out / "audio.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    if not wav.exists():
        return []
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    wav.unlink(missing_ok=True)
    secs = len(data) // sr
    if secs < bucket * 3:
        return []
    rms = np.sqrt((data[: secs * sr].reshape(secs, sr) ** 2).mean(axis=1)) + 1e-6
    db = 20 * np.log10(rms)
    base = np.median(db)
    nb = secs // bucket
    bdb = db[: nb * bucket].reshape(nb, bucket).mean(axis=1) - base
    smooth = bdb.copy()
    for i in range(nb):
        smooth[i] = bdb[max(0, i - 1): i + 2].mean()
    picked = []
    for b in np.argsort(-smooth):
        if smooth[b] < min_db:
            break
        center = float(b * bucket + bucket / 2)
        if all(abs(center - p) >= gap for p, _ in picked):
            picked.append((center, round(float(smooth[b]), 1)))
        if len(picked) >= top_n:
            break
    return sorted(picked)


def live_collect(a, rx, cookies, out):
    """Record the running stream and collect chat times (seconds since recording start)."""
    rec = out / "live.ts"
    cmd = ["yt-dlp", *cookies, "--js-runtimes", "node",
           "-f", "b[height<=720]/bv*[height<=720]+ba/b",
           "--hls-use-mpegts", "--no-part", "-o", str(rec), a.url]
    print("+", " ".join(cmd), flush=True)
    t0 = time.time()
    proc = subprocess.Popen(cmd)
    funny, allm = [], []
    try:
        chat = make_dl().get_chat(
            a.url, message_types=["text_message"], timeout=a.minutes * 60)
        for m in chat:
            ts = m.get("timestamp")  # microseconds since epoch
            t = (ts / 1e6 - t0) if ts else (time.time() - t0)
            if t < 0:
                continue
            allm.append(t)
            if is_funny(rx, m):
                funny.append(t)
    except Exception as e:
        print(f"Chat failed ({type(e).__name__}: {e}). Recording continues, will use AUDIO detection.")
        end = t0 + a.minutes * 60
        while time.time() < end and proc.poll() is None:
            time.sleep(5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    return rec, funny, allm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--live", action="store_true", help="stream is running now")
    ap.add_argument("--minutes", type=int, default=30, help="live: how long to record")
    ap.add_argument("--top", type=int, default=5, help="max clips")
    ap.add_argument("--before", type=int, default=10, help="sec before peak")
    ap.add_argument("--after", type=int, default=20, help="sec after peak")
    ap.add_argument("--bucket", type=int, default=5, help="bucket size in sec")
    ap.add_argument("--min-score", type=int, default=8)
    ap.add_argument("--min-db", type=float, default=6.0, help="audio fallback sensitivity")
    ap.add_argument("--keywords", default="", help="extra words, comma separated")
    ap.add_argument("--chat-timeout", type=int, default=None)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    a.url = normalize_url(a.url)
    print("URL:", a.url)

    out = Path(a.out)
    out.mkdir(exist_ok=True)

    pattern = FUNNY
    extra = [re.escape(k.strip()) for k in a.keywords.split(",") if k.strip()]
    if extra:
        pattern += "|" + "|".join(extra)
    rx = re.compile(pattern, re.IGNORECASE)
    cookies = ["--cookies", "cookies.txt"] if os.path.exists("cookies.txt") else []

    rec = None
    funny, allm = [], []
    if a.live:
        print(f"LIVE mode: recording + watching chat for {a.minutes} min...")
        rec, funny, allm = live_collect(a, rx, cookies, out)
        if not rec.exists() or rec.stat().st_size < 100_000:
            sys.exit("Recording failed: YouTube blocked the download or the stream is not live. "
                     "Add the YT_COOKIES secret and try again.")
    else:
        print("Reading chat replay...")
        try:
            funny, allm = read_chat_replay(a.url, rx, a.chat_timeout)
        except Exception as e:
            print(f"Chat failed ({type(e).__name__}: {e})")

    print(f"{len(allm)} chat messages, {len(funny)} funny")
    if allm:
        peaks = find_peaks(funny, allm, a.bucket, a.top, a.before + a.after, a.min_score)
    else:
        print("No chat data -> using AUDIO loudness detection")
        src, tmp = rec, None
        if src is None:
            tmp = src = out / "full_audio.m4a"
            rc = run(["yt-dlp", *cookies, "--js-runtimes", "node", "-f", "ba",
                      "-o", str(tmp), a.url])
            if rc != 0 or not tmp.exists():
                sys.exit("Could not download audio (YouTube block?). Add the YT_COOKIES secret.")
        peaks = audio_peaks(src, out, a.bucket, a.top, a.before + a.after, a.min_db)
        if tmp:
            tmp.unlink(missing_ok=True)
    if not peaks:
        sys.exit("No peaks found. Try lowering --min-score (chat) or --min-db (audio).")

    notes = []
    for i, (center, score) in enumerate(peaks, 1):
        start = max(0, center - a.before)
        end = center + a.after
        dur = end - start
        wide = out / f"clip_{i:02d}_wide.mp4"
        vert = out / f"clip_{i:02d}_vertical.mp4"

        if rec:
            encode(rec, start, dur, wide, vert)
        else:
            raw = out / f"raw_{i:02d}.mp4"
            rc = run(["yt-dlp", *cookies, "--js-runtimes", "node",
                      "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                      "--merge-output-format", "mp4",
                      "--download-sections", f"*{start}-{end}",
                      "--force-keyframes-at-cuts",
                      "-o", str(raw), a.url])
            if rc != 0 or not raw.exists():
                print(f"Clip {i}: download failed, skipping")
                continue
            encode(raw, 0, dur, wide, vert)
            raw.unlink(missing_ok=True)
        notes.append(f"clip_{i:02d}: {hms(start)} - {hms(end)} (score {score})")

    if rec:
        rec.unlink(missing_ok=True)  # don't publish the full recording
    (out / "clips.txt").write_text("\n".join(notes) + "\n")
    print("\n".join(notes))


if __name__ == "__main__":
    main()
