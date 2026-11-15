#!/usr/bin/env python3
"""The board's link in the status line: the filter, the launcher and the set-up script (release 2.3.0, part B, 15 Nov 2026).

    python3 tests/test_statusline.py --plugin <plugin dir> [--claude-hud <dist/index.js of claude-hud>]

What is tested (status-board/): `statusline_filter.py` (reads the lines a status line printed on stdin, adds ` | board` as a
link right after the context usage, and prints them; any failure prints them as they came), `statusline_launcher.py` (the
small file copied into the plugin's data folder, which finds the installed plugin and runs its filter), and
`statusline_setup.py` (`status`, `add`, `remove`, `record`: the one program that edits a user's settings.json, only after
the user's yes, and gives the file back byte for byte on `remove`).

The requirement behind most of it, from two reports of 15 Nov 2026: a board started during a session shows at
the next refresh of the status line, and with claude-hud the link sits right after the context usage, never at the end of
the status line and never past the window's edge. The command `add` writes gives claude-hud 8 columns fewer than the window
(the width of ` | board`), and the filter measures against the full width.

EVERY RUN is in temporary folders: HOME, CLAUDE_CONFIG_DIR, STATUS_BOARD_HOME, TMPDIR, and settings.json fixtures made here.
Nothing reads or writes the account's own settings.json or status line, none of these programs is ever run against the real
home, and no test binds a port from 8800 to 8920 or sends a request to anything. The one test that runs claude-hud itself
(Node, `dist/index.js`, a recorded stdin) runs it in a temporary home and says "not run" by name where it is not installed.

Run against the code before the change, every test fails: the three files do not exist.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="the plugin directory under test")
ap.add_argument("--claude-hud", default="", help="claude-hud's dist/index.js, for the test that runs it (found by itself when omitted)")
ap.add_argument("--bash", action="append", default=[], help="another bash to run the composed command under (repeatable): a current one, beside /bin/bash")
ap.add_argument("--only", default="", help="run only the tests whose name contains this")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
STATUS = PLUGIN / "status-board"
FILTER, LAUNCHER, SETUP = STATUS / "statusline_filter.py", STATUS / "statusline_launcher.py", STATUS / "statusline_setup.py"
sys.dont_write_bytecode = True

PLUGIN_KEY = "project-init@claudecode-superinit"
DATA_ID = "project-init-claudecode-superinit"
MARKER = "statusline-board-link.py"
TMP = Path(tempfile.mkdtemp(prefix="statusline-")).resolve()
FAKE_HOME = TMP / "home"
TMPDIR = TMP / "tmpdir"
SAFE_PATH = f"/usr/bin:/bin:{Path(sys.executable).parent}"
REAL_HOME = Path(os.path.expanduser("~"))  # read once, only to look for claude-hud's files; nothing is written there
PROCESSES: list[subprocess.Popen] = []
FRONT_PORT = 0
ESCAPES = re.compile(r"\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;]*[A-Za-z]")


class Skip(Exception):
    """A test that cannot run here, said by name instead of passed."""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not 8800 <= port <= 8920, port
    return port


def setup() -> None:
    global FRONT_PORT
    for d in (FAKE_HOME, TMPDIR):
        d.mkdir(parents=True, exist_ok=True)
    FRONT_PORT = free_port()


def strip(text: str) -> str:
    return ESCAPES.sub("", text)


def width(text: str) -> int:
    plain = strip(text)
    return sum(0 if unicodedata.combining(c) or c in "️‍" else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in plain)


# ── a world: a project, its board, a plugin install record, a config folder ──────────────────────────────────────────
class World:
    def __init__(self, with_board: bool = True, up: bool = True) -> None:
        self.root = Path(tempfile.mkdtemp(dir=TMP, prefix="w-"))
        self.state = self.root / "status-board-home"
        self.state.mkdir()
        self.config = self.root / "config"
        (self.config / "plugins").mkdir(parents=True)
        self.project = self.root / "shop-app"
        git_init(self.project)
        self.live = live_process()
        self.install(PLUGIN)
        if with_board:
            self.add_board(up=up)

    def install(self, plugin: Path | None, version: str = "2.3.0") -> None:
        record = {"version": 2, "plugins": {}}
        if plugin is not None:
            record["plugins"][PLUGIN_KEY] = [{"scope": "user", "installPath": str(plugin), "version": version}]
        (self.config / "plugins" / "installed_plugins.json").write_text(json.dumps(record))

    def add_board(self, up: bool = True) -> None:
        board = self.state / "boards" / "shop-app"
        board.mkdir(parents=True, exist_ok=True)
        (board / "repo.json").write_text(json.dumps({"repo": str(self.project.resolve())}))
        (board / "narrative.json").write_text(json.dumps({"headline": "x"}))
        records = self.state / "servers"
        records.mkdir(mode=0o700, exist_ok=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        rec = {"v": 1, "service": "status-board", "slug": "shop-app", "name": "shop-app", "repo": str(self.project.resolve()),
               "main_repo": None, "port": 40111, "host": "127.0.0.1", "pid": self.live.pid if up else dead.pid,
               "started": "2026-11-15T00:00:00+01:00", "plugin_version": "2.2.1"}
        (records / "40111.json").write_text(json.dumps(rec))

    @property
    def data(self) -> Path:
        return self.config / "plugins" / "data" / DATA_ID

    @property
    def url(self) -> str:
        return f"http://localhost:{FRONT_PORT}/shop-app"

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(FAKE_HOME), "PATH": SAFE_PATH, "STATUS_BOARD_HOME": str(self.state), "STATUS_BOARD_FRONT_PORT": str(FRONT_PORT),
               "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_PROJECT_DIR": str(self.project), "TMPDIR": str(TMPDIR),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}


def live_process() -> subprocess.Popen:
    """One process that stays alive for the whole run: the pid in every server record of every world."""
    if not PROCESSES:
        PROCESSES.append(subprocess.Popen(["sleep", "600"]))
    return PROCESSES[0]


def git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)


def fresh_tmpdir() -> None:
    """The filter keeps its answers in the temporary folder: every test starts with none."""
    shutil.rmtree(TMPDIR, ignore_errors=True)
    TMPDIR.mkdir(parents=True)


def need(path: Path) -> Path:
    assert path.exists(), f"{path.relative_to(PLUGIN)} does not exist"
    return path


# ── running the filter and the launcher ─────────────────────────────────────────────────────────────────────────────
HUD = "[Sonnet] │ shop-app git:(main)\nContext \x1b[32m█\x1b[2m░░░░░░░░░\x1b[0m \x1b[32m12%\x1b[0m\n"


def run_filter(world: World, data: bytes | str, umask: int | None = None, **env_extra: str) -> bytes:
    raw = data.encode() if isinstance(data, str) else data
    done = subprocess.run([sys.executable, "-I", "-S", "-B", str(need(FILTER))], input=raw, capture_output=True, env=world.env(**env_extra),
                          cwd=str(TMP), timeout=30, preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)
    assert done.returncode == 0, f"the filter exited {done.returncode}: {done.stderr.decode(errors='replace')[:200]}"
    return done.stdout


def lines(out: bytes) -> list[str]:
    return strip(out.decode("utf-8", "replace")).rstrip("\n").split("\n")


# ── step 2: the filter ──────────────────────────────────────────────────────────────────────────────────────────────
def test_link_right_after_the_context_usage_and_nothing_else_moves():
    fresh_tmpdir()
    world = World()
    out = run_filter(world, HUD, COLUMNS="100")
    assert lines(out) == ["[Sonnet] │ shop-app git:(main)", "Context █░░░░░░░░░ 12% | board"], lines(out)
    assert world.url.encode() in out and b"\x1b]8;;" in out, "board is a link (OSC 8) to the address"
    assert out.startswith(b"[Sonnet] "), "what came before is untouched"


def test_link_goes_before_the_next_segment_of_the_same_line():
    fresh_tmpdir()
    world = World()
    for separator in (" │ ", " | "):
        line = f"[Sonnet]{separator}Context █░░░░░ 12%{separator}git:(main)\n"
        got = lines(run_filter(world, line, COLUMNS="100"))
        assert got == [f"[Sonnet]{separator}Context █░░░░░ 12% | board{separator}git:(main)"], got


def test_end_of_the_first_line_with_room_when_there_is_no_context_bar():
    fresh_tmpdir()
    world = World()
    got = lines(run_filter(world, "line one\nline two\n", COLUMNS="100"))
    assert got == ["line one | board", "line two"], got


def test_no_board_prints_the_input_byte_for_byte():
    fresh_tmpdir()
    world = World(with_board=False)
    for data in (HUD, "", "no newline at the end", HUD.rstrip("\n") + "\n\n\n"):
        assert run_filter(world, data, COLUMNS="100") == data.encode(), repr(data)


def test_non_utf8_bytes_survive_unchanged():
    fresh_tmpdir()
    odd = b"caf\xe9 \xff\xfe\n" + HUD.encode()
    assert run_filter(World(with_board=False), odd, COLUMNS="100") == odd, "no board: the bytes as they came"
    world = World()
    out = run_filter(world, odd, COLUMNS="100")
    assert out.startswith(b"caf\xe9 \xff\xfe\n"), "with a board: the other lines' bytes are kept too"
    assert b"| board" in strip(out.decode("utf-8", "replace")).encode()


def test_any_exception_prints_the_input_unchanged():
    fresh_tmpdir()
    world = World()
    odd = b"\xff garbage \xfe\n" + HUD.encode()
    code = ("import sys; sys.path.insert(0, %r)\nimport statusline_filter as f\n"
            "def boom(*a, **k): raise RuntimeError('boom')\nf.ask_helper = boom\nf.board_of = boom\nf.main()\n") % str(STATUS)
    done = subprocess.run([sys.executable, "-B", "-c", code], input=odd, capture_output=True, env=world.env(COLUMNS="100"), cwd=str(TMP), timeout=30)
    assert done.returncode == 0 and done.stdout == odd, (done.returncode, done.stdout[:60], done.stderr[-200:])
    garbage = TMP / "garbage-helper"
    shutil.copytree(STATUS, garbage, ignore=shutil.ignore_patterns("__pycache__"))
    (garbage / "board_link.py").write_text("import sys\nsys.stdout.write('not json {')\n")
    done = subprocess.run([sys.executable, "-B", str(garbage / "statusline_filter.py")], input=odd, capture_output=True, env=world.env(), cwd=str(TMP), timeout=30)
    assert done.stdout == odd, "a helper that prints garbage costs the link, never the status line"


def test_filter_measures_full_width_from_BOARD_LINK_COLUMNS():
    fresh_tmpdir()
    world = World()
    line = "Context " + "█" * 30 + " 12%"
    base = width(line)
    tight = lines(run_filter(world, line + "\n", COLUMNS="500", BOARD_LINK_COLUMNS=str(base + 7)))
    assert tight[0] == line and "board" not in tight[0], f"{base + 7} columns leave no room on that line: {tight}"
    assert tight[-1] == "board", f"it goes on a line of its own: {tight}"
    room = lines(run_filter(world, line + "\n", COLUMNS="10", BOARD_LINK_COLUMNS=str(base + 8)))
    assert room == [line + " | board"], f"measured against BOARD_LINK_COLUMNS, not the reduced COLUMNS: {room}"
    only_columns = lines(run_filter(world, line + "\n", COLUMNS=str(base + 7)))
    assert only_columns[-1] == "board", "without BOARD_LINK_COLUMNS it is measured against COLUMNS"


def test_down_board_red_and_down_text_only_where_room():
    fresh_tmpdir()
    world = World(up=False)
    line = "Context " + "█" * 10 + " 12%"
    base = width(line)
    out = run_filter(world, line + "\n", COLUMNS=str(base + 20))
    assert lines(out) == [line + " | board (down)"], lines(out)
    assert b"\x1b[31m" in out and b"\x1b[36m" not in out, "a board that is down is red, not cyan"
    short = run_filter(world, line + "\n", COLUMNS=str(base + 8 + 3))
    assert lines(short) == [line + " | board"], f"room for the link but not for ' (down)': {lines(short)}"
    assert b"\x1b[31m" in short
    up = World(up=True)
    assert b"(down)" not in run_filter(up, line + "\n", COLUMNS="200") and b"\x1b[36m" in run_filter(up, line + "\n", COLUMNS="200")


def test_columns_unset_link_after_context_no_width_check():
    fresh_tmpdir()
    world = World()
    long_line = "Context " + "█" * 400 + " 12%"
    got = lines(run_filter(world, long_line + "\n", COLUMNS=None))
    assert got == [long_line + " | board"], "no width known: no check, the link follows the context usage"
    assert lines(run_filter(world, long_line + "\n", COLUMNS="abc")) == [long_line + " | board"], "a width that is not a number is no width"


def test_other_status_line_link_at_end_of_first_line_with_room_never_wider():
    fresh_tmpdir()
    world = World()
    first, second = "a" * 40, "b" * 10
    got = lines(run_filter(world, f"{first}\n{second}\n", COLUMNS="45"))
    assert got == [first, second + " | board"], f"the first line has no room (40 + 8 > 45): the last line with room takes it: {got}"
    got = lines(run_filter(world, f"{first}\n{second}\n", COLUMNS="48"))
    assert got == [first + " | board", second], got
    got = lines(run_filter(world, f"{first}\n{'c' * 42}\n", COLUMNS="45"))
    assert got == [first, "c" * 42, "board"], f"no line has room: a line of its own: {got}"
    for columns in range(21, 90, 7):
        for out in lines(run_filter(world, f"{first}\n{second}\nxyz\n", COLUMNS=str(columns))):
            assert width(out) <= max(columns, width(first)), f"{columns}: {out!r}"


def test_board_started_during_session_shows_at_next_refresh():
    fresh_tmpdir()
    world = World(with_board=False)
    first = run_filter(world, HUD, COLUMNS="100")
    assert first == HUD.encode(), "no board yet: the status line as it came (and that answer is kept for a minute)"
    world.add_board()  # a server record and a board folder appear: `servers/` and `boards/` change
    second = run_filter(world, HUD, COLUMNS="100")
    assert b"| board" in strip(second.decode()).encode(), "the next refresh asks again and places the link"


def test_kept_answer_used_when_nothing_changed():
    fresh_tmpdir()
    world = World()
    stub = TMP / f"counting-{time.time_ns()}"
    shutil.copytree(STATUS, stub, ignore=shutil.ignore_patterns("__pycache__"))
    log = TMP / f"helper-runs-{time.time_ns()}.log"
    (stub / "board_link.py").write_text(
        "import json, os, sys\nopen(%r, 'a').write('x')\n"
        "sys.stdout.write(json.dumps({'state': '', 'url': %r, 'up': True}) + '\\n')\n" % (str(log), world.url))
    env = world.env(COLUMNS="100")
    for _ in range(3):
        done = subprocess.run([sys.executable, "-B", str(stub / "statusline_filter.py")], input=HUD.encode(), capture_output=True, env=env, cwd=str(TMP), timeout=30)
        assert b"| board" in strip(done.stdout.decode()).encode(), done.stdout
    assert log.read_text() == "x", f"the helper ran {len(log.read_text())} times in three refreshes; once a minute is the rule"
    cache = next(cache_dir_of(world).glob("*"))
    old = time.time() - 120
    os.utime(cache, (old, old))
    subprocess.run([sys.executable, "-B", str(stub / "statusline_filter.py")], input=HUD.encode(), capture_output=True, env=env, cwd=str(TMP), timeout=30)
    assert log.read_text() == "xx", "after a minute it asks again"


def cache_dir_of(world: World) -> Path:
    """Where the filter keeps the helper's answer (review of 15 Nov 2026, finding 3): in the plugin's data folder, which is under the
    user's own config folder, not in a temporary folder another local user could have created first."""
    return world.data / "statusline-cache"


