#!/usr/bin/env python3
"""Add the project's status board to a status line, as a clickable link (release 2.3.0, 15 Nov 2026).

    <a status line command> | python3 -I -S -B statusline_filter.py        (through statusline_launcher.py, see statusline_setup.py)

A filter: it reads the lines a status line printed (stdin), adds ` | board` right after the context usage (`█░░░ 12%`),
before whatever follows it on that line, where `board` is an OSC 8 hyperlink to the project's board address
(`http://localhost:8800/<name>`), and prints the result. Where there is no context bar, or the line has no room for the
link, it goes at the end of the first line with room, else of the last line with room, else on a line of its own: never
past the window's edge. It is what the hand-made filters that some set-ups used before 2.3.0 did (a script named `hud-board-link.py`), generalised: nothing of one machine in it.

Nothing is added when the project has no board; ` (down)` follows the link where there is room for it, and the link is
red, when the board's server is not running. **Any error prints the lines exactly as they came** (the bytes, not a copy
of the text): the status line never breaks because of this.

The address is not worked out here: the helper beside this file (`board_link.py`, the one behind the plugin's row above
the prompt) is asked, so the status line and the row never disagree. It reads a few files under the status-board home,
sends no request and writes nothing. The status line is refreshed every few seconds in every session, so the answer is kept
for a minute in a small file per project in the plugin's data folder (`statusline-cache/`, not the temporary folder, which another
local user may share), and an address read back from it is checked like a fresh one, **except that it is asked again at once when a board has
started, stopped being listed or been created since** (15 Nov 2026: a board started during a session showed up to a minute
late). `servers/` and `boards/` of the status-board home change in those cases: two `stat` calls per refresh tell.

**The width.** The command `statusline_setup.py add` writes gives claude-hud 8 columns fewer than the window (the width of
` | board`), so that the line with the context usage always has room for the link right after it. This filter measures
against the FULL width: `BOARD_LINK_COLUMNS` (the window's `COLUMNS` before that reduction), else `COLUMNS`, else no limit
(a status line with no width known has none to wrap at either). So no line it places the link on is wider than the window.

**Where the link was seen.** Each time it places the link it touches `seen/<first 16 hex of the sha1 of the start folder's
path>` in the plugin's data folder, at most once a minute: the row above the prompt (hooks/board-link.tsx) hides while the
status line shows the link and comes back by itself when it stops. The start folder is hashed as it was given and, where
that differs, as it resolves, so a reader that has the other spelling finds the file too. The data folder is
`CLAUDE_PLUGIN_DATA` when the launcher gives it (its own folder; a status line command is not given it by Claude Code),
else `<config>/plugins/data/project-init-claudecode-superinit`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

LINK_TEXT = "board"  # what is shown; the address is behind it
DOWN_TEXT = " (down)"  # after the link when the board's server is not running
SEPARATOR = " | "  # what the link is joined with
SEPARATORS = (" | ", " │ ")  # what claude-hud puts between its segments

CACHE_SECONDS = 60
SEEN_EVERY_SECONDS = 60
HELPER_TIMEOUT = 2.0
DATA_ID = "project-init-claudecode-superinit"  # `<name>-<marketplace>` of the plugin's marketplace install

CYAN, RED, DIM, RESET = "\x1b[36m", "\x1b[31m", "\x1b[2m", "\x1b[0m"
# The context usage as claude-hud draws it: the bar (█ and ░, with colour codes between), then its percentage.
CONTEXT_USAGE = re.compile(r"[█░](?:\x1b\[[0-9;]*m|[█░])*\s*(?:\x1b\[[0-9;]*m)*\d+%(?:\x1b\[[0-9;]*m)*")
ESCAPES = re.compile(r"\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;]*[A-Za-z]")
ADDRESS = re.compile(r"http://localhost:\d+/[A-Za-z0-9._~%-]+")


def plugin_data_dir(env=os.environ) -> Path:
    """The plugin's data folder: the one place that says where it is (SPIKE.md section 5)."""
    explicit = env.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return Path(explicit)
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config / "plugins" / "data" / DATA_ID


