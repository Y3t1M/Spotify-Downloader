#!/usr/bin/env python3
"""
fix_tags.py — Fix MP3 tags for Navidrome
──────────────────────────────────────────
For each MP3 in ~/Music/SpotifyDownloads:
  1. Detects files with bad/missing tags (no album, YouTube genre, NA- filename)
  2. Looks up correct metadata via MusicBrainz (free, no API key)
  3. Prefers releases by the actual artist (not compilations)
  4. Fetches cover art from Cover Art Archive
  5. Writes tags back using mutagen (title, artist, album, album_artist,
     track, year, genre) and embeds cover art

Safe to re-run — skips files that already have good tags.
Writes fix_tags_log.txt for anything it couldn't fix.
"""

import os
import re
import sys
import json
import time
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, APIC,
        ID3NoHeaderError
    )
except ImportError:
    print("Installing mutagen...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "mutagen", "-q"])
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, APIC,
        ID3NoHeaderError
    )

# ─── Config ───────────────────────────────────────────────────────────────────

OUTPUT_ROOT = os.path.expanduser("~/Music/SpotifyDownloads")
LOG_PATH    = os.path.join(OUTPUT_ROOT, "fix_tags_log.txt")
MB_UA       = "SpotifyDownloader/2.0 (navidrome-fix; contact@example.com)"
MB_DELAY    = 0.35   # MusicBrainz rate limit: max ~3 req/s
WORKERS     = 4      # Keep low — MusicBrainz is rate-limited

# ─── Colours ──────────────────────────────────────────────────────────────────

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"
CYAN  = "\033[36m"; WHITE = "\033[97m"

def c(col, t): return f"{col}{t}{RESET}"

# ─── Tag detection ────────────────────────────────────────────────────────────

BAD_GENRES = {"music", "film & animation", "entertainment", "people & blogs",
              "howto & style", "gaming", "comedy", "news & politics"}

def _clean_yt_title(title):
    """Strip YouTube noise from a title string."""
    # Strip "Artist - Title" prefix (YouTube video title format)
    if " - " in title:
        # If it looks like "Artist - Song (Official...)" take the middle part
        parts = title.split(" - ", 1)
        # Heuristic: if right side has YouTube junk, clean it
        title = parts[1] if len(parts[1]) > 3 else title
    # Strip trailing YouTube tags
    title = re.sub(
        r'\s*[\(\[](Official\s*(Music\s*)?Video|Official\s*Audio|Lyric\s*Video|'
        r'HD|HQ|Audio|4K|Live|Original Cast \d+.*?)[\)\]].*',
        '', title, flags=re.IGNORECASE
    ).strip()
    title = re.sub(
        r'\s*[-–]\s*(Official\s*(Music\s*)?Video|Official\s*Audio|Audio|'
        r'Hamilton\s+Original.*|Live\s+HD.*)$',
        '', title, flags=re.IGNORECASE
    ).strip()
    return title


def _looks_like_uploader(artist):
    """Heuristic: does this look like a YouTube channel name rather than an artist?"""
    if not artist or artist.lower() in ("na", "n/a", ""):
        return True
    # YouTube uploaders often have numbers, underscores, or odd patterns
    if re.search(r'\d{4,}|[_]{2}', artist):
        return True
    # All-lowercase with no spaces — likely username
    if artist == artist.lower() and " " not in artist and len(artist) > 4:
        return True
    return False


def needs_fix(path):
    """Return (title, artist) if the file needs tag repair, else None."""
    fname = os.path.basename(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return _name_from_filename(fname)
    except Exception:
        return None

    title   = str(tags.get("TIT2", "")).strip()
    artist  = str(tags.get("TPE1", "")).strip()
    album   = str(tags.get("TALB", "")).strip()
    genre   = str(tags.get("TCON", "")).strip().lower()
    has_art = bool(tags.getall("APIC"))

    bad_artist = _looks_like_uploader(artist)
    bad_album  = not album
    bad_genre  = genre in BAD_GENRES
    na_file    = re.match(r'^NA[-_\s]', fname, re.IGNORECASE) is not None

    if bad_artist or bad_album or bad_genre or na_file or not has_art:
        # Clean the title
        clean_title = _clean_yt_title(title)
        # If artist is bad, try to recover from filename or title prefix
        if bad_artist:
            fn_title, fn_artist = _name_from_filename(fname)
            if fn_artist and not _looks_like_uploader(fn_artist):
                artist = fn_artist
            elif " - " in title:
                artist = title.split(" - ", 1)[0].strip()
            else:
                artist = ""
        return clean_title or None, artist or None
    return None


def _name_from_filename(fname):
    """Extract (title, artist) from 'Artist - Title.mp3' filename."""
    base = os.path.splitext(fname)[0]
    base = re.sub(r'^NA[-_\s]+', '', base)
    base = base.replace("_", " ")
    if " - " in base:
        artist, title = base.split(" - ", 1)
        return title.strip(), artist.strip()
    return base.strip(), ""


def _artist_from_filename(fname):
    base = os.path.splitext(fname)[0].replace("_", " ")
    base = re.sub(r'^NA[-_\s]+', '', base)
    if " - " in base:
        return base.split(" - ", 1)[0].strip()
    return ""


# ─── MusicBrainz lookup ───────────────────────────────────────────────────────

_mb_lock = threading.Lock()
_last_mb = [0.0]

def _mb_get(url):
    """Rate-limited MusicBrainz GET."""
    with _mb_lock:
        wait = MB_DELAY - (time.time() - _last_mb[0])
        if wait > 0:
            time.sleep(wait)
        _last_mb[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=12).read())
    except Exception:
        return {}