PLANTED = {
    "an address of the right shape for somebody else's board": "http://localhost:1/planted",
    "another host": "http://evil.example/x",
    "an escape sequence in the address": "http://localhost:1/x\x1b]52;c;ZXZpbA==\x07",
    "a newline in the address": "http://localhost:1/a\nb",
}


def test_a_planted_cache_file_in_the_shared_temporary_folder_is_never_read():
    """Finding 3 (15 Nov 2026): on a shared /tmp another local user can create `board-link-statusline.<uid>` first and put an address in it;
    it used to go into the status line unchecked. The cache is no longer there, so a file planted there is not looked at."""
    fresh_tmpdir()
    world = World()
    start = str(world.project)
    name = hashlib.sha1(start.encode()).hexdigest()[:16]
    for label, url in PLANTED.items():
        for folder in (TMPDIR / f"board-link-statusline.{os.getuid()}", TMPDIR / "board-link-statusline"):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(json.dumps({"url": url, "up": True}))
        shown = run_filter(world, HUD, COLUMNS="100")
        assert url.encode() not in shown and b"planted" not in shown and b"evil" not in shown, f"{label}: a planted file was used: {shown!r}"
        assert world.url.encode() in shown, f"{label}: the real address is asked for and shown: {shown!r}"


def test_a_cached_address_is_checked_like_a_fresh_one_and_the_cache_is_in_the_data_folder():
    fresh_tmpdir()
    world = World()
    start = str(world.project)
    name = hashlib.sha1(start.encode()).hexdigest()[:16]
    shown = run_filter(world, HUD, COLUMNS="100")
    assert world.url.encode() in shown
    cache = cache_dir_of(world)
    assert (cache / name).is_file(), f"the answer is kept in the data folder: {sorted(str(p.relative_to(world.root)) for p in world.root.rglob('*') if p.is_file())[:12]}"
    assert not list(TMPDIR.glob("board-link-statusline*")), "nothing is kept in the temporary folder any more"
    for label, url in list(PLANTED.items())[1:]:  # the first has the right shape: a file in the user's own data folder is as trusted as the user
        (cache / name).write_text(json.dumps({"url": url, "up": True}))  # fresh: newer than any board change, so it would be used if it were believed
        shown = run_filter(world, HUD, COLUMNS="100")
        assert url.encode() not in shown and b"evil" not in shown and b"planted" not in shown, f"{label}: an address that fails the check was used: {shown!r}"
        assert world.url.encode() in shown, f"{label}: the helper is asked again and its address shown: {shown!r}"
    (cache / name).write_text(json.dumps({"url": world.url, "up": True}))
    assert world.url.encode() in run_filter(world, HUD, COLUMNS="100"), "a good kept answer is still used"


def test_seen_is_touched_only_when_the_link_is_placed_once_per_minute():
    fresh_tmpdir()
    world = World()
    seen = world.data / "seen"
    h = hashlib.sha1(str(world.project.resolve()).encode()).hexdigest()[:16]
    run_filter(world, HUD, COLUMNS="100")
    touched = seen / h
    assert touched.is_file(), f"seen/{h} (the first 16 hex of the sha1 of the start folder's path) was not touched: {list(seen.glob('*'))}"
    first = touched.stat().st_mtime
    os.utime(touched, (first - 20, first - 20))
    run_filter(world, HUD, COLUMNS="100")
    assert abs(touched.stat().st_mtime - (first - 20)) < 1, "touched again inside a minute: once per cache minute is the rule"
    os.utime(touched, (first - 200, first - 200))
    run_filter(world, HUD, COLUMNS="100")
    assert touched.stat().st_mtime > first - 5, "after a minute it is touched again"
    quiet = World(with_board=False)
    run_filter(quiet, HUD, COLUMNS="100")
    assert not (quiet.data / "seen").exists() or not list((quiet.data / "seen").glob("*")), "nothing placed, nothing touched"


def test_seen_is_touched_under_both_spellings_of_a_start_folder_that_is_a_symlink():
    fresh_tmpdir()
    world = World()
    link = world.root / "via-link"
    link.symlink_to(world.project)
    run_filter(world, HUD, COLUMNS="100", CLAUDE_PROJECT_DIR=str(link))
    names = {p.name for p in (world.data / "seen").glob("*")}
    wanted = {hashlib.sha1(str(p).encode()).hexdigest()[:16] for p in (link, world.project.resolve())}
    assert wanted <= names, f"as given and as resolved are both touched, so the reader finds it either way: {names} vs {wanted}"


