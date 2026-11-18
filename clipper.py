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
    ap.add_argument("--keywords", default="", help="extra words, comma separated")
    ap.add_argument("--chat-timeout", type=int, default=None)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(exist_ok=True)

    pattern = FUNNY
    extra = [re.escape(k.strip()) for k in a.keywords.split(",") if k.strip()]
    if extra:
        pattern += "|" + "|".join(extra)
    rx = re.compile(pattern, re.IGNORECASE)
    cookies = ["--cookies", "cookies.txt"] if os.path.exists("cookies.txt") else []

    rec = None
    if a.live:
        print(f"LIVE mode: recording + watching chat for {a.minutes} min...")
        rec, funny, allm = live_collect(a, rx, cookies, out)
        if not rec.exists():
            sys.exit("Recording failed (stream not live, or YouTube blocked the download).")
    else:
        print("Reading chat replay...")
        funny, allm = read_chat_replay(a.url, rx, a.chat_timeout)

    print(f"{len(allm)} messages, {len(funny)} funny")
    if not allm:
        sys.exit("No chat found (chat disabled, or stream not a replay yet).")

    peaks = find_peaks(funny, allm, a.bucket, a.top, a.before + a.after, a.min_score)
    if not peaks:
        sys.exit("No peaks found. Try lowering --min-score.")

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
