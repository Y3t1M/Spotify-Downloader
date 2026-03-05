#!/usr/bin/env python3
"""
Retry failed downloads from download_all_ytdlp.py
───────────────────────────────────────────────────
Run this after the main script finishes.
• Reads all download_log.txt files for FAIL entries
• Retries with progressively simplified search queries
• Tries up to 3 alternate YouTube results per track
• Writes unfindable tracks to unfindable.txt
• Safe to re-run — skips anything already downloaded
"""

import os
import re
import time
import subprocess
import html as html_module
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Paths ────────────────────────────────────────────────────────────────────

OUTPUT_ROOT    = os.path.expanduser("~/Music/SpotifyDownloads")
UNFINDABLE_LOG = os.path.join(OUTPUT_ROOT, "unfindable.txt")

# ─── Worker count ─────────────────────────────────────────────────────────────
# Lower than the main script — retry queries are more expensive (tries 3 results)

_cpu_count          = os.cpu_count() or 4
RECOMMENDED_WORKERS = min(max(_cpu_count, 4), 12)
WORKERS             = RECOMMENDED_WORKERS

# ─── Colours ──────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"

def c(color, text):
    return f"{color}{text}{RESET}"


# ─── Progress bar ─────────────────────────────────────────────────────────────

def progress_bar(done, total, ok, fail, skip, status, label, bar_width=28):
    pct    = done / total if total else 1.0
    filled = int(bar_width * pct)
    bar    = c(GREEN, "█" * filled) + c(DIM, "░" * (bar_width - filled))
    pct_s  = f"{pct * 100:5.1f}%"
    counts = f"{c(GREEN, '✓')}{ok} {c(RED, '✗')}{fail} {c(DIM, '⊘')}{skip}"
    trunc  = label[:54] + "…" if len(label) > 55 else label
    icons  = {"ok": c(GREEN, "✓"), "skip": c(DIM, "⊘"), "fail": c(RED, "✗"),
              "rate": c(YELLOW, "⏳")}
    icon   = icons.get(status, c(CYAN, "↺"))
    line   = (
        f"  [{bar}] {c(BOLD, f'{done}/{total}')} {pct_s}  "
        f"{counts}  {icon} {c(DIM, trunc)}"
    )
    print(f"\r{line}\033[K", end="", flush=True)


# ─── Query cleaner ────────────────────────────────────────────────────────────