def test_data_folder_follows_the_config_dir():
    fresh_tmpdir()
    world = World()
    run_filter(world, HUD, COLUMNS="100")
    assert (world.config / "plugins" / "data" / DATA_ID / "seen").is_dir(), "the plugin's data folder under CLAUDE_CONFIG_DIR"
    other = TMP / "explicit-data"
    run_filter(world, HUD, COLUMNS="100", CLAUDE_PLUGIN_DATA=str(other))
    assert (other / "seen").is_dir(), "CLAUDE_PLUGIN_DATA, when it is set, names the data folder"


# ── step 2: the launcher ────────────────────────────────────────────────────────────────────────────────────────────
def run_launcher(world: World, data: bytes) -> bytes:
    """The launcher as the status line runs it: from the data folder, where `add` copies it (never from the plugin's own folder)."""
    world.data.mkdir(parents=True, exist_ok=True)
    shutil.copy(need(LAUNCHER), world.data / MARKER)
    done = subprocess.run([sys.executable, "-I", "-S", "-B", str(world.data / MARKER)], input=data, capture_output=True, env=world.env(COLUMNS="100"),
                          cwd=str(TMP), timeout=30)
    assert done.returncode == 0, done.stderr[-200:]
    return done.stdout


def test_launcher_runs_the_installed_plugins_filter():
    fresh_tmpdir()
    world = World()
    out = run_launcher(world, HUD.encode())
    assert b"| board" in strip(out.decode()).encode() and world.url.encode() in out, out


def test_launcher_without_the_plugin_installed_prints_stdin_unchanged():
    fresh_tmpdir()
    odd = b"\xff\xfe odd\n" + HUD.encode()
    world = World()
    world.install(None)
    assert run_launcher(world, odd) == odd, "no record of the plugin"
    world.install(TMP / "no-such-folder")
    assert run_launcher(world, odd) == odd, "a record that points nowhere"
    (world.config / "plugins" / "installed_plugins.json").write_text("not json {")
    assert run_launcher(world, odd) == odd, "a record that is not JSON"
    (world.config / "plugins" / "installed_plugins.json").unlink()
    assert run_launcher(world, odd) == odd, "no record file at all"


def test_launcher_with_a_filter_that_fails_prints_stdin_unchanged():
    fresh_tmpdir()
    world = World()
    for body in ("import sys\nsys.exit(3)\n", "raise SystemExit('no')\n", "import sys\nsys.stdout.write('')\n", "import time\ntime.sleep(60)\n"):
        broken = TMP / f"broken-{time.time_ns()}"
        (broken / "status-board").mkdir(parents=True)
        (broken / "status-board" / "statusline_filter.py").write_text(body)
        world.install(broken)
        started = time.time()
        assert run_launcher(world, HUD.encode()) == HUD.encode(), body
        assert time.time() - started < 15, "a hung filter must not hang the status line"


def test_launcher_gives_the_filter_its_own_folder_as_the_data_folder():
    """A status line command is not given CLAUDE_PLUGIN_DATA (SPIKE.md section 5); the launcher is copied into the data folder."""
    fresh_tmpdir()
    world = World()
    elsewhere = TMP / f"data-elsewhere-{time.time_ns()}"
    elsewhere.mkdir()
    copy = elsewhere / MARKER
    shutil.copy(need(LAUNCHER), copy)
    done = subprocess.run([sys.executable, "-I", "-S", "-B", str(copy)], input=HUD.encode(), capture_output=True, env=world.env(COLUMNS="100"),
                          cwd=str(TMP), timeout=30)
    assert b"| board" in strip(done.stdout.decode()).encode(), done.stdout
    assert (elsewhere / "seen").is_dir(), "seen/ is under the launcher's own folder"
    assert not (world.data / "seen").exists(), "and not under the config folder's default"


def test_launcher_is_small():
    assert len(need(LAUNCHER).read_text().splitlines()) <= 45, "it is copied into every user's data folder: keep it a few lines"


# ── step 2: the whole command, around a stand-in for claude-hud ─────────────────────────────────────────────────────
FAKE_HUD = '''import os, sys
def width(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
raw = os.environ.get("COLUMNS", "")
cols = int(raw) if raw.isdigit() and int(raw) > 0 else 10 ** 6
bar = max(1, min(10, cols - 12))
segments = ["[Sonnet]", "Context \\x1b[32m" + "█" * bar + "\\x1b[0m 12%", "shop-app git:(main)", "Usage ██░░ 20% (1h)", "Tools 3"]
out, cur = [], ""
for seg in segments:  # fills the first line to the width it was given, then wraps (as claude-hud 0.15.3 does)
    cand = cur + (" │ " if cur else "") + seg
    if not cur or width(cand) <= cols:
        cur = cand
    else:
        out.append(cur)
        cur = seg
out.append(cur)
left, right = "Mode: plan", "12:30"  # one row padded to exactly the width: a right-aligned row
out.append(left + " " * max(1, cols - width(left) - width(right)) + right)
sys.stdout.write("\\n".join(out) + "\\n")
'''


def hud_command() -> tuple[str, Path]:
    """A status line command that is claude-hud's for the set-up (its path holds `claude-hud`) and prints the stand-in's lines."""
    folder = TMP / "claude-hud"
    folder.mkdir(exist_ok=True)
    script = folder / "fake_hud.py"
    script.write_text(FAKE_HUD)
    return f"'{sys.executable}' '{script}'", script


def composed(world: World, command: str, *extra_args: str) -> tuple[str, dict]:
    """settings.json with this status line command, `add` run on it: the command the user would then have."""
    cfg = Cfg(world, json.dumps({"statusLine": {"type": "command", "command": command}}, indent=2) + "\n")
    proc, said = cfg.run("add", *extra_args)
    assert proc.returncode == 0 and said and said.get("written"), f"add failed: {proc.stdout[:300]} {proc.stderr[-300:]}"
    world.config = cfg.dir  # what runs the command next sees the data folder `add` copied the launcher into
    return json.loads(cfg.settings.read_text())["statusLine"]["command"], said


