import argparse
import curses
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

from mutagen import File as MutagenFile
from PIL import Image

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus", ".aac"}
COVER_NAMES = ("folder.jpg", "large_cover.jpg")
ASCII_RAMP = " .:-=+*#%@"


def find_cover(album_dir: Path | None) -> Path | None:
    if not album_dir or not album_dir.is_dir():
        return None
    for name in COVER_NAMES:
        candidate = album_dir / name
        if candidate.is_file():
            return candidate
        for entry in album_dir.iterdir():
            if entry.is_file() and entry.name.lower() == name:
                return entry
    return None


def image_to_ascii(img_path: Path | None, width: int, height: int) -> list[str]:
    if width < 4 or height < 3 or not img_path or not img_path.is_file():
        return []
    try:
        with Image.open(img_path) as im:
            im = im.convert("L")
            # Terminal characters are roughly twice as tall as they are wide
            target_h = max(1, min(height, int(width * 0.5)))
            target_w = max(1, min(width, int(target_h * 2.0)))
            resized = im.resize((target_w, target_h), Image.Resampling.BILINEAR)
            if hasattr(resized, "get_flattened_data"):
                pixels = list(resized.get_flattened_data())
            else:
                pixels = list(resized.getdata())
            lines = []
            scale = len(ASCII_RAMP) - 1
            for r in range(target_h):
                row_chars = [
                    ASCII_RAMP[min(scale, int((pixels[r * target_w + c] / 255.0) * scale))]
                    for c in range(target_w)
                ]
                lines.append("".join(row_chars))
            return lines
    except Exception:
        return ["[cover error]"]


def get_track_info(track_path: Path) -> dict:
    artist = track_path.parent.parent.name if track_path.parent.parent else "Unknown Artist"
    album = track_path.parent.name if track_path.parent else "Unknown Album"
    title = track_path.stem
    length = 0.0

    try:
        audio = MutagenFile(track_path, easy=True)
        if audio is not None:
            if audio.get("artist"):
                artist = audio["artist"][0]
            if audio.get("album"):
                album = audio["album"][0]
            if audio.get("title"):
                title = audio["title"][0]
            if audio.info and getattr(audio.info, "length", None):
                length = float(audio.info.length)
    except Exception:
        pass

    return {
        "path": track_path,
        "artist": artist,
        "album": album,
        "title": title,
        "length": length,
    }


def scan_music_library(music_root: Path) -> list[dict]:
    tracks = []
    if not music_root.is_dir():
        return tracks
    # Structure: Music -> Artist -> Album -> Songs
    for artist_dir in sorted(music_root.iterdir(), key=lambda p: p.name.lower()):
        if not artist_dir.is_dir():
            continue
        for album_dir in sorted(artist_dir.iterdir(), key=lambda p: p.name.lower()):
            if not album_dir.is_dir():
                continue
            song_files = sorted(
                [
                    p for p in album_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                ],
                key=lambda p: p.name.lower(),
            )
            for song in song_files:
                tracks.append(get_track_info(song))
    return tracks


def build_artist_album_rows(tracks: list[dict]) -> list[dict]:
    """
    Build display rows for the Artist -> Album view:
      {"kind": "artist", "label": "Artist name", "selectable": False}
      {"kind": "album", "label": "  Album name 1", "artist": ..., "album": ..., "album_dir": ..., "track_indices": [...], "selectable": True}
    """
    grouped: dict[str, dict[Path, dict]] = {}
    for idx, t in enumerate(tracks):
        artist_name = t["artist"]
        album_dir = t["path"].parent
        album_name = t["album"]
        grouped.setdefault(artist_name, {})
        if album_dir not in grouped[artist_name]:
            grouped[artist_name][album_dir] = {
                "kind": "album",
                "label": f"  {album_name}",
                "artist": artist_name,
                "album": album_name,
                "album_dir": album_dir,
                "track_indices": [],
                "selectable": True,
            }
        grouped[artist_name][album_dir]["track_indices"].append(idx)

    rows: list[dict] = []
    for artist_name in sorted(grouped.keys(), key=lambda s: s.lower()):
        rows.append({
            "kind": "artist",
            "label": artist_name,
            "selectable": False,
        })
        albums_sorted = sorted(
            grouped[artist_name].values(),
            key=lambda a: a["album"].lower(),
        )
        rows.extend(albums_sorted)
    return rows