def clean_query(text):
    """Strip feat/remaster/live noise for a cleaner YouTube search."""
    text = html_module.unescape(text)
    text = re.sub(r'\s*[\(\[]?(feat\.?|ft\.?|with)\s[^\)\]]*[\)\]]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[-–]\s*(remastered?|live|single version|radio edit|original mix).*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\(\[\{][^\)\]\}]*[\)\]\}]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# ─── YouTube search (multi-result) ────────────────────────────────────────────

def search_youtube_multi(query, n=3):
    """Return up to n YouTube video URLs for a query."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", f"--playlist-items", f"1-{n}",
             "--print", "%(id)s", "--no-warnings", f"ytsearch{n}:{query}"],
            capture_output=True, text=True, timeout=20
        )
        ids = [v.strip() for v in r.stdout.strip().split("\n") if v.strip() and len(v.strip()) == 11]
        return [f"https://music.youtube.com/watch?v={vid}" for vid in ids]
    except Exception:
        return []


# ─── Downloader ───────────────────────────────────────────────────────────────

def download_track(yt_url, output_dir):
    """Download a single track as MP3. Returns True on success."""
    try:
        r = subprocess.run(
            [
                "yt-dlp", "-x",
                "--audio-format", "mp3", "--audio-quality", "0",
                "--embed-thumbnail", "--add-metadata",
                "--output", os.path.join(output_dir, "%(artist)s - %(title)s.%(ext)s"),
                "--no-warnings", "--restrict-filenames",
                yt_url,
            ],
            capture_output=True, text=True, timeout=180
        )
        return r.returncode == 0
    except Exception:
        return False


# ─── Failure collector ────────────────────────────────────────────────────────

def collect_failures():
    """Scan all playlist download_log.txt files for FAIL lines."""
    failures = []
    for playlist_dir in sorted(os.listdir(OUTPUT_ROOT)):
        full_dir = os.path.join(OUTPUT_ROOT, playlist_dir)
        log_path = os.path.join(full_dir, "download_log.txt")
        if not os.path.isfile(log_path):
            continue
        with open(log_path, "r") as f:
            for line in f:
                line = line.strip()

                # FAIL (search) Artist - Title | spotify_url
                m = re.match(r'FAIL \(search\) (.+) \| (https://open\.spotify\.com/track/\S+)', line)
                if m:
                    label, url = m.group(1), m.group(2)
                    parts = label.split(" - ", 1)
                    failures.append({
                        "playlist": playlist_dir, "output_dir": full_dir,
                        "reason": "search",
                        "artist": parts[0].strip() if len(parts) == 2 else "",
                        "title":  parts[1].strip() if len(parts) == 2 else label,
                        "ref": url,
                    })
                    continue

                # FAIL (download) Artist - Title | yt_url
                m = re.match(r'FAIL \(download\) (.+) \| (https?://\S+)', line)
                if m:
                    label, url = m.group(1), m.group(2)
                    parts = label.split(" - ", 1)
                    failures.append({
                        "playlist": playlist_dir, "output_dir": full_dir,
                        "reason": "download",
                        "artist": parts[0].strip() if len(parts) == 2 else "",
                        "title":  parts[1].strip() if len(parts) == 2 else label,
                        "ref": url,
                    })

    # Deduplicate
    seen, unique = set(), []
    for f in failures:
        key = (f["playlist"], f["artist"].lower(), f["title"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


# ─── Retry worker ─────────────────────────────────────────────────────────────

def retry_one(item, lock, counters, total):
    artist     = html_module.unescape(item["artist"])
    title      = html_module.unescape(item["title"])
    output_dir = item["output_dir"]
    playlist   = item["playlist"]
    log_path   = os.path.join(output_dir, "download_log.txt")

    def emit(status, label):
        progress_bar(counters["done"], total, counters["ok"],
                     counters["fail"], counters["skip"], status, label)

    # Already on disk?
    safe = title.lower()[:20]
    with lock:
        on_disk = any(safe in f.lower() for f in os.listdir(output_dir) if f.endswith(".mp3"))
    if on_disk:
        with lock:
            counters["skip"] += 1
            counters["done"] += 1
            emit("skip", f"{artist} - {title}")
        return

    # Build query variants: original → cleaned → title-only
    queries = [
        f"{artist} {title}",
        f"{clean_query(artist)} {clean_query(title)}",
        clean_query(title),
    ]

    for query in queries:
        urls = search_youtube_multi(query, n=3)
        for yt_url in urls:
            if download_track(yt_url, output_dir):
                with lock:
                    counters["ok"]   += 1
                    counters["done"] += 1
                    emit("ok", f"{artist} - {title}")
                with open(log_path, "a") as lf:
                    lf.write(f"OK (retry) {artist} - {title}\n")
                return
            time.sleep(0.5)

    # Permanently unfindable
    with lock:
        counters["fail"] += 1
        counters["done"] += 1
        emit("fail", f"{artist} - {title}")
        print()
        with open(UNFINDABLE_LOG, "a") as uf:
            uf.write(f"{playlist} | {artist} - {title}\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    workers = WORKERS
    import sys
    args = sys.argv[1:]
    if "--workers" in args:
        idx = args.index("--workers")
        try:
            workers = int(args[idx + 1])
        except (IndexError, ValueError):
            pass

    print(f"\n{c(BOLD + WHITE, '  Retry Failed Downloads')}")
    print(f"  {c(DIM, f'Scanning  →  {OUTPUT_ROOT}')}")

    failures = collect_failures()
    total    = len(failures)

    if total == 0:
        print(f"\n  {c(GREEN, '✓ Nothing to retry — no failures found!')}\n")
        return

    print(f"  {c(DIM, f'Failed tracks →  {total:,}')}")
    print(
        f"  {c(DIM, f'Workers   →  {workers}')}  "
        f"{c(DIM, f'(CPU: {_cpu_count}  recommended: {RECOMMENDED_WORKERS})')}"
    )

    # Clear unfindable log
    open(UNFINDABLE_LOG, "w").close()

    counters = {"ok": 0, "fail": 0, "skip": 0, "done": 0}
    lock     = threading.Lock()

    print()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(retry_one, item, lock, counters, total) for item in failures]
        for f in as_completed(futures):
            f.result()

    print(f"\n\n{c(BOLD, '  Retry complete')}")
    recovered = counters["ok"]
    on_disk   = counters["skip"]
    unfound   = counters["fail"]
    print(f"  {c(GREEN,  f'✓ Recovered:          {recovered:,}')}")
    print(f"  {c(DIM,    f'⊘ Already on disk:    {on_disk:,}')}")
    print(f"  {c(RED,    f'✗ Still unfindable:   {unfound:,}')}")

    if counters["fail"] > 0:
        print(f"\n  Unfindable list → {c(DIM, UNFINDABLE_LOG)}")
    print()


if __name__ == "__main__":
    main()