def run_bash(command: str, world: World, columns: str | None, bash: str = "/bin/bash", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = world.env(COLUMNS=columns, **(extra_env or {}))
    return subprocess.run([bash, "-c", command], capture_output=True, text=True, env=env, cwd=str(TMP), timeout=60)


def test_claude_hud_link_right_after_context_at_every_width_and_no_line_wider():
    fresh_tmpdir()
    world = World()
    command, _ = composed(world, hud_command()[0])
    assert "claude-hud" in command, "the command still holds `claude-hud`, which is how `hud doctor` finds it"
    failures = []
    for columns in range(21, 221):
        done = run_bash(command, world, str(columns))
        out = done.stdout.rstrip("\n").split("\n")
        shown = [strip(line) for line in out]
        context = [line for line in shown if "Context" in line]
        if len(context) != 1 or not re.search(r"Context █+ 12% \| board", context[0]):
            failures.append(f"{columns}: link not right after the context usage: {shown}")
            continue
        wide = [line for line in out if width(line) > columns]
        if wide:
            failures.append(f"{columns}: a line wider than the window: {[strip(w) for w in wide]}")
    assert not failures, f"{len(failures)} of 200 widths: " + " ;; ".join(failures[:2])


def find_claude_hud() -> Path | None:
    """claude-hud's dist/index.js: the one named by `--claude-hud`, else the newest under the account's own home (from the account
    database, not from HOME: a run with HOME in a temporary folder, as the briefs require, found nothing and skipped the test; review of
    15 Nov 2026, finding 6), else under HOME. Read only: nothing is written there, and it is run with a temporary config folder."""
    if ARGS.claude_hud:
        return Path(ARGS.claude_hud)
    import pwd
    homes = [Path(pwd.getpwuid(os.getuid()).pw_dir), REAL_HOME]
    found = sorted({p for home in homes for p in glob.glob(str(home / ".claude" / "plugins" / "cache" / "*" / "claude-hud" / "*" / "dist" / "index.js"))},
                   key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[./-]", p.split("claude-hud/")[-2])])
    return Path(found[-1]) if found else None


def test_claude_hud_link_right_after_context_with_the_real_claude_hud():
    node, dist = shutil.which("node"), find_claude_hud()
    if node is None or dist is None or not dist.is_file():
        raise Skip("NOT RUN: Node or claude-hud's dist/index.js is not installed here")
    fresh_tmpdir()
    world = World()
    recorded = json.dumps({"model": {"id": "claude-sonnet-x", "display_name": "Sonnet"}, "version": "2.1.0", "cwd": str(world.project),
                           "context_window": {"context_window_size": 200000, "used_percentage": 12,
                                              "current_usage": {"input_tokens": 24000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
    stdin_file = TMP / "recorded-stdin.json"
    stdin_file.write_text(recorded)
    command, _ = composed(world, f"'{node}' '{dist}' < '{stdin_file}'")
    assert "claude-hud" in command
    failures = []
    for columns in (30, 40, 60, 80, 100, 120, 160, 220):
        done = run_bash(command, world, str(columns))
        out = done.stdout.rstrip("\n").split("\n")
        hits = [strip(line) for line in out if "Context" in strip(line)]
        if len(hits) != 1 or not re.search(r"12%(?: \(.*?\))? \| board", hits[0]):
            failures.append(f"{columns}: {[strip(l) for l in out]}")
    assert not failures, "; ".join(failures[:2])


def test_the_real_claude_hud_is_found_when_this_run_has_a_temporary_home():
    """Review of 15 Nov 2026, finding 6: run with HOME in a temporary folder (as the briefs for this work require), the test above looked
    for claude-hud under that folder and was skipped. The real one is looked for where the account's own home is, whatever HOME says;
    `--claude-hud <dist/index.js>` still names it directly. This test runs the test above in a child under a temporary HOME and wants a PASS."""
    import pwd
    account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not glob.glob(str(account / ".claude" / "plugins" / "cache" / "*" / "claude-hud" / "*" / "dist" / "index.js")) or shutil.which("node") is None:
        raise Skip("NOT RUN: claude-hud (or Node) is not installed under the account's home")
    name = "test_claude_hud_link_right_after_context_with_the_real_claude_hud"
    for label, extra in (("found by itself", []), ("named with --claude-hud", ["--claude-hud", str(find_claude_hud() or glob.glob(str(account / ".claude" / "plugins" / "cache" / "*" / "claude-hud" / "*" / "dist" / "index.js"))[-1])])):
        home = Path(tempfile.mkdtemp(dir=TMP, prefix="temp-home-"))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_DATA")}
        env["HOME"] = str(home)
        done = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--plugin", str(PLUGIN), "--only", "with_the_real_claude_hud", *extra],
                              capture_output=True, text=True, env=env, cwd=str(TMP), timeout=300)
        assert f"PASS  {name}" in done.stdout and done.returncode == 0, f"{label}, under a temporary HOME: {done.stdout[-400:]} {done.stderr[-200:]}"
        assert "SKIP" not in done.stdout, f"{label}: skipped under a temporary HOME: {done.stdout[-300:]}"


def test_columns_20_or_less_not_reduced_link_where_it_fits():
    fresh_tmpdir()
    world = World()
    command, _ = composed(world, "printf 'claude-hud COLUMNS=%s\\n' \"${COLUMNS-unset}\"")
    done = run_bash(command, world, "20")
    got = [strip(line) for line in done.stdout.rstrip("\n").split("\n")]
    assert got[0] == "claude-hud COLUMNS=20", f"20 or less is not reduced: {got}"
    assert got[-1] == "board", f"the line has no room (21 + 8 > 20): the link goes on a line of its own: {got}"


def test_orig_sees_columns_8_smaller_only_in_the_claude_hud_form_and_caller_is_unchanged():
    fresh_tmpdir()
    world = World()
    show = "printf '%s COLUMNS=%s\\n' LABEL \"${COLUMNS-unset}\""
    reducing, _ = composed(world, show.replace("LABEL", "claude-hud"))
    plain, _ = composed(world, show.replace("LABEL", "other"))
    assert strip(run_bash(reducing, world, "120").stdout).startswith("claude-hud COLUMNS=112")
    assert strip(run_bash(plain, world, "120").stdout).startswith("other COLUMNS=120"), "any other status line is not reduced"
    assert strip(run_bash(reducing, world, "21").stdout).startswith("claude-hud COLUMNS=13")
    for value in ("20", "5", None, "abc", ""):
        got = strip(run_bash(reducing, world, value).stdout).split("\n")[0].removesuffix(" | board")  # the link may follow, where it fits
        assert got == f"claude-hud COLUMNS={value if value else 'unset' if value is None else ''}", (value, got)
    # The command now ends with `exit $rc`, so what runs after it is an EXIT trap: it sees the shell's own COLUMNS at the end.
    after = run_bash("trap 'echo AFTER=$COLUMNS' EXIT; " + reducing, world, "120")
    assert "AFTER=120" in after.stdout, "the export is inside the subshell: the caller's COLUMNS is unchanged"


def test_command_parses_and_runs_under_every_bash_here():
    fresh_tmpdir()
    world = World()
    command, _ = composed(world, hud_command()[0])
    plain, _ = composed(world, "printf 'one line\\n'")
    bashes = {Path(p).resolve(): p for p in ("/bin/bash", shutil.which("bash", path=os.environ.get("PATH", "")) or "/bin/bash", *ARGS.bash) if Path(p).exists()}
    assert bashes, "no bash found"
    for resolved, bash in bashes.items():
        for cmd in (command, plain):
            syntax = subprocess.run([bash, "-n", "-c", cmd], capture_output=True, text=True)
            assert syntax.returncode == 0, f"{bash} -n: {syntax.stderr[:200]}"
            done = run_bash(cmd, world, "100", bash=bash)
            assert done.returncode == 0 and "board" in strip(done.stdout), f"{bash}: {done.stdout[:200]} {done.stderr[:200]}"


def test_no_python_prints_what_the_status_line_printed():
    fresh_tmpdir()
    world = World()
    command, _ = composed(world, "printf '%s\\n' 'claude-hud stand-in line'")
    empty = TMP / "empty-path"
    empty.mkdir(exist_ok=True)
    done = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, env=world.env(COLUMNS="100", PATH=str(empty)), cwd=str(TMP), timeout=30)
    assert done.stdout == "claude-hud stand-in line\n", f"python3 missing: ORIG's output unchanged: {done.stdout!r} {done.stderr[:200]!r}"


def test_no_machine_path_in_the_shipped_files():
    forbidden = ("/opt/homebrew", "/Users/", "/home/", str(REAL_HOME))  # a machine's own paths; the home of whoever runs this test
    for path in (FILTER, LAUNCHER, SETUP):
        text = need(path).read_text()
        for word in forbidden:
            assert word not in text, f"{path.name} names {word!r}"


# ── step 3: the set-up script ───────────────────────────────────────────────────────────────────────────────────────
class Cfg:
    """A config folder with a settings.json fixture, and the set-up script run on it."""

    _n = 0

    def __init__(self, world: World, text: str | bytes | None, symlink: bool = False) -> None:
        Cfg._n += 1
        self.world = world
        self.dir = world.root / f"cfg{Cfg._n}"
        self.dir.mkdir()
        (self.dir / "plugins").mkdir()
        shutil.copy(world.config / "plugins" / "installed_plugins.json", self.dir / "plugins" / "installed_plugins.json")
        self.settings = self.dir / "settings.json"
        self.real = self.settings
        if text is not None:
            data = text.encode() if isinstance(text, str) else text
            if symlink:
                self.real = world.root / f"dotfiles{Cfg._n}" / "settings.json"
                self.real.parent.mkdir()
                self.real.write_bytes(data)
                self.real.chmod(0o640)
                self.settings.symlink_to(self.real)
            else:
                self.settings.write_bytes(data)
                self.settings.chmod(0o640)

    @property
    def data(self) -> Path:
        return self.dir / "plugins" / "data" / DATA_ID

    def raw(self) -> bytes:
        return self.real.read_bytes()

    def run(self, *args: str, env: dict | None = None, umask: int | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        environment = self.world.env(CLAUDE_CONFIG_DIR=str(self.dir))
        environment.update(env or {})
        done = subprocess.run([sys.executable, "-B", str(need(SETUP)), "--config-dir", str(self.dir), *args], capture_output=True, text=True,
                              env=environment, cwd=str(TMP), timeout=60, preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)
        out = done.stdout.strip()
        try:
            said = json.loads(out) if out else None
        except ValueError:
            said = None
        return done, said

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*") if p.is_file() and p.name != "installed_plugins.json")


def settings_with(command: str | None, indent: int | str | None = 2, extra: dict | None = None, **status_extra) -> str:
    data: dict = dict(extra or {})
    if command is not None:
        data["statusLine"] = {"type": "command", "command": command, **status_extra}
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


CLAUDE_HUD_POSIX = "'/node/bin/node' '/home/u/.claude/plugins/claude-hud/statusline.mjs'"
CLAUDE_HUD_GITBASH = 'exec "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/claude-hud/statusline.cmd"'
OWNER_STYLE = ("bash -c 'cols=${COLUMNS:-}; case \"$cols\" in \"\"|*[!0-9]*) cols=120;; esac; export COLUMNS=$(( cols > 4 ? cols - 4 : 1 )); "
               "plugin_dir=$(ls -d \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"/plugins/cache/*/claude-hud/*/ 2>/dev/null | tail -1); "
               "out=$(COLUMNS=$(( COLUMNS > 20 ? COLUMNS - 8 : COLUMNS )) \"/node/bin/node\" \"${plugin_dir}dist/index.js\"); printf \"%s\\n\" \"$out\"'")
OTHER = "echo hello from my own status line"
HAND_MADE = ("bash -c 'out=$(echo claude-hud line); printf \"%s\\n\" \"$out\" | python3 -I -S -B \"$HOME/.claude/hud-board-link.py\" 2>/dev/null "
             "|| printf \"%s\\n\" \"$out\"'")


def test_add_then_remove_gives_the_file_back_byte_for_byte():
    fresh_tmpdir()
    world = World()
    base = {"theme": "dark", "env": {"A": "é\"q\\"}, "permissions": {"allow": ["Bash(ls:*)"]}}
    cases = {
        "claude-hud posix form, 2 spaces": settings_with(CLAUDE_HUD_POSIX, 2, base, refreshInterval=5),
        "claude-hud Git Bash exec form, 4 spaces": settings_with(CLAUDE_HUD_GITBASH, 4, base),
        "owner-style bash -c with quotes, tabs": settings_with(OWNER_STYLE, "\t", base, refreshInterval=5, padding=0),
        "another status line, compact": json.dumps({"statusLine": {"type": "command", "command": OTHER}, "a": [1, 2]}),
        "no trailing newline, CRLF": settings_with(CLAUDE_HUD_POSIX, 2, base).replace("\n", "\r\n").rstrip("\r\n"),
        "escapes the writer chose": ('{"statusLine": {"type": "command", "command": "echo claude-hud \\u0027x\\u0027 \\/ \\u00e9"},\n'
                                      ' "k": "v"}\n'),
        "byte order mark": "\ufeff" + settings_with(CLAUDE_HUD_POSIX, 2, base),
        "key order and duplicates of other keys": '{"z": 1, "statusLine": {"command": "echo claude-hud x", "type": "command"}, "a": 2}\n',
    }
    for label, text in cases.items():
        for symlink in (False, True):
            cfg = Cfg(world, text.encode("utf-8"), symlink=symlink)
            original = cfg.raw()
            proc, added = cfg.run("add")
            assert proc.returncode == 0 and added and added.get("written") is True, f"{label}: add: {proc.stdout[:200]} {proc.stderr[-200:]}"
            assert cfg.raw() != original and MARKER in json.loads(cfg.raw().decode("utf-8").lstrip("\ufeff"))["statusLine"]["command"], label
            proc, removed = cfg.run("remove")
            assert proc.returncode == 0 and removed and removed.get("written") is True, f"{label}: remove: {proc.stdout[:200]} {proc.stderr[-200:]}"
            assert cfg.raw() == original, f"{label} (symlink={symlink}): the file is not back byte for byte"


def test_add_chooses_the_reducing_form_exactly_when_orig_has_claude_hud_and_remove_restores_either():
    fresh_tmpdir()
    world = World()
    reducing, said_r = composed(world, CLAUDE_HUD_POSIX)
    plain, said_p = composed(world, OTHER)
    assert reducing.startswith('w=${COLUMNS:-}; out=$([ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8)); ' + CLAUDE_HUD_POSIX + ");"), reducing
    assert said_r["old"] == CLAUDE_HUD_POSIX and said_r["new"] == reducing and said_p["old"] == OTHER and said_p["new"] == plain, (said_r, said_p)
    assert plain.startswith("w=${COLUMNS:-}; out=$(" + OTHER + ");") and "export COLUMNS" not in plain, plain
    for command in (reducing, plain):
        assert command.endswith('2>/dev/null || printf \'%s\\n\' "$out"; fi; exit $rc'), command
        assert f"/plugins/data/{DATA_ID}/{MARKER}" in command and 'BOARD_LINK_COLUMNS="$w"' in command and "python3 -I -S -B" in command
        assert '"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/data/' in command, "no versioned plugin folder, no machine path in the command"
        assert "/cache/" not in command, "never a versioned plugin folder"


def test_add_refuses_and_writes_nothing():
    fresh_tmpdir()
    world = World()
    bad_syntax = "case $x in a) echo a;; esac"
    # A bare `case` with its `)` patterns inside `$( )` is a syntax error for the bash 3.2 that macOS ships; a current bash
    # accepts it. The refusal is tested where this machine's /bin/bash rejects it, and is said to be not run where it does not.
    rejects = subprocess.run(["/bin/bash", "-n", "-c", f"out=$({bad_syntax}); echo"], capture_output=True).returncode != 0
    cases = {
        **({"a case with its ) patterns, which this /bin/bash cannot parse inside $( )": (settings_with(bad_syntax), "syntax")} if rejects else {}),
        "no statusLine at all": (settings_with(None, 2, {"theme": "dark"}), "no-status-line"),
        "a statusLine that is not a command": (json.dumps({"statusLine": {"type": "text", "text": "hi"}}), "no-status-line"),
        "a newline in the command": (settings_with("echo one\necho two"), "newline"),
        "a # in the command": (settings_with("echo claude-hud # a comment"), "comment"),
        "PowerShell form": (settings_with('C:\\Windows\\System32\\cmd.exe /d /s /c ""C:\\node.exe" "C:\\hud\\statusline.mjs""'), "powershell"),
        "settings.json is not JSON": ("{ not json", "settings-unreadable"),
        "settings.json is a list": ("[1, 2]", "settings-unreadable"),
        "a hand-made filter, without the flag": (settings_with(HAND_MADE), "hand-made"),
    }
    for label, (text, code) in cases.items():
        cfg = Cfg(world, text)
        before = cfg.raw()
        proc, said = cfg.run("add")
        assert proc.returncode == 2 and said and said.get("code") == code, f"{label}: {proc.returncode} {said} {proc.stderr[-200:]}"
        assert isinstance(said.get("refused"), str) and len(said["refused"]) > 10, f"{label}: `refused` is the reason, in a sentence"
        assert set(said) == {"refused", "code"}, said
        assert cfg.raw() == before and cfg.files() == ["settings.json"], f"{label}: nothing was written: {cfg.files()}"
        dry, said_dry = cfg.run("add", "--dry-run")
        assert dry.returncode == 2 and said_dry == said, f"{label}: a refusal is reported the same way with --dry-run, so the skill learns it before it asks"
    missing = Cfg(world, None)
    proc, said = missing.run("add")
    assert proc.returncode == 2 and said["code"] == "no-status-line" and missing.files() == [], "no settings.json: not created"


def test_add_when_already_wrapped_is_a_no_op():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX))
    cfg.run("add")
    wrapped = cfg.raw()
    files = cfg.files()
    proc, said = cfg.run("add")
    assert proc.returncode == 0 and said == {"written": False, "already": True}, (proc.stdout, said)
    assert cfg.raw() == wrapped and cfg.files() == files, "the second add changed nothing and wrote no second backup"
    assert cfg.raw().count(MARKER.encode()) == 1


def test_add_with_a_hand_made_filter_needs_the_flag_and_replaces_only_its_path():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(HAND_MADE, 2, {"k": 1}))
    original = cfg.raw()
    proc, said = cfg.run("add", "--replace-hand-made")
    assert proc.returncode == 0 and said["written"] is True, (proc.stdout, proc.stderr[-200:])
    command = json.loads(cfg.raw())["statusLine"]["command"]
    assert "hud-board-link.py" not in command and f"/plugins/data/{DATA_ID}/{MARKER}" in command, command
    assert command.count("out=$(echo claude-hud line)") == 1 and "2>/dev/null || printf" in command, "the rest of the user's command is as it was"
    assert subprocess.run(["/bin/bash", "-n", "-c", command]).returncode == 0
    proc, _ = cfg.run("remove")
    assert proc.returncode == 0 and cfg.raw() == original, "the user's command back byte for byte"


def test_dry_run_shows_old_and_new_and_writes_nothing():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, {"theme": "dark"}))
    before = cfg.raw()
    proc, said = cfg.run("add", "--dry-run")
    assert proc.returncode == 0 and set(said) == {"old", "new"}, said
    assert said["old"] == CLAUDE_HUD_POSIX and MARKER in said["new"], said
    assert cfg.raw() == before and cfg.files() == ["settings.json"], "a dry run writes nothing, not even the launcher"
    proc, again = cfg.run("add")
    assert again["new"] == said["new"], "what the dry run showed is what add writes"