def mb_lookup(artist, title):
    """
    Look up a recording on MusicBrainz.
    Returns dict with: title, artist, album, album_artist, track, date, genre
    Prefers the artist's own release over compilations.
    """
    clean_artist = re.sub(r'\s*[\(\[].*?[\)\]]', '', artist).strip()
    clean_title  = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip()

    query = urllib.parse.quote(f'recording:"{clean_title}" AND artist:"{clean_artist}"')
    data  = _mb_get(f"https://musicbrainz.org/ws/2/recording/?query={query}&fmt=json&limit=10&inc=releases+artist-credits+genres")

    recordings = data.get("recordings", [])
    if not recordings:
        # Looser fallback
        query2 = urllib.parse.quote(f'"{clean_title}" {clean_artist}')
        data2  = _mb_get(f"https://musicbrainz.org/ws/2/recording/?query={query2}&fmt=json&limit=5")
        recordings = data2.get("recordings", [])

    best_rec = best_rel = None

    for rec in recordings:
        releases = rec.get("releases", [])
        # Prefer: album by same artist > single > compilation
        for rel in releases:
            rtype = rel.get("release-group", {}).get("primary-type", "").lower()
            status = rel.get("status", "").lower()
            if status != "official":
                continue
            if rtype == "album":
                best_rec, best_rel = rec, rel
                break
        if best_rec:
            break
        # Settle for single/ep
        for rel in releases:
            rtype = rel.get("release-group", {}).get("primary-type", "").lower()
            if rtype in ("single", "ep") and rel.get("status", "").lower() == "official":
                best_rec, best_rel = rec, rel
                break
        if best_rec:
            break

    # Last resort: first recording, first release
    if not best_rec and recordings:
        best_rec = recordings[0]
        rels = best_rec.get("releases", [])
        best_rel = rels[0] if rels else {}

    if not best_rec:
        return None

    # Extract fields
    ac = best_rec.get("artist-credit", [{}])
    mb_artist = ac[0].get("artist", {}).get("name", artist) if ac else artist

    media     = (best_rel.get("media") or [{}])[0] if best_rel else {}
    track_obj = (media.get("track") or [{}])[0] if media else {}
    track_num = track_obj.get("number") or track_obj.get("position")

    genres    = best_rec.get("genres") or best_rec.get("tags") or []
    genre_str = genres[0].get("name", "").title() if genres else ""

    return {
        "title":        best_rec.get("title", title),
        "artist":       mb_artist,
        "album":        best_rel.get("title", "") if best_rel else "",
        "album_artist": mb_artist,
        "track":        str(track_num) if track_num else "",
        "date":         (best_rel.get("date") or "")[:4] if best_rel else "",
        "genre":        genre_str,
        "release_id":   best_rel.get("id", "") if best_rel else "",
    }


# ─── Cover art ────────────────────────────────────────────────────────────────

def fetch_cover(release_id):
    """Fetch front cover art from Cover Art Archive. Returns bytes or None."""
    if not release_id:
        return None
    for size in ("250", "500"):
        url = f"https://coverartarchive.org/release/{release_id}/front-{size}"
        req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            data = r.read()
            if len(data) > 1000:
                return data
        except Exception:
            pass
    # Try release group cover
    rg_url = f"https://musicbrainz.org/ws/2/release/{release_id}?fmt=json&inc=release-groups"
    rg_data = _mb_get(rg_url)
    rg_id   = rg_data.get("release-group", {}).get("id")
    if rg_id:
        url = f"https://coverartarchive.org/release-group/{rg_id}/front-250"
        req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            data = r.read()
            if len(data) > 1000:
                return data
        except Exception:
            pass
    return None


# ─── Tag writer ───────────────────────────────────────────────────────────────

