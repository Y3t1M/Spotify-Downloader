#!/usr/bin/env python3
"""
Spotify Downloader — Interactive Setup Wizard
──────────────────────────────────────────────
Run once (or any time) to configure:
  • Output directory
  • Download worker count
  • Playlists (add / edit / delete / paste URLs in bulk)

Writes:
  • config.json          ← output dir + workers
  • playlists/playlists.txt ← playlist definitions

Usage:
    python3 setup.py
"""

import os
import json
import sys
import re
import textwrap

# ─── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(SCRIPT_DIR, "config.json")
PLAYLISTS_DIR = os.path.join(SCRIPT_DIR, "playlists")
PLAYLISTS_TXT = os.path.join(PLAYLISTS_DIR, "playlists.txt")

# ─── Colours ──────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"
BLUE   = "\033[34m"

def c(color, text): return f"{color}{text}{RESET}"
def dim(t):         return c(DIM, t)
def bold(t):        return c(BOLD + WHITE, t)
def ok(t):          return c(GREEN, t)
def err(t):         return c(RED, t)
def hi(t):          return c(CYAN, t)
def warn(t):        return c(YELLOW, t)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def rule(char="─", width=62, color=CYAN):
    print(c(color + BOLD, char * width))

def header(title):
    print()
    rule()
    print(f"  {bold(title)}")
    rule()
    print()

def prompt(label, default=None, password=False):
    hint = f"  {dim(f'[{default}]')} " if default is not None else "  "
    try:
        val = input(f"  {c(CYAN, '›')} {label}{hint}").strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n\n{warn('  Setup cancelled.')}\n")
        sys.exit(0)
    return val if val else (default if default is not None else "")

def confirm(label, default=True):
    hint = "Y/n" if default else "y/N"
    val  = prompt(f"{label} [{hint}]").lower()
    if val == "":
        return default
    return val in ("y", "yes")

def pause():
    try:
        input(f"\n  {dim('Press Enter to continue…')}")
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)

def clear_screen():
    os.system("clear")

# ─── Config I/O ───────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "output_dir": os.path.expanduser("~/Music/SpotifyDownloads"),
    "workers":    os.cpu_count() or 8,
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            # Merge with defaults for any missing keys
            return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\n  {ok('✓')} Saved {dim(CONFIG_PATH)}")

# ─── Playlist I/O ─────────────────────────────────────────────────────────────