def test_other_keys_are_untouched_and_a_backup_of_the_original_is_written():
    fresh_tmpdir()
    world = World()
    base = {"theme": "dark", "permissions": {"allow": ["Bash(ls:*)"], "deny": []}, "hooks": {"Stop": [{"hooks": []}]}}
    text = settings_with(CLAUDE_HUD_POSIX, 2, base, refreshInterval=5, padding=1)
    cfg = Cfg(world, text)
    original = cfg.raw()
    proc, said = cfg.run("add")
    after = json.loads(cfg.raw())
    before = json.loads(original)
    assert {k: v for k, v in after.items() if k != "statusLine"} == {k: v for k, v in before.items() if k != "statusLine"}
    assert after["statusLine"]["refreshInterval"] == 5 and after["statusLine"]["padding"] == 1 and after["statusLine"]["type"] == "command"
    backups = sorted(cfg.dir.glob("settings.json.bak.*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original, f"backups: {backups}"
    assert said["backup"] == str(backups[0]) and set(said) == {"old", "new", "backup", "written"}, said


def test_symlinked_settings_stays_a_symlink_and_keeps_its_permissions():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX), symlink=True)
    cfg.run("add")
    assert cfg.settings.is_symlink() and cfg.settings.resolve() == cfg.real.resolve(), "the link is still the link"
    assert MARKER in cfg.real.read_text(), "the real file behind it was written"
    assert (cfg.real.stat().st_mode & 0o777) == 0o640, oct(cfg.real.stat().st_mode)
    assert not list(cfg.real.parent.glob("*.tmp")) and not list(cfg.dir.glob("*.tmp")), "no temporary file left"
    backups = list(cfg.real.parent.glob("settings.json.bak.*"))
    assert len(backups) == 1 and not list(cfg.dir.glob("settings.json.bak.*")), f"the backup lies beside the real file, not beside the link: {backups}"
    assert (backups[0].stat().st_mode & 0o777) == 0o640, oct(backups[0].stat().st_mode)


def test_add_copies_the_launcher_into_the_data_folder_before_writing_settings():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX))
    cfg.run("add")
    copy = cfg.data / MARKER
    assert copy.is_file() and copy.read_bytes() == LAUNCHER.read_bytes(), "the launcher as the plugin ships it"
    assert (copy.stat().st_mode & 0o777) in (0o644, 0o755, 0o600, 0o700)
    cfg.run("remove")
    assert not copy.exists(), "remove takes the copy away with the wrapper"


def test_two_adds_at_once_give_one_wrapper():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, {"k": 1}))
    start = TMP / f"go-{time.time_ns()}"
    code = ("import sys, time, subprocess\nwhile not __import__('os').path.exists(%r): time.sleep(0.005)\n"
            "sys.exit(subprocess.run(sys.argv[1:]).returncode)\n") % str(start)
    argv = [sys.executable, "-B", str(need(SETUP)), "--config-dir", str(cfg.dir), "add"]
    env = world.env(CLAUDE_CONFIG_DIR=str(cfg.dir))
    procs = [subprocess.Popen([sys.executable, "-c", code, *argv], stdout=subprocess.PIPE, text=True, env=env) for _ in range(4)]
    start.write_text("go")
    results = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert sum(1 for r in results if r.get("written") is True) == 1 and sum(1 for r in results if r.get("already") is True) == 3, results
    text = cfg.raw().decode()
    assert text.count(MARKER) == 1 and json.loads(text)["statusLine"]["command"].count(MARKER) == 1, "one wrapper, never two"
    assert len(list(cfg.dir.glob("settings.json.bak.*"))) == 1, "and one backup"


def test_remove_restores_from_the_known_prefix_and_suffix_when_the_record_is_gone():
    fresh_tmpdir()
    world = World()
    for orig in (CLAUDE_HUD_POSIX, OTHER):
        cfg = Cfg(world, settings_with(orig))
        original = cfg.raw()
        cfg.run("add")
        for leftover in cfg.data.glob("statusline-before.json"):
            leftover.unlink()
        proc, said = cfg.run("remove")
        assert proc.returncode == 0 and said["written"] is True and said["new"] == orig, (proc.stdout, proc.stderr[-200:])
        assert json.loads(cfg.raw())["statusLine"]["command"] == orig, "ORIG recovered from between the prefix and the suffix"