def fmt_time(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:
        return "00:00"
    total = int(seconds)
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class AudioBackend:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.current_path: Path | None = None
        self.start_wall: float = 0.0
        self.offset_sec: float = 0.0
        self.paused: bool = False

    def play(self, path: Path, start_at: float = 0.0):
        self.stop()
        self.current_path = path
        self.offset_sec = max(0.0, start_at)
        self.paused = False

        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if self.offset_sec > 0.1:
            cmd.extend(["-ss", f"{self.offset_sec:.2f}"])
        cmd.append(str(path))

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.start_wall = time.monotonic()

    def toggle_pause(self):
        if not self.proc or self.proc.poll() is not None:
            return
        if not self.paused:
            self.offset_sec += max(0.0, time.monotonic() - self.start_wall)
            self.paused = True
            self.proc.terminate()
            self.proc = None
        else:
            if self.current_path:
                self.play(self.current_path, start_at=self.offset_sec)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.paused = False

    def elapsed(self) -> float:
        if self.paused:
            return self.offset_sec
        if self.proc and self.proc.poll() is None:
            return self.offset_sec + max(0.0, time.monotonic() - self.start_wall)
        return self.offset_sec

    def is_finished(self) -> bool:
        if self.paused or self.current_path is None:
            return False
        return self.proc is not None and self.proc.poll() is not None


class CliPlayer:
    def __init__(self, stdscr, music_root: Path):
        self.stdscr = stdscr
        self.music_root = music_root
        self.tracks = scan_music_library(music_root)
        self.album_rows = build_artist_album_rows(self.tracks)

        self.current_idx: int = -1
        self.cursor_idx: int = 0
        self.scroll_offset: int = 0

        # Open directly on the Library view ("albums" stage)
        self.view_mode: str = "library"  # "album", "playlist", or "library"
        self.lib_stage: str = "albums"  # "albums" or "songs"
        self.selected_album_row: dict | None = None

        self.playlist: list[int] = list(range(len(self.tracks)))
        self.backend = AudioBackend()
        self.cached_cover_path: Path | None = None
        self.cached_cover_size: tuple[int, int] = (0, 0)
        self.cached_ascii: list[str] = []

        self.ensure_valid_cursor()

        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)

    def current_track(self) -> dict | None:
        if 0 <= self.current_idx < len(self.tracks):
            return self.tracks[self.current_idx]
        return None

    def preview_album_dir(self) -> Path | None:
        if self.view_mode == "library":
            if self.lib_stage == "songs" and self.selected_album_row:
                return self.selected_album_row.get("album_dir")
            if self.lib_stage == "albums" and 0 <= self.cursor_idx < len(self.album_rows):
                row = self.album_rows[self.cursor_idx]
                if row.get("selectable"):
                    return row.get("album_dir")
        track = self.current_track()
        if track:
            return track["path"].parent
        return None

    def start_track(self, idx: int):
        if not self.tracks or not (0 <= idx < len(self.tracks)):
            return
        self.current_idx = idx
        track = self.tracks[idx]
        self.backend.play(track["path"], start_at=0.0)

    def visible_track_indices(self) -> list[int]:
        if not self.tracks:
            return []
        if self.view_mode == "playlist":
            return self.playlist
        if self.view_mode == "library" and self.lib_stage == "songs" and self.selected_album_row:
            return self.selected_album_row["track_indices"]
        cur = self.current_track()
        if not cur:
            return list(range(len(self.tracks)))
        target_album_dir = cur["path"].parent
        album_indices = [
            i for i, t in enumerate(self.tracks) if t["path"].parent == target_album_dir
        ]
        return album_indices if album_indices else list(range(len(self.tracks)))

    def selectable_album_indices(self) -> list[int]:
        return [i for i, r in enumerate(self.album_rows) if r.get("selectable")]

    def ensure_valid_cursor(self):
        if self.view_mode == "library" and self.lib_stage == "albums":
            sel = self.selectable_album_indices()
            if not sel:
                self.cursor_idx = 0
                return
            if self.cursor_idx not in sel:
                self.cursor_idx = sel[0]
        elif self.view_mode == "library" and self.lib_stage == "songs":
            total_items = 1 + len(self.visible_track_indices())
            if self.cursor_idx < 0:
                self.cursor_idx = 0
            elif self.cursor_idx >= total_items:
                self.cursor_idx = total_items - 1
        else:
            vis = self.visible_track_indices()
            if not vis:
                self.cursor_idx = 0
                return
            if self.cursor_idx < 0:
                self.cursor_idx = 0
            elif self.cursor_idx >= len(vis):
                self.cursor_idx = len(vis) - 1

    def safe_addstr(self, win, y: int, x: int, text: str, attr: int = 0):
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x >= w or w <= 0:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        avail = w - x
        if avail <= 0:
            return
        try:
            win.addstr(y, x, text[:avail], attr)
        except curses.error:
            pass

    def draw_box(self, win, title: str, active: bool = False):
        h, w = win.getmaxyx()
        if h < 2 or w < 2:
            return
        attr = curses.color_pair(1) | curses.A_BOLD if active else curses.A_DIM
        try:
            win.box()
        except curses.error:
            pass
        label = f" {title} "
        if len(label) < w - 2:
            self.safe_addstr(win, 0, 2, label, attr)

    def render(self):
        self.stdscr.erase()
        max_h, max_w = self.stdscr.getmaxyx()
        if max_h < 8 or max_w < 24:
            self.safe_addstr(self.stdscr, 0, 0, "Terminal too small (min 24x8).")
            self.stdscr.refresh()
            return

        work_h = max_h - 1
        left_w = max(22, int(max_w * 0.42))
        right_w = max_w - left_w
        if right_w < 16:
            left_w = max(12, max_w - 16)
            right_w = max_w - left_w

        meta_h = 9
        top_left_h = max(4, work_h - meta_h)
        if top_left_h + meta_h > work_h:
            meta_h = max(3, work_h - top_left_h)

        top_left = self.stdscr.derwin(top_left_h, left_w, 0, 0)
        bot_left = self.stdscr.derwin(meta_h, left_w, top_left_h, 0)
        right_win = self.stdscr.derwin(work_h, right_w, 0, left_w)

        track = self.current_track()

        # 1. Top-left: ASCII art of album cover (previews browsed album or playing song's album)
        art_title = "Cover Art"
        if self.view_mode == "library" and not track:
            art_title = "Album Preview"
        self.draw_box(top_left, art_title)

        inner_h = max(1, top_left_h - 2)
        inner_w = max(1, left_w - 2)
        cover_path = find_cover(self.preview_album_dir())

        if cover_path != self.cached_cover_path or (inner_w, inner_h) != self.cached_cover_size:
            self.cached_cover_path = cover_path
            self.cached_cover_size = (inner_w, inner_h)
            self.cached_ascii = image_to_ascii(cover_path, inner_w, inner_h) if cover_path else []

        if self.cached_ascii:
            start_y = 1 + max(0, (inner_h - len(self.cached_ascii)) // 2)
            for idx, line in enumerate(self.cached_ascii):
                start_x = 1 + max(0, (inner_w - len(line)) // 2)
                self.safe_addstr(top_left, start_y + idx, start_x, line, curses.color_pair(4))
        else:
            msg = "No folder.jpg / large_cover.jpg"
            self.safe_addstr(
                top_left,
                1 + inner_h // 2,
                1 + max(0, (inner_w - len(msg)) // 2),
                msg,
                curses.A_DIM,
            )

        # 2. Bottom-left: Song metadata + progress bar
        status_tag = "PAUSED" if self.backend.paused else ("PLAYING" if track else "IDLE")
        self.draw_box(
            bot_left,
            f"Now Playing [{status_tag}]",
            active=not self.backend.paused and track is not None,
        )
        if track:
            self.safe_addstr(bot_left, 1, 2, f"Title : {track['title']}", curses.A_BOLD)
            self.safe_addstr(bot_left, 2, 2, f"Artist: {track['artist']}", curses.color_pair(1))
            self.safe_addstr(bot_left, 3, 2, f"Album : {track['album']}", curses.color_pair(2))

            elapsed = self.backend.elapsed()
            total = track["length"]
            if total > 0 and elapsed > total:
                elapsed = total
            ratio = (elapsed / total) if total > 0 else 0.0
            ratio = max(0.0, min(1.0, ratio))

            time_str = f"{fmt_time(elapsed)} / {fmt_time(total)}"
            bar_avail = max(4, left_w - 6 - len(time_str) - 1)
            filled = int(round(bar_avail * ratio))
            bar = "#" * filled + "-" * (bar_avail - filled)
            progress_y = min(meta_h - 2, 5)
            self.safe_addstr(bot_left, progress_y, 2, f"[{bar}] {time_str}")
        else:
            self.safe_addstr(bot_left, 2, 2, "Select an album and song to play.", curses.A_DIM)

        # 3. Right side
        if self.view_mode == "album":
            mode_title = "Album (Folder)"
        elif self.view_mode == "playlist":
            mode_title = "Playlist (All)"
        elif self.lib_stage == "albums":
            mode_title = "Library: Artist -> Album"
        else:
            alb_name = self.selected_album_row["album"] if self.selected_album_row else "Album"
            mode_title = f"Library: {alb_name}"

        self.draw_box(right_win, mode_title, active=True)
        self.ensure_valid_cursor()

        list_h = max(1, work_h - 2)
        list_w = max(1, right_w - 2)

        if self.cursor_idx < self.scroll_offset:
            self.scroll_offset = self.cursor_idx
        elif self.cursor_idx >= self.scroll_offset + list_h:
            self.scroll_offset = self.cursor_idx - list_h + 1

        if self.view_mode == "library" and self.lib_stage == "albums":
            if not self.album_rows:
                self.safe_addstr(right_win, 2, 2, "No albums found.", curses.A_DIM)
            else:
                for row in range(list_h):
                    r_idx = self.scroll_offset + row
                    if r_idx >= len(self.album_rows):
                        break
                    item = self.album_rows[r_idx]
                    is_selected = (r_idx == self.cursor_idx) and item.get("selectable")
                    has_playing_song = (
                        item.get("selectable") and self.current_idx in item.get("track_indices", [])
                    )

                    if not item.get("selectable"):
                        attr = curses.color_pair(1) | curses.A_BOLD
                        self.safe_addstr(right_win, 1 + row, 1, item["label"][:list_w], attr)
                    else:
                        marker = ">" if has_playing_song else " "
                        line = f" {marker}{item['label']}"
                        attr = curses.A_NORMAL
                        if is_selected:
                            attr = curses.color_pair(3) | curses.A_BOLD
                        elif has_playing_song:
                            attr = curses.color_pair(2) | curses.A_BOLD
                        self.safe_addstr(right_win, 1 + row, 1, line[:list_w], attr)

        elif self.view_mode == "library" and self.lib_stage == "songs":
            vis = self.visible_track_indices()
            total_items = 1 + len(vis)
            for row in range(list_h):
                v_idx = self.scroll_offset + row
                if v_idx >= total_items:
                    break
                is_selected = (v_idx == self.cursor_idx)
                if v_idx == 0:
                    line = ".. [Back to Albums]"
                    attr = curses.color_pair(3) | curses.A_BOLD if is_selected else curses.color_pair(4)
                    self.safe_addstr(right_win, 1 + row, 1, line[:list_w], attr)
                else:
                    t_idx = vis[v_idx - 1]
                    t = self.tracks[t_idx]
                    is_playing = (t_idx == self.current_idx)
                    prefix = "> " if is_playing else "  "
                    dur = fmt_time(t["length"])
                    left_text = f"{prefix}{t['title']}"
                    right_pad_w = max(1, list_w - len(dur) - 1)
                    line = f"{left_text[:right_pad_w].ljust(right_pad_w)} {dur}"
                    attr = curses.A_NORMAL
                    if is_selected:
                        attr = curses.color_pair(3) | curses.A_BOLD
                    elif is_playing:
                        attr = curses.color_pair(2) | curses.A_BOLD
                    self.safe_addstr(right_win, 1 + row, 1, line[:list_w], attr)

        else:
            vis = self.visible_track_indices()
            if not vis:
                self.safe_addstr(right_win, 2, 2, "Empty list.", curses.A_DIM)
            else:
                for row in range(list_h):
                    v_idx = self.scroll_offset + row
                    if v_idx >= len(vis):
                        break
                    t_idx = vis[v_idx]
                    t = self.tracks[t_idx]
                    is_playing = (t_idx == self.current_idx)
                    is_selected = (v_idx == self.cursor_idx)

                    prefix = "> " if is_playing else "  "
                    dur = fmt_time(t["length"])
                    if self.view_mode == "album":
                        left_text = f"{prefix}{t['title']}"
                    else:
                        left_text = f"{prefix}{t['artist']} - {t['title']}"

                    right_pad_w = max(1, list_w - len(dur) - 1)
                    line = f"{left_text[:right_pad_w].ljust(right_pad_w)} {dur}"

                    attr = curses.A_NORMAL
                    if is_selected:
                        attr = curses.color_pair(3) | curses.A_BOLD
                    elif is_playing:
                        attr = curses.color_pair(2) | curses.A_BOLD

                    self.safe_addstr(right_win, 1 + row, 1, line[:list_w], attr)

        help_text = " [Up/Down] Move  [Enter] Select/Play  [Bksp] Back  [Space] Pause  [n/p] Next/Prev  [m] View  [q] Quit "
        self.safe_addstr(self.stdscr, max_h - 1, 0, help_text, curses.A_REVERSE)
        self.stdscr.refresh()

    def _step_album_cursor(self, direction: int):
        sel = self.selectable_album_indices()
        if not sel:
            return
        if self.cursor_idx not in sel:
            self.cursor_idx = sel[0]
            return
        pos = sel.index(self.cursor_idx)
        self.cursor_idx = sel[(pos + direction) % len(sel)]

    def handle_key(self, key: int) -> bool:
        if key in (ord("q"), ord("Q")):
            return False

        elif key in (curses.KEY_UP, ord("k")):
            if self.view_mode == "library" and self.lib_stage == "albums":
                self._step_album_cursor(-1)
            elif self.view_mode == "library" and self.lib_stage == "songs":
                total = 1 + len(self.visible_track_indices())
                if total > 0:
                    self.cursor_idx = (self.cursor_idx - 1) % total
            else:
                vis = self.visible_track_indices()
                if vis:
                    self.cursor_idx = (self.cursor_idx - 1) % len(vis)

        elif key in (curses.KEY_DOWN, ord("j")):
            if self.view_mode == "library" and self.lib_stage == "albums":
                self._step_album_cursor(1)
            elif self.view_mode == "library" and self.lib_stage == "songs":
                total = 1 + len(self.visible_track_indices())
                if total > 0:
                    self.cursor_idx = (self.cursor_idx + 1) % total
            else:
                vis = self.visible_track_indices()
                if vis:
                    self.cursor_idx = (self.cursor_idx + 1) % len(vis)

        elif key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT, ord("h")):
            if self.view_mode == "library" and self.lib_stage == "songs":
                self.lib_stage = "albums"
                self.cursor_idx = 0
                self.scroll_offset = 0
                self.ensure_valid_cursor()

        elif key in (curses.KEY_ENTER, 10, 13):
            if self.view_mode == "library" and self.lib_stage == "albums":
                if 0 <= self.cursor_idx < len(self.album_rows):
                    row = self.album_rows[self.cursor_idx]
                    if row.get("selectable"):
                        self.selected_album_row = row
                        self.lib_stage = "songs"
                        self.cursor_idx = 1 if row.get("track_indices") else 0
                        self.scroll_offset = 0
            elif self.view_mode == "library" and self.lib_stage == "songs":
                vis = self.visible_track_indices()
                if self.cursor_idx == 0:
                    self.lib_stage = "albums"
                    self.cursor_idx = 0
                    self.scroll_offset = 0
                    self.ensure_valid_cursor()
                elif 1 <= self.cursor_idx <= len(vis):
                    self.start_track(vis[self.cursor_idx - 1])
            else:
                vis = self.visible_track_indices()
                if vis and 0 <= self.cursor_idx < len(vis):
                    self.start_track(vis[self.cursor_idx])

        elif key == ord(" "):
            self.backend.toggle_pause()
        elif key in (ord("n"), ord("N")):
            if self.tracks:
                base = self.current_idx if self.current_idx >= 0 else -1
                self.start_track((base + 1) % len(self.tracks))
        elif key in (ord("p"), ord("P")):
            if self.tracks:
                base = self.current_idx if self.current_idx >= 0 else 1
                self.start_track((base - 1) % len(self.tracks))
        elif key in (ord("m"), ord("M")):
            order = ["album", "playlist", "library"]
            self.view_mode = order[(order.index(self.view_mode) + 1) % len(order)]
            self.scroll_offset = 0
            if self.view_mode == "library":
                self.lib_stage = "albums"
                self.cursor_idx = 0
                self.ensure_valid_cursor()
            else:
                self.cursor_idx = 0
                vis = self.visible_track_indices()
                if self.current_idx >= 0 and self.current_idx in vis:
                    self.cursor_idx = vis.index(self.current_idx)

        return True

    def run(self):
        try:
            while True:
                if self.backend.is_finished() and self.tracks and self.current_idx >= 0:
                    self.start_track((self.current_idx + 1) % len(self.tracks))
                self.render()
                key = self.stdscr.getch()
                if key != -1:
                    if not self.handle_key(key):
                        break
        finally:
            self.backend.stop()


def main():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    parser = argparse.ArgumentParser(description="Terminal CLI Music Player")
    parser.add_argument(
        "music_dir",
        nargs="?",
        default=os.path.expanduser("~/Music"),
        help="Path to root Music folder (default: ~/Music)",
    )
    args = parser.parse_args()
    music_path = Path(args.music_dir).expanduser().resolve()

    if not music_path.exists():
        print(f"Music directory does not exist: {music_path}", file=sys.stderr)
        sys.exit(1)

    curses.wrapper(lambda stdscr: CliPlayer(stdscr, music_path).run())


if __name__ == "__main__":
    main()