def write_tags(path, info, cover_data):
    """Write corrected ID3 tags and embed cover art."""
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        if info.get("title"):
            tags["TIT2"] = TIT2(encoding=3, text=info["title"])
        if info.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=info["artist"])
        if info.get("album_artist") or info.get("artist"):
            tags["TPE2"] = TPE2(encoding=3, text=info.get("album_artist") or info["artist"])
        if info.get("album"):
            tags["TALB"] = TALB(encoding=3, text=info["album"])
        if info.get("track"):
            tags["TRCK"] = TRCK(encoding=3, text=str(info["track"]))
        if info.get("date"):
            tags["TDRC"] = TDRC(encoding=3, text=str(info["date"]))
        if info.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=info["genre"])
        if cover_data:
            tags.delall("APIC")
            tags["APIC"] = APIC(
                encoding=3, mime="image/jpeg",
                type=3, desc="Cover",
                data=cover_data
            )
        tags.save(path, v2_version=3)
        return True
    except Exception as e:
        return False


# ─── Progress ─────────────────────────────────────────────────────────────────

def progress(done, total, ok, fail, skip, label, bar_width=30):
    pct    = done / total if total else 1.0
    filled = int(bar_width * pct)
    bar    = c(GREEN, "█" * filled) + c(DIM, "░" * (bar_width - filled))
    trunc  = label[:50] + "…" if len(label) > 51 else label
    counts = f"{c(GREEN,'✓')}{ok} {c(RED,'✗')}{fail} {c(DIM,'⊘')}{skip}"
    print(f"\r  [{bar}] {c(BOLD,f'{done}/{total}')} {pct*100:5.1f}%  {counts}  {c(DIM,trunc)}\033[K",
          end="", flush=True)


# ─── Worker ───────────────────────────────────────────────────────────────────

def fix_one(path, lock, counters, total, log_f):
    fname = os.path.basename(path)

    result = needs_fix(path)
    if result is None:
        with lock:
            counters["skip"] += 1
            counters["done"] += 1
            progress(counters["done"], total, counters["ok"], counters["fail"], counters["skip"], "skip")
        return

    raw_title, raw_artist = result
    if not raw_title:
        with lock:
            counters["fail"] += 1
            counters["done"] += 1
            progress(counters["done"], total, counters["ok"], counters["fail"], counters["skip"], fname)
            print()
            log_f.write(f"SKIP (no title) {path}\n")
        return

    label = f"{raw_artist} - {raw_title}" if raw_artist else raw_title

    # MusicBrainz lookup
    info = mb_lookup(raw_artist or "", raw_title)
    if not info:
        # Still write the cleaned title/artist even without MB data
        info = {"title": raw_title, "artist": raw_artist, "album": "", "album_artist": raw_artist,
                "track": "", "date": "", "genre": "", "release_id": ""}

    # Cover art
    cover = fetch_cover(info.get("release_id", ""))

    # Write tags
    ok = write_tags(path, info, cover)

    with lock:
        if ok:
            counters["ok"] += 1
        else:
            counters["fail"] += 1
        counters["done"] += 1
        status = label if ok else f"WRITE FAIL: {fname}"
        if not ok:
            print()
            log_f.write(f"FAIL (write) {path}\n")
        progress(counters["done"], total, counters["ok"], counters["fail"], counters["skip"], status)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    workers = WORKERS
    args    = sys.argv[1:]
    if "--workers" in args:
        idx = args.index("--workers")
        try:
            workers = int(args[idx + 1])
        except (IndexError, ValueError):
            pass

    # Collect all MP3s
    all_mp3s = []
    for d in sorted(os.listdir(OUTPUT_ROOT)):
        pdir = os.path.join(OUTPUT_ROOT, d)
        if not os.path.isdir(pdir) or d.startswith("."):
            continue
        for f in os.listdir(pdir):
            if f.endswith(".mp3"):
                all_mp3s.append(os.path.join(pdir, f))

    total = len(all_mp3s)
    print(f"\n{c(BOLD+WHITE, '  Fix Tags for Navidrome')}")
    print(f"  {c(DIM, f'Files     →  {total:,} MP3s')}")
    print(f"  {c(DIM, f'Workers   →  {workers}  (MusicBrainz rate limit: keep ≤ 6)')}")
    print(f"  {c(DIM, 'Skips files that already have good tags.')}")
    print()

    counters = {"ok": 0, "fail": 0, "skip": 0, "done": 0}
    lock     = threading.Lock()

    with open(LOG_PATH, "w") as log_f:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fix_one, p, lock, counters, total, log_f) for p in all_mp3s]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

    print(f"\n\n{c(BOLD, '  Fix Tags complete')}")
    fixed   = counters["ok"]
    skipped = counters["skip"]
    failed  = counters["fail"]
    print(f"  {c(GREEN, f'✓ Fixed:    {fixed:,}')}")
    print(f"  {c(DIM,   f'⊘ Skipped:  {skipped:,}  (already good)')}")
    print(f"  {c(RED,   f'✗ Failed:   {failed:,}')}")
    print(f"  {c(DIM,   f'Log:        {LOG_PATH}')}")
    print()
    print(f"  {c(BOLD+CYAN, '→ Rescan Navidrome to pick up changes.')}")
    print()


if __name__ == "__main__":
    main()