def test_remove_refuses_when_the_command_is_no_longer_ours_and_writes_nothing():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX))
    cfg.run("add")
    ours = json.loads(cfg.raw())["statusLine"]["command"]
    edited = Cfg(world, settings_with(ours.replace('2>/dev/null ||', '2>/dev/null && true ||', 1)))
    before = edited.raw()
    proc, said = edited.run("remove")  # the marker is there, but the wrapper is not the one `add` writes
    assert proc.returncode == 2 and said["code"] == "not-ours" and said["refused"], (proc.stdout, said)
    assert edited.raw() == before, "nothing written"
    rewritten = json.dumps({"statusLine": {"type": "command", "command": "'/node' '/hud/statusline.mjs'"}}, indent=2) + "\n"
    cfg.real.write_text(rewritten)  # `hud install` rewrote it: there is nothing of ours in the command
    before = cfg.raw()
    proc, said = cfg.run("remove")
    assert proc.returncode == 0 and said == {"written": False, "already": True}, (proc.stdout, said)
    assert cfg.raw() == before, "nothing written"
    cfg2 = Cfg(world, settings_with(OTHER))
    proc, said = cfg2.run("remove")
    assert proc.returncode == 0 and said == {"written": False, "already": True}, (proc.stdout, said)
    assert cfg2.files() == ["settings.json"], "and nothing is created"
    dry, said_dry = cfg.run("remove", "--dry-run")
    assert dry.returncode == 0 and said_dry == {"written": False, "already": True}


def test_remove_after_other_edits_restores_only_the_command_literal():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, {"theme": "dark"}))
    cfg.run("add")
    edited = cfg.raw().decode().replace('"theme": "dark"', '"theme": "light", "model": "x"')
    cfg.real.write_text(edited)  # the user changed other things in between
    proc, said = cfg.run("remove")
    assert proc.returncode == 0, (proc.stdout, proc.stderr[-300:])
    after = json.loads(cfg.raw())
    assert after["statusLine"]["command"] == CLAUDE_HUD_POSIX and after["theme"] == "light" and after["model"] == "x"


def test_status_reports_what_add_would_do_and_why_not():
    fresh_tmpdir()
    world = World()
    cases = {
        "none": settings_with(None),
        "claude-hud": settings_with(CLAUDE_HUD_POSIX),
        "other": settings_with(OTHER),
        "hand-made": settings_with(HAND_MADE),
        "powershell": settings_with("C:\\x\\cmd.exe /d /s /c \"x\""),
    }
    got = {}
    for label, text in cases.items():
        cfg = Cfg(world, text)
        before = cfg.raw()
        proc, said = cfg.run("status")
        assert proc.returncode == 0 and said, (label, proc.stdout, proc.stderr[-200:])
        assert cfg.raw() == before and cfg.files() == ["settings.json"], "status reads and writes nothing"
        got[label] = said
    keys = {"has_status_line", "is_claude_hud", "wrapped", "hand_made_filter", "claude_hud_installed", "recorded"}
    assert all(set(said) == keys for said in got.values()), {k: sorted(v) for k, v in got.items()}
    assert got["none"]["has_status_line"] is False and got["none"]["is_claude_hud"] is False and got["none"]["recorded"] is None
    assert got["claude-hud"]["has_status_line"] is True and got["claude-hud"]["is_claude_hud"] is True and got["claude-hud"]["wrapped"] is False
    assert got["other"]["has_status_line"] is True and got["other"]["is_claude_hud"] is False and got["other"]["hand_made_filter"] is False
    assert got["hand-made"]["hand_made_filter"] is True and got["hand-made"]["wrapped"] is False
    assert got["powershell"]["has_status_line"] is True
    wrapped = Cfg(world, settings_with(CLAUDE_HUD_POSIX))
    wrapped.run("add")
    status = wrapped.run("status")[1]
    assert status["wrapped"] is True and status["is_claude_hud"] is True
    assert status["claude_hud_installed"] is False
    unreadable = Cfg(world, "{ nope")
    proc, said = unreadable.run("status")
    assert proc.returncode == 0 and said["has_status_line"] is False, "status only reports what it can see"
    (wrapped.dir / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {"claude-hud@claude-hud": [{"installPath": "/x"}], PLUGIN_KEY: []}}))
    assert wrapped.run("status")[1]["claude_hud_installed"] is True, "read from Claude Code's own install record"


def test_an_empty_or_blank_settings_file_is_no_settings_for_status_add_and_remove():
    """Review of 15 Nov 2026, finding 4: `t[i]` on an empty text raised IndexError, and every command failed with exit 1, `status` too."""
    fresh_tmpdir()
    world = World()
    for label, text in (("empty", b""), ("a newline", b"\n"), ("spaces and a newline", b"  \n"), ("tabs and CRLF", b"\t\r\n\r\n"), ("a byte order mark alone", "﻿".encode()),
                        ("a byte order mark and blanks", "﻿ \n".encode())):
        cfg = Cfg(world, text)
        proc, said = cfg.run("status")
        assert proc.returncode == 0 and said and said["has_status_line"] is False and said["wrapped"] is False, f"{label}: status: {proc.returncode} {proc.stdout[:200]} {proc.stderr[-200:]}"
        for args in (("add",), ("add", "--dry-run")):
            proc, said = cfg.run(*args)
            assert proc.returncode == 2 and said and said.get("code") == "no-status-line", f"{label}: {args}: {proc.returncode} {proc.stdout[:200]} {proc.stderr[-200:]}"
        for args in (("remove",), ("remove", "--dry-run")):
            proc, said = cfg.run(*args)
            assert proc.returncode == 0 and said == {"written": False, "already": True}, f"{label}: {args}: {proc.returncode} {proc.stdout[:200]} {proc.stderr[-200:]}"
        assert cfg.raw() == text and cfg.files() == ["settings.json"], f"{label}: nothing was written"


def test_a_cut_off_settings_file_is_a_refusal_with_one_line_of_json():
    fresh_tmpdir()
    world = World()
    for label, text in (("an open brace", "{"), ("a key without a value", '{"statusLine"'), ("a colon without a value", '{"statusLine":'), ("an open string", '{"a": "b'),
                        ("an open array", '{"a": ['), ("a comma at the end", '{"a": 1,')):
        cfg = Cfg(world, text)
        for args in (("status",), ("add",), ("add", "--dry-run"), ("remove",)):
            proc, said = cfg.run(*args)
            assert len(proc.stdout.strip().split("\n")) == 1 and said, f"{label} {args}: exactly one line of JSON: {proc.stdout[:200]!r} {proc.stderr[-200:]}"
            if args[0] == "status":
                assert proc.returncode == 0 and said["has_status_line"] is False, f"{label}: {said}"
            else:
                assert proc.returncode == 2 and said.get("code") == "settings-unreadable", f"{label} {args}: {proc.returncode} {said}"
        assert cfg.raw() == text.encode() and cfg.files() == ["settings.json"], f"{label}: nothing was written"


def test_a_malformed_command_line_prints_one_line_of_json_with_error_and_exits_1():
    """INTERFACE.md: exactly one line of JSON for every command, `1` with `"error"` on anything unexpected (argparse printed its usage on
    stderr and exited 2 with nothing on stdout)."""
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(OTHER))
    before = cfg.raw()
    bad = (("frobnicate",), ("add", "--bogus"), ("status", "extra"), ("record", "--answer"), ("remove", "--replace-hand-made"), ("--config-dir",), ())
    for args in bad:
        done = subprocess.run([sys.executable, "-B", str(need(SETUP)), "--config-dir", str(cfg.dir), *args] if args != ("--config-dir",) and args != () else
                              [sys.executable, "-B", str(need(SETUP)), *args], capture_output=True, text=True, env=world.env(CLAUDE_CONFIG_DIR=str(cfg.dir)), cwd=str(TMP), timeout=30)
        lines_out = done.stdout.strip().split("\n")
        try:
            said = json.loads(done.stdout)
        except ValueError:
            said = None
        assert done.returncode == 1 and len(lines_out) == 1 and isinstance(said, dict) and isinstance(said.get("error"), str) and said["error"], \
            f"{args}: exit {done.returncode}, stdout {done.stdout[:200]!r}, stderr {done.stderr[-120:]!r}"
        assert set(said) == {"error"}, f"{args}: {said}"
    assert cfg.raw() == before and cfg.files() == ["settings.json"], "nothing was written"


# ── the mode of what the set-up script writes (the security review of 15 Nov 2026, on the backup of settings.json) ──────────────────
def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o7777


# `statusline_setup.py` run in-process, so that what it asks the operating system for can be seen: every file it creates (the flags
# and the mode it asks for), every chmod by name. FIXED_STAMP fixes the time the backup's name is made of, so that a test can plant
# something at exactly that name.
SPY_DRIVER = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import statusline_setup as S
stamp = os.environ.get("FIXED_STAMP")
if stamp:
    real_strftime = time.strftime
    time.strftime = lambda fmt, *a: stamp if fmt == "%Y%m%dT%H%M%S" else real_strftime(fmt, *a)
log = []
real_open, real_chmod = os.open, os.chmod
def spy_open(path, flags, mode=0o777, **kw):
    if flags & os.O_CREAT:
        log.append(["create", str(path), bool(flags & os.O_EXCL), mode])
    return real_open(path, flags, mode, **kw)
def spy_chmod(path, mode, **kw):
    log.append(["chmod", str(path), mode])
    return real_chmod(path, mode, **kw)