def load_playlists():
    """Parse playlists.txt → list of {name, urls}."""
    if not os.path.exists(PLAYLISTS_TXT):
        return []
    playlists, current_name, current_urls = [], None, []
    with open(PLAYLISTS_TXT, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("https://open.spotify.com/track/"):
                if current_name is not None:
                    current_urls.append(line)
            elif line.startswith(("https://open.spotify.com/local/", "spotify:local/")):
                pass  # skip local files silently
            else:
                if current_name is not None:
                    playlists.append({"name": current_name, "urls": current_urls})
                current_name = line
                current_urls = []
    if current_name is not None:
        playlists.append({"name": current_name, "urls": current_urls})
    return playlists

def save_playlists(playlists):
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    with open(PLAYLISTS_TXT, "w") as f:
        for i, pl in enumerate(playlists):
            if i > 0:
                f.write("\n")
            f.write(pl["name"] + "\n")
            for url in pl["urls"]:
                f.write(url + "\n")
    print(f"\n  {ok('✓')} Saved {dim(PLAYLISTS_TXT)}")

# ─── URL parsing helpers ──────────────────────────────────────────────────────

TRACK_RE    = re.compile(r'https://open\.spotify\.com/track/[A-Za-z0-9]+(?:\?[^\s]*)?')
PLAYLIST_RE = re.compile(r'https://open\.spotify\.com/playlist/[A-Za-z0-9]+(?:\?[^\s]*)?')

def extract_track_urls(text):
    """Pull all unique spotify track URLs out of a blob of pasted text."""
    urls = TRACK_RE.findall(text)
    # Normalise — strip query params
    clean = []
    seen  = set()
    for u in urls:
        base = u.split("?")[0]
        if base not in seen:
            seen.add(base)
            clean.append(base)
    return clean

def collect_urls_interactively():
    """
    Let the user paste a big blob of text (Spotify share links, playlist exports,
    plain lists of URLs — anything).  Returns a list of clean track URLs.
    """
    print(f"""
  {bold('Paste your Spotify track URLs below.')}
  {dim('Accepted formats:')}
    {dim('• One URL per line')}
    {dim('• Bulk paste from a Spotify playlist export')}
    {dim('• Any text — only track URLs will be extracted')}

  {warn('When done, type')} {bold('END')} {warn('on its own line and press Enter.')}
  {dim('(or press Ctrl+C to cancel)')}
""")
    lines = []
    try:
        while True:
            line = input("  ")
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        print()
        return []

    blob = "\n".join(lines)
    urls = extract_track_urls(blob)

    if not urls:
        print(f"\n  {err('✗ No Spotify track URLs found in that text.')}")
        has_playlist = PLAYLIST_RE.search(blob)
        if has_playlist:
            print(f"  {warn('ℹ  Detected a playlist URL — this tool needs individual track URLs.')}")
            print(f"  {dim('  Tip: Open the playlist in Spotify, select all tracks, right-click → Share → Copy Song Links.')}")
    else:
        print(f"\n  {ok(f'✓ Found {len(urls):,} track URL(s)')}")

    return urls

# ─── Screens ──────────────────────────────────────────────────────────────────

def screen_settings(cfg):
    header("⚙  Settings")

    cpu = os.cpu_count() or 4
    rec = min(max(cpu + cpu // 2, 4), 20)

    print(f"  {bold('Output directory')}")
    print(f"  {dim('Where MP3s are saved. Subfolders are created per playlist.')}")
    new_dir = prompt("Output dir", default=cfg["output_dir"])
    new_dir = os.path.expanduser(new_dir)

    print()
    print(f"  {bold('Worker count')}")
    print(f"  {dim(f'Parallel downloads. Recommended for your machine: {rec}  (CPU: {cpu})')}")
    print(f"  {dim('Higher = faster, but risks rate-limiting from YouTube (~429 errors).')}")
    raw = prompt("Workers", default=str(cfg["workers"]))
    try:
        new_workers = max(1, int(raw))
    except ValueError:
        print(f"  {warn('Invalid number — keeping current value.')}")
        new_workers = cfg["workers"]

    cfg["output_dir"] = new_dir
    cfg["workers"]    = new_workers
    save_config(cfg)
    pause()
    return cfg


def screen_playlist_list(playlists):
    header("♫  Playlists")

    if not playlists:
        print(f"  {dim('No playlists configured yet.')}\n")
    else:
        for i, pl in enumerate(playlists):
            print(f"  {c(CYAN, str(i + 1)):>4}.  {bold(pl['name'])}  {dim(f'({len(pl[\"urls\"]):,} tracks)')}")
        print()

    print(f"  {hi('a')}  Add new playlist")
    if playlists:
        print(f"  {hi('e')}  Edit a playlist")
        print(f"  {hi('d')}  Delete a playlist")
    print(f"  {hi('b')}  Back")
    print()

    try:
        choice = input(f"  {c(CYAN, '›')} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return playlists, "back"
    return playlists, choice


def screen_add_playlist(playlists):
    header("♫  Add Playlist")
    name = prompt("Playlist name").strip()
    if not name:
        print(f"  {err('✗ Name cannot be empty.')}")
        pause()
        return playlists

    # Check for duplicate
    if any(p["name"].lower() == name.lower() for p in playlists):
        print(f"  {warn(f'A playlist named \"{name}\" already exists.')}")
        if not confirm("Overwrite it?", default=False):
            pause()
            return playlists
        playlists = [p for p in playlists if p["name"].lower() != name.lower()]

    urls = collect_urls_interactively()
    if not urls:
        pause()
        return playlists

    playlists.append({"name": name, "urls": urls})
    save_playlists(playlists)
    pause()
    return playlists


def screen_edit_playlist(playlists):
    if not playlists:
        return playlists

    header("♫  Edit Playlist")
    for i, pl in enumerate(playlists):
        print(f"  {c(CYAN, str(i + 1)):>4}.  {bold(pl['name'])}  {dim(f'({len(pl[\"urls\"]):,} tracks)')}")
    print()

    raw = prompt("Playlist number (or blank to cancel)")
    if not raw:
        return playlists
    try:
        idx = int(raw) - 1
        if not (0 <= idx < len(playlists)):
            raise ValueError
    except ValueError:
        print(f"  {err('Invalid selection.')}")
        pause()
        return playlists

    pl = playlists[idx]
    print(f"\n  Editing: {bold(pl['name'])}  {dim(f'({len(pl[\"urls\"]):,} tracks currently)')}")
    print()
    print(f"  {hi('r')}  Replace all URLs  {dim('(paste a fresh list)')}")
    print(f"  {hi('a')}  Append URLs       {dim('(add to existing list)')}")
    print(f"  {hi('n')}  Rename playlist")
    print(f"  {hi('b')}  Back")
    print()

    try:
        choice = input(f"  {c(CYAN, '›')} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return playlists

    if choice == "r":
        urls = collect_urls_interactively()
        if urls:
            playlists[idx]["urls"] = urls
            save_playlists(playlists)
    elif choice == "a":
        urls = collect_urls_interactively()
        if urls:
            existing = set(playlists[idx]["urls"])
            new_urls = [u for u in urls if u not in existing]
            playlists[idx]["urls"].extend(new_urls)
            print(f"  {ok(f'✓ Added {len(new_urls):,} new URL(s)  ({len(urls) - len(new_urls):,} duplicates skipped)')}")
            save_playlists(playlists)
    elif choice == "n":
        new_name = prompt("New name", default=pl["name"]).strip()
        if new_name and new_name != pl["name"]:
            playlists[idx]["name"] = new_name
            save_playlists(playlists)
    elif choice == "b":
        return playlists
    else:
        print(f"  {err('Unknown option.')}")

    pause()
    return playlists


def screen_delete_playlist(playlists):
    if not playlists:
        return playlists

    header("♫  Delete Playlist")
    for i, pl in enumerate(playlists):
        print(f"  {c(CYAN, str(i + 1)):>4}.  {bold(pl['name'])}  {dim(f'({len(pl[\"urls\"]):,} tracks)')}")
    print()

    raw = prompt("Playlist number to delete (or blank to cancel)")
    if not raw:
        return playlists
    try:
        idx = int(raw) - 1
        if not (0 <= idx < len(playlists)):
            raise ValueError
    except ValueError:
        print(f"  {err('Invalid selection.')}")
        pause()
        return playlists

    name = playlists[idx]["name"]
    if confirm(f"Delete \"{name}\"?", default=False):
        playlists.pop(idx)
        save_playlists(playlists)
        print(f"  {ok(f'✓ Deleted \"{name}\"')}")
    else:
        print(f"  {dim('Cancelled.')}")

    pause()
    return playlists


def screen_summary(cfg, playlists):
    header("✓  Current Configuration")
    total_tracks = sum(len(p["urls"]) for p in playlists)

    print(f"  {bold('Output directory')}   {dim(cfg['output_dir'])}")
    print(f"  {bold('Workers')}            {dim(str(cfg['workers']))}")
    print()
    if playlists:
        print(f"  {bold('Playlists')}  {dim(f'({len(playlists)} total, {total_tracks:,} tracks)')}")
        for pl in playlists:
            print(f"    {ok('•')} {pl['name']}  {dim(f'({len(pl[\"urls\"]):,} tracks)')}")
    else:
        print(f"  {warn('No playlists configured yet.')}")
    print()
    print(f"  {dim('Run downloads with:')}")
    print(f"  {hi('  python3 download_all_ytdlp.py')}")
    print(f"  {dim('Retry failures with:')}")
    print(f"  {hi('  python3 retry_failed.py')}")
    print()
    pause()


# ─── Main menu ────────────────────────────────────────────────────────────────

def main():
    clear_screen()
    print(f"""
  {bold('Spotify Downloader — Setup Wizard')}
  {dim('Configure output directory, workers, and playlists.')}
  {dim('Changes are saved immediately to config.json and playlists/playlists.txt')}
""")

    cfg       = load_config()
    playlists = load_playlists()

    while True:
        print(f"\n  {bold('Main Menu')}")
        rule("─", 40, DIM)
        total = sum(len(p["urls"]) for p in playlists)
        print(f"  {hi('1')}  Playlists        {dim(f'({len(playlists)} playlists, {total:,} tracks)')}")
        print(f"  {hi('2')}  Settings         {dim(f'(output: {cfg[\"output_dir\"]}  workers: {cfg[\"workers\"]})')}")
        print(f"  {hi('3')}  Show summary")
        print(f"  {hi('q')}  Quit")
        print()

        try:
            choice = input(f"  {c(CYAN, '›')} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{warn('  Bye!')}\n")
            sys.exit(0)

        if choice == "1":
            while True:
                playlists, action = screen_playlist_list(playlists)
                if action == "a":
                    playlists = screen_add_playlist(playlists)
                elif action == "e":
                    playlists = screen_edit_playlist(playlists)
                elif action == "d":
                    playlists = screen_delete_playlist(playlists)
                elif action in ("b", "back", ""):
                    break
                else:
                    pass  # unrecognised — loop back

        elif choice == "2":
            cfg = screen_settings(cfg)

        elif choice == "3":
            screen_summary(cfg, playlists)

        elif choice in ("q", "quit", "exit"):
            print(f"\n  {ok('✓ Done!')}\n")
            sys.exit(0)

        else:
            print(f"  {err('Unknown option — try 1, 2, 3, or q.')}")


if __name__ == "__main__":
    main()
