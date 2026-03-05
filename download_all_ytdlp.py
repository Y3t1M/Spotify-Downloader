#!/usr/bin/env python3
"""
Spotify → YouTube Music Downloader
────────────────────────────────────
No Spotify API key needed.
• Scrapes public Spotify track pages for title + artist
• Searches YouTube for the best match via yt-dlp
• Downloads as MP3 with embedded metadata and album art
• Builds a .m3u8 playlist per playlist when done
• Safe to restart — .done_ markers skip already-completed tracks
"""

import os
import re
import sys
import time
import subprocess
import urllib.request
import threading
import html as html_module
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
PLAYLISTS_TXT = os.path.join(SCRIPT_DIR, "playlists", "playlists.txt")
OUTPUT_ROOT   = os.path.expanduser("~/Music/SpotifyDownloads")

# ─── Worker count ─────────────────────────────────────────────────────────────
# Auto-detects CPU cores. Recommended = ~1.5× CPU count (sweet spot before
# YouTube starts 429-rate-limiting). Max capped at 20.
# Override on CLI: python3 download_all_ytdlp.py --workers 8

_cpu_count          = os.cpu_count() or 4
RECOMMENDED_WORKERS = min(max(_cpu_count + (_cpu_count // 2), 4), 20)
WORKERS             = RECOMMENDED_WORKERS

# ─── HTTP user-agent ──────────────────────────────────────────────────────────

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ─── Terminal colours (stdlib only) ───────────────────────────────────────────

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


# ─── UI helpers ───────────────────────────────────────────────────────────────

def progress_bar(done, total, ok, fail, skip, status, label, bar_width=28):
    pct    = done / total if total else 1.0
    filled = int(bar_width * pct)
    bar    = c(GREEN, "█" * filled) + c(DIM, "░" * (bar_width - filled))
    pct_s  = f"{pct * 100:5.1f}%"
    counts = f"{c(GREEN, '✓')}{ok} {c(RED, '✗')}{fail} {c(DIM, '⊘')}{skip}"
    trunc  = label[:54] + "…" if len(label) > 55 else label

    icons  = {"ok": c(GREEN, "✓"), "skip": c(DIM, "⊘"), "fail": c(RED, "✗"),
              "rate": c(YELLOW, "⏳"), "dl": c(CYAN, "↓")}
    icon   = icons.get(status, c(CYAN, "↓"))

    line = (
        f"  [{bar}] {c(BOLD, f'{done}/{total}')} {pct_s}  "
        f"{counts}  {icon} {c(DIM, trunc)}"
    )
    print(f"\r{line}\033[K", end="", flush=True)


def section_header(name, total):
    inner = f"  {c(BOLD + WHITE, name)}  {c(DIM, f'({total:,} tracks)')}"
    rule  = c(BOLD + CYAN, "─" * 62)
    print(f"\n{rule}")
    print(inner)
    print(rule)


def section_footer(ok, fail, total, m3u8_path):
    print(
        f"\n  {c(BOLD, 'Result')}  "
        f"{c(GREEN, f'✓ {ok:,} ok')}  "
        f"{c(RED, f'✗ {fail:,} failed')}  "
        f"{c(DIM, f'/ {total:,} total')}"
    )
    if m3u8_path:
        print(f"  {c(DIM, '→ playlist:')} {c(DIM, os.path.basename(m3u8_path))}")


# ─── Playlist loader ──────────────────────────────────────────────────────────

def load_playlists(path):
    """Parse playlists.txt → list of {name, urls}."""
    playlists, current_name, current_urls = [], None, []
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("https://open.spotify.com/track/"):
                if current_name:
                    current_urls.append(line)
            elif line.startswith(("https://open.spotify.com/local/", "spotify:local/")):
                pass  # skip local files
            else:
                if current_name and current_urls:
                    playlists.append({"name": current_name, "urls": current_urls})
                current_name  = line
                current_urls  = []
    if current_name and current_urls:
        playlists.append({"name": current_name, "urls": current_urls})
    return playlists


# ─── Spotify scraper ──────────────────────────────────────────────────────────

def scrape_track_info(spotify_url):
    """Return (title, artist) from a public Spotify track page, or (None, None)."""
    try:
        req  = urllib.request.Request(spotify_url, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        t_m  = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        d_m  = re.search(r'<meta property="og:description" content="([^"]+)"', html)
        if not t_m:
            return None, None
        title  = html_module.unescape(t_m.group(1))
        parts  = d_m.group(1).split(" · ") if d_m else []
        artist = html_module.unescape(parts[0]) if parts else ""
        return title, artist
    except Exception:
        return None, None


# ─── YouTube search ───────────────────────────────────────────────────────────

def search_youtube(title, artist):
    """Search YouTube and return the best match URL, or None."""
    query = re.sub(r'\s+', ' ', f"{artist} {title}".strip())
    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-items", "1",
             "--print", "%(id)s", "--no-warnings", f"ytsearch1:{query}"],
            capture_output=True, text=True, timeout=15
        )
        vid = r.stdout.strip().split("\n")[0].strip()
        if vid and len(vid) == 11:
            return f"https://music.youtube.com/watch?v={vid}"
    except Exception:
        pass
    return None


# ─── Downloader ───────────────────────────────────────────────────────────────

def download_track(yt_url, output_dir, retries=2):
    """Download a YouTube URL as MP3 with metadata. Returns truthy on success."""
    for attempt in range(retries + 1):
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
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

        if re.search(r'\[ExtractAudio\] Destination: .+\.mp3', r.stdout):
            return "downloaded"
        if re.search(r'\[download\] .+\.mp3 has already been downloaded', r.stdout):
            return "exists"
        if r.returncode != 0:
            if "429" in r.stderr or "Too Many Requests" in r.stderr or "rate" in r.stderr.lower():
                if attempt < retries:
                    time.sleep(15 * (attempt + 1))
                    continue
            return None
        return "downloaded"
    return None


# ─── m3u8 builder ─────────────────────────────────────────────────────────────

def build_m3u8(output_dir, name):
    """
    Build an m3u8 with paths relative to OUTPUT_ROOT so Navidrome can resolve them.
    e.g.  Country/Alabama - Dixieland Delight.mp3
    """
    mp3s        = sorted(f for f in os.listdir(output_dir) if f.endswith(".mp3"))
    folder_name = os.path.basename(output_dir)
    path        = os.path.join(output_dir, f"{name}.m3u8")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for mp3 in mp3s:
            rel = f"{folder_name}/{mp3}"
            f.write(f"#EXTINF:-1,{os.path.splitext(mp3)[0]}\n{rel}\n")
    return path


# ─── Playlist processor ───────────────────────────────────────────────────────

def process_playlist(playlist, workers):
    name       = playlist["name"]
    track_urls = playlist["urls"]
    output_dir = os.path.join(OUTPUT_ROOT, name)
    os.makedirs(output_dir, exist_ok=True)
    log_path   = os.path.join(output_dir, "download_log.txt")

    total    = len(track_urls)
    counters = {"ok": 0, "fail": 0, "skip": 0, "done": 0}
    lock     = threading.Lock()

    section_header(name, total)

    def emit(status, label):
        progress_bar(
            counters["done"], total,
            counters["ok"], counters["fail"], counters["skip"],
            status, label
        )

    def process_one(spotify_url):
        track_id    = spotify_url.rstrip("/").split("/")[-1].split("?")[0]
        done_marker = os.path.join(output_dir, f".done_{track_id}")

        # ── Already completed ──
        if os.path.exists(done_marker):
            with lock:
                counters["ok"]   += 1
                counters["skip"] += 1
                counters["done"] += 1
                emit("skip", "cached")
            return

        # ── Scrape Spotify ──
        title, artist = scrape_track_info(spotify_url)
        if not title:
            with lock:
                counters["fail"] += 1
                counters["done"] += 1
                emit("fail", track_id)
                print()  # persist fail line
                with open(log_path, "a") as lf:
                    lf.write(f"FAIL (scrape) {spotify_url}\n")
            return

        label = f"{artist} - {title}"

        # ── Check if MP3 already on disk ──
        safe = title.lower()[:20]
        with lock:
            on_disk = any(safe in f.lower() for f in os.listdir(output_dir) if f.endswith(".mp3"))
        if on_disk:
            with lock:
                open(done_marker, "w").close()
                counters["ok"]   += 1
                counters["skip"] += 1
                counters["done"] += 1
                emit("skip", label)
            return

        # ── Search YouTube ──
        yt_url = search_youtube(title, artist)
        if not yt_url:
            with lock:
                counters["fail"] += 1
                counters["done"] += 1
                emit("fail", label)
                print()
                with open(log_path, "a") as lf:
                    lf.write(f"FAIL (search) {artist} - {title} | {spotify_url}\n")
            return

        # ── Download ──
        result = download_track(yt_url, output_dir)
        with lock:
            if result:
                open(done_marker, "w").close()
                counters["ok"]   += 1
                counters["done"] += 1
                emit("ok", label)
                with open(log_path, "a") as lf:
                    lf.write(f"OK {artist} - {title}\n")
            else:
                counters["fail"] += 1
                counters["done"] += 1
                emit("fail", label)
                print()
                with open(log_path, "a") as lf:
                    lf.write(f"FAIL (download) {artist} - {title} | {yt_url}\n")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_one, url) for url in track_urls]
        for f in as_completed(futures):
            f.result()

    print()  # newline after final progress bar line
    m3u8 = build_m3u8(output_dir, name)
    section_footer(counters["ok"], counters["fail"], total, m3u8)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    workers = WORKERS
    args    = sys.argv[1:]

    # --workers N
    if "--workers" in args:
        idx = args.index("--workers")
        try:
            workers = int(args[idx + 1])
            args    = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            print("Usage: python3 download_all_ytdlp.py [--workers N] [\"playlist name\"]")
            sys.exit(1)

    playlists   = load_playlists(PLAYLISTS_TXT)
    total_tracks = sum(len(p["urls"]) for p in playlists)

    print(f"\n{c(BOLD + WHITE, '  Spotify → YouTube Downloader')}")
    print(f"  {c(DIM, f'Output    →  {OUTPUT_ROOT}')}")
    print(f"  {c(DIM, f'Playlists →  {len(playlists)}   Tracks: {total_tracks:,}')}")
    cpu_note = f"(CPU: {_cpu_count}  recommended: {RECOMMENDED_WORKERS})"
    print(
        f"  {c(DIM, f'Workers   →  {workers}')}  "
        f"{c(DIM, cpu_note)}"
    )

    if args:
        target    = args[0].lower()
        playlists = [p for p in playlists if p["name"].lower() == target]
        if not playlists:
            all_pl = load_playlists(PLAYLISTS_TXT)
            print(f"\n  {c(RED, 'Unknown playlist:')} {args[0]}")
            print(f"  Available: {', '.join(p['name'] for p in all_pl)}")
            sys.exit(1)

    for playlist in playlists:
        process_playlist(playlist, workers)

    print(f"\n{c(BOLD + GREEN, '  ✓ All playlists complete!')}\n")


if __name__ == "__main__":
    main()