os.open, os.chmod = spy_open, spy_chmod
sys.argv = ["statusline_setup.py"] + sys.argv[2:]
code = S.main()
open(os.environ["SPY_LOG"], "w").write(json.dumps(log))
sys.exit(code)
'''


def run_spied(cfg: "Cfg", *args: str, umask: int = 0o000, stamp: str | None = None) -> tuple[subprocess.CompletedProcess, list]:
    log = TMP / f"spy-{time.time_ns()}.json"
    env = cfg.world.env(CLAUDE_CONFIG_DIR=str(cfg.dir), SPY_LOG=str(log), **({"FIXED_STAMP": stamp} if stamp else {}))
    done = subprocess.run([sys.executable, "-B", "-c", SPY_DRIVER, str(STATUS), "--config-dir", str(cfg.dir), *args], capture_output=True, text=True,
                          env=env, cwd=str(TMP), timeout=60, preexec_fn=lambda: os.umask(umask))
    assert log.exists(), f"the driver did not finish: {done.stdout[:200]} {done.stderr[-300:]}"
    return done, json.loads(log.read_text())


def test_backup_and_rewritten_settings_have_the_original_mode_under_any_umask():
    """The backup was made by shutil.copyfile (the umask's mode, 0644 where the original is 0600) and the file written next went through a
    temporary file that was open to everyone the umask let in until the chmod after it; a user's settings.json holds tokens in `env`."""
    fresh_tmpdir()
    world = World()
    launcher_mode = (mode_of(STATUS / "statusline_launcher.py") & 0o644) | 0o400
    for original_mode in (0o600, 0o644, 0o640, 0o400):
        for umask in (0o022, 0o000, 0o077):
            label = f"mode {original_mode:o}, umask {umask:03o}"
            cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, {"env": {"TOKEN": "secret"}}))
            cfg.real.chmod(original_mode)
            original = cfg.raw()
            proc, said = cfg.run("add", umask=umask)
            assert proc.returncode == 0 and said and said.get("written"), f"{label}: {proc.stdout[:200]} {proc.stderr[-200:]}"
            backups = sorted(cfg.dir.glob("settings.json.bak.*"))
            assert len(backups) == 1 and backups[0].read_bytes() == original, f"{label}: one backup with the original's content: {backups}"
            assert mode_of(backups[0]) == original_mode, f"{label}: the backup is {mode_of(backups[0]):o}"
            assert mode_of(cfg.real) == original_mode, f"{label}: the rewritten settings.json is {mode_of(cfg.real):o}"
            assert mode_of(cfg.data / "statusline-before.json") == (original_mode & 0o600 or 0o600), f"{label}: statusline-before.json holds the old command: {mode_of(cfg.data / 'statusline-before.json'):o}"
            assert mode_of(cfg.data / MARKER) == launcher_mode, f"{label}: the launcher is a copy of a public file: {mode_of(cfg.data / MARKER):o}"
            proc, said = cfg.run("remove", umask=umask)
            assert proc.returncode == 0 and said and said.get("written"), f"{label}: {proc.stdout[:200]} {proc.stderr[-200:]}"
            assert cfg.raw() == original, f"{label}: remove gives the file back byte for byte"
            assert mode_of(cfg.real) == original_mode, f"{label}: after remove the file is {mode_of(cfg.real):o}"
            backups = sorted(cfg.dir.glob("settings.json.bak.*"))
            assert len(backups) == 2 and all(mode_of(b) == original_mode for b in backups), f"{label}: remove makes a backup too, in the same mode: {[oct(mode_of(b)) for b in backups]}"
            assert not list(cfg.dir.rglob("*.tmp")), f"{label}: no temporary file left"


def test_what_the_set_up_script_creates_is_created_closed_and_exclusive_and_never_chmodded_by_name():
    """Never for an instant more open than the original: a file is created with at most 0600 (O_EXCL, so nothing already there is followed
    or written), written, and given its mode last, by its descriptor. No `chmod` of a path after the fact."""
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, {"env": {"TOKEN": "secret"}}))
    cfg.real.chmod(0o644)
    done, log = run_spied(cfg, "add")
    assert done.returncode == 0, f"{done.stdout[:200]} {done.stderr[-300:]}"
    created = [entry for entry in log if entry[0] == "create" and str(cfg.dir) in entry[1]]
    names = [Path(entry[1]).name for entry in created]
    assert any(".bak." in n for n in names), f"the backup is created with os.open: {names}"
    assert any(n.startswith(".settings.json.") and n.endswith(".tmp") for n in names), f"the temporary file of settings.json: {names}"
    assert any(n.startswith(".statusline-before.json.") for n in names) and any(n.startswith(f".{MARKER}.") for n in names), names
    for _, path, exclusive, mode in created:
        assert mode & 0o077 == 0, f"{Path(path).name}: created with {mode:o}, open to others before anything was done to close it"
        if Path(path).name != "statusline.lock":  # a lock file is opened again by the next run: it cannot be O_EXCL (it is empty and 0600)
            assert exclusive, f"{Path(path).name}: created without O_EXCL: something at that name would be followed or written"
    assert not [entry for entry in log if entry[0] == "chmod" and str(cfg.dir) in entry[1]], f"a file chmodded by name after it was made: {[e for e in log if e[0] == 'chmod']}"
    done, log = run_spied(cfg, "remove")
    assert done.returncode == 0
    assert all((e[2] or e[1].endswith("statusline.lock")) and e[3] & 0o077 == 0 for e in log if e[0] == "create" and str(cfg.dir) in e[1]), "remove: the same"
    assert not [e for e in log if e[0] == "chmod" and str(cfg.dir) in e[1]]


def test_a_file_or_a_symlink_at_the_backups_name_is_never_followed_or_overwritten():
    fresh_tmpdir()
    world = World()
    stamp = "20260101T000000"
    cfg = Cfg(world, settings_with(OTHER, 2, {"env": {"TOKEN": "secret"}}))
    cfg.real.chmod(0o600)
    original = cfg.raw()
    victim = world.root / "victim.txt"
    victim.write_text("VICTIM")
    gone = world.root / "does-not-exist-yet.txt"
    name = cfg.dir / f"settings.json.bak.{stamp}"
    name.symlink_to(victim)  # a symlink to somebody's file at the very name the backup would take
    (cfg.dir / f"settings.json.bak.{stamp}-1").symlink_to(gone)  # a dangling one: `exists()` says no, `open()` creates the target
    (cfg.dir / f"settings.json.bak.{stamp}-2").write_text("KEEP")  # a file of the user's
    done, log = run_spied(cfg, "add", umask=0o022, stamp=stamp)
    assert done.returncode == 0 and json.loads(done.stdout).get("written"), f"{done.stdout[:200]} {done.stderr[-300:]}"
    assert victim.read_text() == "VICTIM" and name.is_symlink() and name.resolve() == victim.resolve(), "nothing was written through the symlink"
    assert not gone.exists() and (cfg.dir / f"settings.json.bak.{stamp}-1").is_symlink(), "nothing was created through the dangling symlink"
    assert (cfg.dir / f"settings.json.bak.{stamp}-2").read_text() == "KEEP", "a file already there is not overwritten"
    made = cfg.dir / f"settings.json.bak.{stamp}-3"
    assert made.is_file() and not made.is_symlink() and made.read_bytes() == original and mode_of(made) == 0o600, "the first free name is used"
    assert json.loads(done.stdout)["backup"] == str(made)


def test_the_other_files_written_are_no_more_open_than_what_they_derive_from():
    """statusline-before.json (the old command, which can hold a token), board-link.json, the lock, the cache and the seen marks: 0600, in
    folders of 0700; the launcher is a copy of a public file and keeps its mode. Under a umask of 000."""
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2))
    cfg.real.chmod(0o600)
    assert cfg.run("add", umask=0o000)[0].returncode == 0
    assert cfg.run("record", "--answer", "both", umask=0o000)[0].returncode == 0
    assert mode_of(cfg.data) == 0o700, f"the data folder: {mode_of(cfg.data):o}"
    for name in ("statusline-before.json", "board-link.json", "statusline.lock"):
        assert mode_of(cfg.data / name) == 0o600, f"{name}: {mode_of(cfg.data / name):o}"
    assert mode_of(cfg.data / MARKER) == (mode_of(STATUS / "statusline_launcher.py") & 0o644) | 0o400
    # the filter, as the launcher runs it, with the data folder of this config
    world.config = cfg.dir
    assert b"| board" in strip(run_filter(world, HUD, umask=0o000, COLUMNS="100").decode()).encode()
    for folder in ("statusline-cache", "seen"):
        assert mode_of(cfg.data / folder) == 0o700, f"{folder}/: {mode_of(cfg.data / folder):o}"
        files = [p for p in (cfg.data / folder).iterdir()]
        assert files and all(mode_of(p) == 0o600 for p in files), f"{folder}/: {[oct(mode_of(p)) for p in files]}"
    assert not list(cfg.data.rglob("*.tmp")) and not [p for p in cfg.data.rglob(".*") if p.is_file() and p.suffix != ".lock"], "no temporary file left"


# ── a status line command that does not parse by itself (review of 15 Nov 2026, delta, finding 1) ───────────────────────────────────
NOT_PARSING = {
    "an unbalanced closing parenthesis (closes the wrapper's $( and leaves the rest outside it)": "echo a) ; echo INJECTED; (echo c",
    "an unbalanced quote": 'echo "unfinished',
    "an unfinished if": "if true; then echo x",
}


def test_add_refuses_a_command_that_does_not_parse_on_its_own():
    fresh_tmpdir()
    world = World()
    for label, command in NOT_PARSING.items():
        cfg = Cfg(world, settings_with(command))
        before = cfg.raw()
        for args in (("add",), ("add", "--dry-run")):
            proc, said = cfg.run(*args)
            assert proc.returncode == 2 and said and said.get("code") == "syntax" and isinstance(said.get("refused"), str) and len(said["refused"]) > 10, \
                f"{label}, {args}: {proc.returncode} {proc.stdout[:200]} {proc.stderr[-200:]}"
            assert set(said) == {"refused", "code"}, said
            assert cfg.raw() == before and cfg.files() == ["settings.json"], f"{label}, {args}: nothing was written: {cfg.files()}"
    broken = Cfg(world, settings_with(HAND_MADE + " )"))
    proc, said = broken.run("add", "--replace-hand-made")
    assert proc.returncode == 2 and said and said.get("code") == "syntax" and broken.files() == ["settings.json"], f"a hand-made command that does not parse: {proc.stdout[:200]}"


def test_every_command_of_the_fixtures_is_still_accepted():
    fresh_tmpdir()
    world = World()
    fixtures = [CLAUDE_HUD_POSIX, CLAUDE_HUD_GITBASH, OWNER_STYLE, OTHER, hud_command()[0], "printf 'one line\\n'", "printf 'x line\\n'; exit 3", "exit 7", "true",
                "printf ''", "printf 'a\\nb\\n'", "printf '%s\\n' 'claude-hud stand-in line'", "echo $(date) | cat && echo done;"]
    for command in fixtures:
        cfg = Cfg(world, settings_with(command))
        proc, said = cfg.run("add", "--dry-run")
        assert proc.returncode == 0 and said and said.get("old") == command, f"{command[:60]!r}: {proc.returncode} {proc.stdout[:200]}"
    cfg = Cfg(world, settings_with(HAND_MADE))
    proc, said = cfg.run("add", "--replace-hand-made", "--dry-run")
    assert proc.returncode == 0 and said and said.get("old") == HAND_MADE, "the hand-made filter's own command parses by itself"


def test_record_writes_board_link_json_through_a_rename_and_overwrites():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX))
    before = cfg.raw()
    proc, said = cfg.run("record", "--answer", "row")
    assert proc.returncode == 0 and said["answer"] == "row" and said["v"] == 1 and said["at"] and "hud" not in said, (proc.stdout, proc.stderr[-200:])
    assert json.loads((cfg.data / "board-link.json").read_text()) == said, "it prints what it wrote"
    proc, said = cfg.run("record", "--answer", "both", "--hud", "declined")
    record = json.loads((cfg.data / "board-link.json").read_text())
    assert record["answer"] == "both" and record["hud"] == "declined" and record == said
    cfg.run("record", "--answer", "status-line")
    assert json.loads((cfg.data / "board-link.json").read_text()).get("hud") is None and not list(cfg.data.glob("*.tmp")), "it overwrites: the user may choose again"
    assert cfg.raw() == before, "record never touches settings.json"
    for bad in (("--answer", "somewhere-else"), ("--hud", "proposed"), ("--answer", "row", "--hud", "maybe")):
        proc, said = cfg.run("record", *bad)
        assert proc.returncode == 2 and said["refused"] and said["code"] == "bad-answer", (bad, said)
    assert cfg.run("status")[1]["recorded"]["answer"] == "status-line", "status shows what was recorded"


def test_add_and_remove_never_touch_the_two_switches():
    """INTERFACE.md: the skill sets board_link_row and board_link_status_line (claude plugin configure); this script never does."""
    fresh_tmpdir()
    world = World()
    for options in (None, {"board_link_row": False}, {"board_link_row": False, "board_link_status_line": True}, {"board_link_row": True, "z": 1}):
        extra = {"pluginConfigs": {PLUGIN_KEY: {"options": options}, "other@x": {"options": {"k": 1}}}} if options is not None else {}
        cfg = Cfg(world, settings_with(CLAUDE_HUD_POSIX, 2, extra))
        original = cfg.raw()
        cfg.run("add")
        after_add = json.loads(cfg.raw())
        assert after_add.get("pluginConfigs") == json.loads(original).get("pluginConfigs"), f"{options}: add changed the plugin's options"
        cfg.run("remove")
        assert cfg.raw() == original, f"{options}: the file is back byte for byte, the switches as they were"


def test_before_file_holds_the_old_command_and_goes_with_remove():
    fresh_tmpdir()
    world = World()
    cfg = Cfg(world, settings_with(OWNER_STYLE, 2, {}, refreshInterval=5))
    cfg.run("add")
    before = json.loads((cfg.data / "statusline-before.json").read_text())
    assert before["command"] == OWNER_STYLE and MARKER in before["after_command"], "the old command is kept, for `remove` to restore"
    cfg.run("remove")
    assert not (cfg.data / "statusline-before.json").exists() and not (cfg.data / MARKER).exists()


def test_composed_command_is_the_one_the_plan_gives_character_for_character():
    fresh_tmpdir()
    world = World()
    command, _ = composed(world, CLAUDE_HUD_POSIX)
    expected = ('w=${COLUMNS:-}; out=$([ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8)); ' + CLAUDE_HUD_POSIX +
                '); rc=$?; if [ -n "$out" ]; then printf \'%s\\n\' "$out" | BOARD_LINK_COLUMNS="$w" python3 -I -S -B '
                f'"${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/plugins/data/{DATA_ID}/{MARKER}" 2>/dev/null || printf \'%s\\n\' "$out"; fi; exit $rc')
    assert command == expected, command


# The command as 2.3.0's first build wrote it (before the review of 15 Nov 2026): settings.json files of that form exist, so `status`, `add`
# and `remove` must still recognise it. Written out here, not imported: it is what is on a user's disk.
EARLIER_SUFFIX = ('); printf \'%s\\n\' "$out" | BOARD_LINK_COLUMNS="$w" python3 -I -S -B '
                  f'"${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/plugins/data/{DATA_ID}/{MARKER}" 2>/dev/null || printf \'%s\\n\' "$out"')
EARLIER_REDUCING = 'w=${COLUMNS:-}; out=$([ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8)); '
EARLIER_PLAIN = "w=${COLUMNS:-}; out=$("


def test_a_command_wrapped_by_the_earlier_form_is_still_recognised_and_removed():
    fresh_tmpdir()
    world = World()
    for label, prefix, orig in (("claude-hud's", EARLIER_REDUCING, CLAUDE_HUD_POSIX), ("another", EARLIER_PLAIN, OTHER), ("the owner's", EARLIER_PLAIN, OWNER_STYLE)):
        wrapped = prefix + orig + EARLIER_SUFFIX
        text = settings_with(wrapped, 4, {"theme": "dark"}, refreshInterval=5)
        for with_record in (False, True):
            cfg = Cfg(world, text)
            if with_record:  # as `add` of that build left it: the old command, and what it wrote
                cfg.data.mkdir(parents=True)
                (cfg.data / "statusline-before.json").write_text(json.dumps({"v": 1, "form": "x", "command": orig, "command_literal": json.dumps(orig), "after_command": wrapped}))
            proc, said = cfg.run("status")
            assert proc.returncode == 0 and said["wrapped"] is True, f"{label}: status sees the wrapper: {said}"
            proc, said = cfg.run("add")
            assert proc.returncode == 0 and said == {"written": False, "already": True}, f"{label}: add finds it already there: {said}"
            dry, said = cfg.run("remove", "--dry-run")
            assert dry.returncode == 0 and said == {"old": wrapped, "new": orig}, f"{label}: remove --dry-run shows the old command back: {said}"
            proc, said = cfg.run("remove")
            assert proc.returncode == 0 and said and said.get("written") is True and said["new"] == orig, f"{label} (record: {with_record}): {proc.stdout[:300]} {proc.stderr[-200:]}"
            assert json.loads(cfg.raw())["statusLine"]["command"] == orig, f"{label}: the command is back"
            assert json.loads(cfg.raw())["theme"] == "dark" and json.loads(cfg.raw())["statusLine"]["refreshInterval"] == 5, "nothing else moved"


def stdout_and_status(command: str, world: World, columns: str | None = "100", **env_extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, env=world.env(COLUMNS=columns, **env_extra), cwd=str(TMP), timeout=30)


def test_wrapper_keeps_the_exit_status_of_the_wrapped_command():
    """Review of 15 Nov 2026, finding 5: `out=$( ORIG ); … ` ended with the status of the last printf, so a command that exited 3 now exited 0."""
    fresh_tmpdir()
    world = World()
    empty = TMP / "empty-path"
    empty.mkdir(exist_ok=True)
    for orig, status in (("printf 'x line\\n'; exit 3", 3), ("printf 'x line\\n'", 0), ("printf 'x line\\n'; exit 1", 1), ("exit 7", 7), ("printf 'x\\n'; false", 1)):
        command, _ = composed(world, orig)
        done = stdout_and_status(command, world)
        assert done.returncode == status, f"{orig!r}: exit {done.returncode}, wanted {status}: {done.stdout!r} {done.stderr[:200]!r}"
        if "x line" in orig or "x\\n" in orig:
            assert "x" in strip(done.stdout), f"{orig!r}: its line still comes out: {done.stdout!r}"
        bare = stdout_and_status(command, world, PATH=str(empty))  # no python3: the line as it came, and still its status
        assert bare.returncode == status, f"{orig!r}: with no python3 the status is still {status}: {bare.returncode}"
        if status != 7:
            assert bare.stdout.startswith("x"), f"{orig!r}: {bare.stdout!r}"


def test_wrapper_prints_nothing_when_the_wrapped_command_printed_nothing():
    fresh_tmpdir()
    world = World()
    for orig in ("true", "exit 0", ":", "printf ''", "exit 5"):
        command, _ = composed(world, orig)
        done = stdout_and_status(command, world)
        assert done.stdout == "", f"{orig!r}: the wrapped command printed nothing, so nothing is printed (not one empty line): {done.stdout!r}"
        assert done.returncode == (5 if orig == "exit 5" else 0), f"{orig!r}: {done.returncode}"
    command, _ = composed(world, "printf 'a\\nb\\n'")
    done = stdout_and_status(command, world)
    assert strip(done.stdout).split("\n")[0].startswith("a") and "b" in done.stdout, "a command that prints something is still printed"


def plugin_files() -> set[str]:
    return {str(p) for root in (STATUS, PLUGIN / "hooks") for p in root.rglob("*") if p.is_file()}


PLUGIN_FILES_AT_START = plugin_files()


def test_last_nothing_was_written_inside_the_plugin_folder():
    """Run last (tests run in file order): the filter, the launcher and the set-up script write in the data folder, the temporary
    folder and settings.json fixtures, never beside their own files (a launcher run from status-board/ once made a seen/ there)."""
    now = plugin_files()
    assert now == PLUGIN_FILES_AT_START, f"new: {sorted(now - PLUGIN_FILES_AT_START)[:3]}, gone: {sorted(PLUGIN_FILES_AT_START - now)[:3]}"


# ── runner ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def main() -> int:
    setup()
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and ARGS.only in n]
    failed = skipped = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except Skip as why:
                skipped += 1
                print(f"SKIP  {name}: {why}")
            except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
                failed += 1
                reason = " ".join(str(exc).split())[:260] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        for proc in PROCESSES:
            proc.terminate()
        for proc in PROCESSES:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed - skipped} passed, {failed} failed, {skipped} skipped, processes left running: "
          f"{sum(1 for p in PROCESSES if p.poll() is None)}, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