def open_new(first: Path, mode: int) -> tuple[int, Path]:
    """Create a NEW file at `first` (or, where that name is taken, at `first-1`, `first-2`, …) and return its descriptor and path.

    It is created closed: with the owner's bits of `mode` only (0600 at most), whatever the umask, and with O_EXCL and O_NOFOLLOW,
    so that a file or a symlink already at the name (a planted one, a dangling one) is never opened, followed or overwritten. The
    caller writes, then gives the file its full `mode` with fchmod: never open to more than `mode`, not even for an instant (the
    security review of 15 Nov 2026: a backup made by copy-then-chmod was open to everyone the umask allowed)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1000):
        path = first if n == 0 else first.with_name(f"{first.name}-{n}")
        try:
            return os.open(path, flags, mode & 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free name beside {first}")


def write_private(path: Path, payload: bytes, mode: int = 0o600, sync: bool = False) -> None:
    """`payload` into `path` through a new file of its own (exclusive, closed, see open_new) and a rename: a reader sees the old
    file or the whole new one, and the file has `mode` from the moment it has its name."""
    fd, temp = open_new(path.with_name(f".{path.name}.{os.getpid()}.tmp"), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def status_board_home(env=os.environ) -> Path:
    """Where the boards and the servers' records are: serve.py's own rule."""
    return Path(env.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board")


def ask_helper(start: str) -> dict:
    """{"url": ..., "up": ...} for the project started in `start`, or {} when it has no board."""
    helper = Path(__file__).resolve().with_name("board_link.py")
    if not helper.is_file():
        return {}
    done = subprocess.run(
        [sys.executable, "-B", str(helper), "--start", start, "--no-agent-check"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=HELPER_TIMEOUT,
        check=False,
    )
    said = json.loads(done.stdout.strip() or "null")
    url = said.get("url") if isinstance(said, dict) else None
    if not isinstance(url, str) or not ADDRESS.fullmatch(url):
        return {}
    return {"url": url, "up": said.get("up") is True}


def boards_changed_at() -> float:
    """When a board last started, stopped being listed or was created: the newer time of the two folders the plugin's
    servers write (`servers/` holds one record per running board, `boards/` one folder per board)."""
    state = status_board_home()
    newest = 0.0
    for folder in ("servers", "boards"):
        try:
            newest = max(newest, (state / folder).stat().st_mtime)
        except OSError:
            pass  # no board has ever run here
    return newest


def kept_answer(cache: Path) -> dict | None:
    """The answer kept in `cache` when it is fresh and well-formed, else None (ask again). A kept address is held to the same
    shape as a fresh one: it ends up inside an escape sequence of the user's terminal."""
    kept_at = cache.stat().st_mtime
    if time.time() - kept_at >= CACHE_SECONDS or kept_at < boards_changed_at():
        return None
    kept = json.loads(cache.read_text() or "{}")
    if not isinstance(kept, dict):
        return None
    if kept == {}:
        return kept  # "no board", kept for a minute like any other answer
    url = kept.get("url")
    if not isinstance(url, str) or not ADDRESS.fullmatch(url):
        return None
    return {"url": url, "up": kept.get("up") is True}


def board_of(start: str) -> dict:
    """ask_helper, at most once a minute per project, and again at once when a board has started since.

    The answer is kept in the plugin's data folder (`statusline-cache/`), which is under the user's own config folder, and not in
    the temporary folder: on a machine whose temporary folder is shared (/tmp on Linux) another local user could create a folder
    of the name first and put an address of their choosing in it (review of 15 Nov 2026, finding 3)."""
    cache_dir = plugin_data_dir() / "statusline-cache"
    cache = cache_dir / hashlib.sha1(start.encode()).hexdigest()[:16]
    try:
        kept = kept_answer(cache)
        if kept is not None:
            return kept
    except (OSError, ValueError):
        pass  # nothing kept yet for this project, or an older file: ask again
    board = ask_helper(start)
    try:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(cache, json.dumps(board).encode(), 0o600)  # the board's address names the project: for the owner only
    except OSError:
        pass  # still shown; only asked again at the next refresh
    return board


def width(text: str) -> int:
    """Columns `text` takes in a terminal, colour and link codes not counted."""
    plain = ESCAPES.sub("", text)
    return sum(0 if unicodedata.combining(c) or c in "️‍" else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in plain)


def after_context(line: str, link: str) -> str | None:
    """`line` with the link as a segment of its own right after the context usage (`█░░░ 12%`), or None.

    The usage ends at the next plain separator of the line (` | ` or claude-hud's ` │ `), or at the line's end when
    nothing follows it on that line. None when the line shows no context bar, so the caller falls back to the end of a line.
    """
    usage = CONTEXT_USAGE.search(line)
    if usage is None:
        return None
    cuts = [at for at in (line.find(sep, usage.end()) for sep in SEPARATORS) if at >= 0]
    cut = min(cuts) if cuts else len(line)
    return f"{line[:cut]}{DIM}{SEPARATOR}{RESET}{link}{line[cut:]}"


def place_link(hud: str, board: dict, columns: int) -> str | None:
    """`hud` with the board's link in it, or None where there is no board (or nothing to put it on).

    `columns` is the window's full width, 0 for no limit. The link goes right after the context usage where the line has
    room, else at the end of the first line with room, else of the last, else on a line of its own."""
    url = board.get("url")
    if not url:
        return None
    up = bool(board.get("up"))
    # A board that is down is red, and says so in words where the line has room for them.
    short = f"\x1b]8;;{url}\x1b\\{CYAN if up else RED}{LINK_TEXT}{RESET}\x1b]8;;\x1b\\"
    links = [short] if up else [f"{short}{DIM}{DOWN_TEXT}{RESET}", short]
    lines = hud.rstrip("\n").split("\n")
    filled = [i for i, line in enumerate(lines) if width(line) > 0]
    if not filled:
        return None
    fits = lambda line, link: columns <= 0 or width(line) + width(SEPARATOR + link) <= columns  # noqa: E731
    for link in links:
        for i in filled:
            placed = after_context(lines[i], link)
            if placed is not None and fits(lines[i], link):
                lines[i] = placed
                return "\n".join(lines) + "\n"
    for link in links:
        for i in [filled[0], *reversed(filled[1:])]:
            if fits(lines[i], link):
                lines[i] = f"{lines[i]}{DIM}{SEPARATOR}{RESET}{link}"
                return "\n".join(lines) + "\n"
    lines.append(links[0])
    return "\n".join(lines) + "\n"


def full_width(env=os.environ) -> int:
    """The window's width before the command took 8 columns off for claude-hud, else COLUMNS, else 0 (no limit)."""
    for name in ("BOARD_LINK_COLUMNS", "COLUMNS"):
        raw = (env.get(name) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    return 0


def note_seen(start: str) -> None:
    """Say that the status line showed this project's link: touch `seen/<sha1(start)[:16]>`, at most once a minute."""
    names = {start, os.path.realpath(start)}
    for name in names:
        marker = plugin_data_dir() / "seen" / hashlib.sha1(name.encode()).hexdigest()[:16]
        try:
            if time.time() - marker.stat().st_mtime < SEEN_EVERY_SECONDS:
                continue
        except OSError:
            pass
        try:
            marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))  # empty, for the owner only; never through a symlink
            os.utime(marker, None)
        except OSError:
            pass  # the link is still shown; only the row cannot learn it


def main() -> None:
    raw = sys.stdin.buffer.read()
    out = raw
    try:
        start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        if os.path.isdir(start):
            hud = raw.decode("utf-8", "surrogateescape")  # round-trips every byte, valid UTF-8 or not
            placed = place_link(hud, board_of(start), full_width())
            if placed is not None:
                out = placed.encode("utf-8", "surrogateescape")
                note_seen(start)
    except Exception:  # noqa: BLE001 - whatever goes wrong, the status line's own lines are printed as they came
        out = raw
    sys.stdout.buffer.write(out)


if __name__ == "__main__":
    main()
