#!/usr/bin/env python3
"""The front door: one address for every board, each by its project's name (14 Nov 2026).

    python3 tests/test_front_door.py --plugin <plugin dir>

What is tested: status-board/front_door.py (every board server tries to hold one
fixed port; whoever holds it lists the boards and answers /<name> with a 302 to
the board's own address), its parts in serve.py (the record each server writes
at start, the election thread, the banner, `port_for` skipping the front door's
port) and in hooks/revive-board.py (the line it prints).

NO TEST BINDS A PORT FROM 8800 TO 8920, nor one of the owner's boards' ports.
The front door's port is set by the environment (STATUS_BOARD_FRONT_PORT) to a
free port the operating system hands out, every server and hook started here
inherits it, and every port a test binds is checked against the range by
`free_port()` before it is used. Everything runs in temporary folders with
STATUS_BOARD_HOME and HOME pointing into them. Nothing touches
~/.claude/status-board or a live board.

Run it against the code before the change and each test fails for the reason it
names (front_door.py does not exist; port_for still returns 8800); the tests
that guard what must not change (the boards' own checks) pass on both.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import html
import http.client
import importlib
import inspect
import importlib.util
import json
import math
import os
import pwd
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="the plugin directory under test")
ap.add_argument("--only", default="", help="run only the tests whose name contains this")
ap.add_argument("--old-plugin", default="", help="the plugin directory of the release before (2.3.0), for the tests that put its door "
                "and the new one side by side; without it those tests are reported as not run, never as passed")
ARGS = ap.parse_args()
OLD_PLUGIN = Path(ARGS.old_plugin).resolve() if ARGS.old_plugin else None
PLUGIN = Path(ARGS.plugin).resolve()
BOARD_DIR, HOOKS_DIR = PLUGIN / "status-board", PLUGIN / "hooks"
SERVE_PY, REVIVE_PY = BOARD_DIR / "serve.py", HOOKS_DIR / "revive-board.py"
sys.dont_write_bytecode = True

OWNER_PORTS = {8831, 8902, 8805, 18801, 18802}
FORBIDDEN_LOW, FORBIDDEN_HIGH = 8800, 8920
TMP = Path(tempfile.mkdtemp(prefix="front-door-")).resolve()
FAKE_HOME = TMP / "fakehome"
HOME = TMP / "home"  # the STATUS_BOARD_HOME of this process and of every server started here
PROCESSES: list[subprocess.Popen] = []
SOCKETS: list[socket.socket] = []


class NotRun(Exception):
    """A test that could not run here (it needs the old release): said by name, and never counted as passed."""


def safe_port(port: int) -> bool:
    return not (FORBIDDEN_LOW <= port <= FORBIDDEN_HIGH) and port not in OWNER_PORTS


def free_port() -> int:
    """A free port on loopback from the OS, never one of the forbidden ones."""
    for _ in range(100):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if safe_port(port):
            return port
    raise RuntimeError("no safe free port")


FRONT = free_port()  # THIS run's front door port; the real one (8800) is never used
assert safe_port(FRONT)
# Set before serve.py or front_door.py is imported here: they read these at import.
os.environ["STATUS_BOARD_HOME"] = str(HOME)
os.environ["STATUS_BOARD_FRONT_PORT"] = str(FRONT)
os.environ["STATUS_BOARD_ORCA"] = "0"
sys.path.insert(0, str(BOARD_DIR))


def FD():  # noqa: N802 - a module accessor, read as one
    """front_door, imported when a test first needs it, so that a missing file fails that test and no other."""
    path = BOARD_DIR / "front_door.py"
    assert path.exists(), f"{path.relative_to(PLUGIN)} does not exist"
    return importlib.import_module("front_door")


def SERVE():  # noqa: N802
    return importlib.import_module("serve")


def env_for(**extra: str) -> dict:
    FAKE_HOME.mkdir(exist_ok=True)
    env = {"HOME": str(FAKE_HOME), "PATH": f"/usr/bin:/bin:/usr/sbin:{os.path.dirname(sys.executable)}",
           "STATUS_BOARD_HOME": str(HOME), "STATUS_BOARD_FRONT_PORT": str(FRONT),
           "STATUS_BOARD_FRONT_POLL_S": "0.2", "STATUS_BOARD_ORCA": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    assert env["STATUS_BOARD_FRONT_PORT"].isdigit() and safe_port(int(env["STATUS_BOARD_FRONT_PORT"])), env
    return env


def now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=offset_s)).isoformat(timespec="seconds")


# ── a world of boards that are only records and sockets ───────────────
def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class World:
    """A STATUS_BOARD_HOME with `servers/<port>.json` records and `boards/<slug>/` folders. A board that is
    'up' is a listening socket on a free port, owned by this process (so its pid is alive)."""

    def __init__(self) -> None:
        self.state = Path(tempfile.mkdtemp(dir=TMP)) / "state"
        (self.state / "servers").mkdir(parents=True)
        (self.state / "boards").mkdir()
        self.sockets: list[socket.socket] = []

    def add(self, slug: str, *, name: str | None = None, repo: Path | str | None = None, up: bool = True,
            started: str | None = None, main_repo: str | None = None, folder: bool = True, record: bool = True,
            port: int | None = None, extra: dict | None = None) -> dict:
        if folder:
            (self.state / "boards" / slug).mkdir(exist_ok=True)
        if not record:
            return {}
        if repo is None:
            repo = self.state.parent / "repos" / slug
            repo.mkdir(parents=True, exist_ok=True)
        if port is None:
            port = free_port()
        if up:
            listener = socket.socket()
            listener.bind(("127.0.0.1", port))
            listener.listen(8)
            self.sockets.append(listener)
            SOCKETS.append(listener)
        rec = {"v": 1, "service": "status-board", "slug": slug, "name": name or slug, "repo": str(repo),
               "main_repo": main_repo, "port": port, "host": "127.0.0.1", "pid": os.getpid() if up else dead_pid(),
               "started": started or now_iso(), "plugin_version": "9.9.9", **(extra or {})}
        (self.state / "servers" / f"{port}.json").write_text(json.dumps(rec))
        return rec

    def close(self) -> None:
        for listener in self.sockets:
            listener.close()


class Door:
    """A FrontDoor of the plugin under test, serving over a free port in this process."""

    def __init__(self, world: World, *, port: int | None = None, real_check: bool = False, state: Path | None = None,
                 module=None, verify=None):
        fd = module or FD()
        kwargs = {}
        if not real_check and "verify" in inspect.signature(fd.FrontDoor.__init__).parameters:
            kwargs["verify"] = verify or (lambda row: True)  # these records are sockets of the test, not serve.py processes
        self.door = fd.FrontDoor(SERVE(), state=state or world.state, **kwargs)
        self.port = port or free_port()
        # The door's own server class when it has one (a listen queue that holds a burst), else the standard one.
        make = getattr(self.door, "make_server", None)
        self.httpd = make(("127.0.0.1", self.port)) if make else ThreadingHTTPServer(("127.0.0.1", self.port), self.door.handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path: str, *, method: str = "GET", host: str | None = None, headers: dict | None = None,
                body: bytes | None = None) -> tuple[int, dict, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdr = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        conn.request(method, path, body=body, headers=hdr)
        res = conn.getresponse()
        data = res.read().decode("utf-8", "replace")
        out = (res.status, {k.lower(): v for k, v in res.getheaders()}, data)
        conn.close()
        return out

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        getattr(self.door, "stop", lambda: None)()  # the thread that asks lsof


class with_door:  # noqa: N801 - a context manager used as `with with_door() as (world, door):`
    def __enter__(self):
        self.world = World()
        self.door = None
        return self.world, self

    def __exit__(self, *exc) -> None:
        if self.door is not None:
            self.door.close()
        self.world.close()

    def start(self) -> Door:
        self.door = Door(self.world)
        return self.door


def served() -> tuple[World, Door]:
    world = World()
    return world, Door(world)


def location(door: Door, path: str, **kw) -> tuple[int, str | None]:
    status, headers, _ = door.request(path, **kw)
    return status, headers.get("location")


# ── redirects and names ───────────────────────────────────────────────
def test_name_redirects_302_to_127_0_0_1_own_port_with_no_store():
    world, door = served()
    try:
        rec = world.add("shopapp", name="ShopApp")
        status, headers, _ = door.request("/shopapp", host=f"localhost:{door.port}")
        assert status == 302, status
        assert headers["location"] == f"http://127.0.0.1:{rec['port']}/", headers
        assert "no-store" in headers.get("cache-control", ""), headers
        assert door.request("/shopapp/")[0] == 302
    finally:
        door.close()
        world.close()


def test_name_is_case_insensitive_and_percent_decoded():
    world, door = served()
    try:
        rec = world.add("my-app", name="My App")
        want = f"http://127.0.0.1:{rec['port']}/"
        for path in ("/MY-APP", "/My-App", "/my%2Dapp", "/My%20App", "/my%20app/"):
            assert location(door, path) == (302, want), path
    finally:
        door.close()
        world.close()


def test_unique_prefix_redirects_exact_beats_prefix():
    world, door = served()
    try:
        plain = world.add("notes")
        longer = world.add("notes-archive-old")
        world.add("journal")
        assert location(door, "/notes") == (302, f"http://127.0.0.1:{plain['port']}/"), "exact beats prefix"
        assert location(door, "/notes-archive") == (302, f"http://127.0.0.1:{longer['port']}/")
        assert location(door, "/jou")[0] == 302
    finally:
        door.close()
        world.close()


def test_ambiguous_prefix_gives_300_with_links():
    world, door = served()
    try:
        a, b = world.add("notes-a"), world.add("notes-b")
        status, headers, body = door.request("/notes")
        assert status == 300 and "location" not in headers, (status, headers)
        assert 'href="/notes-a"' in body and 'href="/notes-b"' in body, body
        assert f"127.0.0.1:{a['port']}" in body and f"127.0.0.1:{b['port']}" in body
    finally:
        door.close()
        world.close()


def test_display_name_slug_also_resolves():
    world, door = served()
    try:
        rec = world.add("shopapp", name="Shop App")
        assert location(door, "/shop-app") == (302, f"http://127.0.0.1:{rec['port']}/")
        assert location(door, "/shopapp")[0] == 302
    finally:
        door.close()
        world.close()


def test_unknown_name_gives_404_with_list():
    world, door = served()
    try:
        world.add("shopapp")
        status, _, body = door.request("/nope")
        assert status == 404 and "shopapp" in body and "nope" in body, (status, body[:200])
        assert door.request("/shopapp/report.json")[0] == 404, "the front door redirects to a board, never into it"
    finally:
        door.close()
        world.close()


def test_board_down_gives_503_with_start_command_and_no_redirect():
    world, door = served()
    try:
        repo = world.state.parent / "repos" / "sleepy"
        repo.mkdir(parents=True)  # a dead board whose project still exists: listed, not hidden
        world.add("sleepy", name="Sleepy", repo=repo, up=False)
        status, headers, body = door.request("/sleepy")
        assert status == 503 and "location" not in headers, (status, headers)
        assert "sleepy is not running" in body.lower().replace("<strong>", "").replace("</strong>", ""), body
        # No path (the paths fix, 2.3.1): the project by name, how its board starts, a placeholder for the folder
        text = html.unescape(body)
        assert "serve.py" in text and "--repo" in text and "<the project's folder>" in text, body
        assert str(repo) not in body and str(repo.parent) not in body and str(PLUGIN) not in body, body
        assert "Sleepy" in body and "next Claude Code session in that project" in body, body
    finally:
        door.close()
        world.close()


def test_index_lists_up_first_and_marks_not_running():
    world, door = served()
    try:
        world.add("aaa-down", up=False)
        up = world.add("zzz-up", name="Zzz Up")
        status, headers, body = door.request("/")
        assert status == 200 and "text/html" in headers["content-type"], (status, headers)
        assert body.index("zzz-up") < body.index("aaa-down"), "boards that are up come first"
        assert "not running" in body and "up" in body.lower()
        assert f"127.0.0.1:{up['port']}" in body and f"localhost:{FRONT}/zzz-up" in body.replace("&#x27;", "'"), body
        assert "<script" not in body.lower(), "the list refreshes on reload, with no script"
    finally:
        door.close()
        world.close()


def test_board_folder_without_record_listed_as_address_not_known():
    world, door = served()
    try:
        world.add("old-board", record=False)
        assert "address not known yet" in door.request("/")[2]
        status, headers, _ = door.request("/old-board")
        assert status == 503 and "location" not in headers, status
    finally:
        door.close()
        world.close()


def test_same_slug_two_repos_gives_300_with_shared_folder_warning():
    world, door = served()
    try:
        repo_a, repo_b = world.state.parent / "a" / "app", world.state.parent / "b" / "app"
        repo_a.mkdir(parents=True)
        repo_b.mkdir(parents=True)
        world.add("app", repo=repo_a)
        world.add("app", repo=repo_b)
        status, headers, body = door.request("/app")
        assert status == 300 and "location" not in headers, (status, headers)
        # No path (the paths fix, 2.3.1): the two are told apart by their ports and start times, which the page says
        assert str(repo_a) not in body and str(repo_b) not in body and str(world.state) not in body, body
        assert body.count("<li>") == 2 and "started" in body and body.count("127.0.0.1:") >= 2, body  # both are up: their ports are links
        assert "share one board folder" in body, body
    finally:
        door.close()
        world.close()


def test_worktree_record_has_main_repo_and_list_says_worktree_of():
    main = TMP / "wt" / "mainproj"
    main.mkdir(parents=True)
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    linked = TMP / "wt" / "linked-tree"
    subprocess.run([*git, "worktree", "add", "-q", str(linked)], check=True)
    world = World()
    try:
        fd = FD()
        door = fd.FrontDoor(SERVE(), state=world.state)
        for repo, port, expect in ((linked, free_port(), str(main)), (main, free_port(), None)):
            rec = door.write_record(SimpleNamespace(slug=repo.name, name=repo.name, repo=repo), port, "127.0.0.1")
            assert rec["main_repo"] == expect, (repo, rec)
            assert json.loads((world.state / "servers" / f"{port}.json").read_text())["main_repo"] == expect
        world.add("linked-tree", repo=linked, up=True, main_repo=str(main))
        world.add("mainproj", repo=main, up=True)
        listing = Door(world)
        try:
            body = listing.request("/")[2]
            assert "worktree of mainproj" in body, body
        finally:
            listing.close()
    finally:
        world.close()


def test_moved_project_live_record_wins_old_one_listed_not_running():
    world, door = served()
    try:
        old_repo, new_repo = world.state.parent / "old" / "proj", world.state.parent / "new" / "proj"
        old_repo.mkdir(parents=True)
        new_repo.mkdir(parents=True)
        world.add("proj", repo=old_repo, up=False, started=now_iso(-3600))
        live = world.add("proj", repo=new_repo, up=True, started=now_iso())
        assert location(door, "/proj") == (302, f"http://127.0.0.1:{live['port']}/"), "the live record wins"
        body = door.request("/")[2]
        assert "not running" in body, "the old one is still listed, as not running"
        assert str(old_repo) not in body and str(new_repo) not in body, "and no path says which project it is"
        assert body.count("<td>proj</td>") == 2, "both rows are there: " + body
    finally:
        door.close()
        world.close()


def test_dead_record_with_missing_repo_is_hidden():
    world, door = served()
    try:
        world.add("ghost", repo=world.state.parent / "no" / "such" / "ghost", up=False, folder=False)
        world.add("here", up=True)
        body = door.request("/")[2]
        assert "ghost" not in body and "here" in body, body
    finally:
        door.close()
        world.close()


# ── safety ────────────────────────────────────────────────────────────
def test_front_door_refuses_foreign_host_403():
    world, door = served()
    try:
        world.add("shopapp")
        for host in ("evil.example", "evil.localhost", "shopapp.localhost", f"evil.example:{door.port}",
                     "127.0.0.1.evil.example", "user@127.0.0.1"):
            status, headers, body = door.request("/shopapp", host=host)
            assert status == 403 and "location" not in headers, (host, status)
        for host in (f"127.0.0.1:{door.port}", f"localhost:{door.port}", f"[::1]:{door.port}"):
            assert door.request("/shopapp", host=host)[0] == 302, host
        assert door.request("/", host="evil.example")[0] == 403
    finally:
        door.close()
        world.close()


def test_front_door_rejects_post_put_delete_405():
    world, door = served()
    try:
        world.add("shopapp")
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, headers, _ = door.request("/shopapp", method=method, body=b"{}",
                                              headers={"Content-Type": "application/json"})
            assert status == 405 and "GET" in headers.get("allow", ""), (method, status, headers)
        assert door.request("/shopapp", method="HEAD")[0] == 302
        status, headers, body = door.request("/", method="HEAD")
        assert status == 200 and body == "", (status, body)
    finally:
        door.close()
        world.close()


def test_front_door_pages_escape_names_and_paths():
    world, door = served()
    try:
        evil = '<script>alert(1)</script>'
        world.add("evil-board", name=evil, repo=world.state.parent / evil, up=False)
        (world.state.parent / evil).mkdir(parents=True, exist_ok=True)
        pages = [door.request("/"), door.request("/evil-board"), door.request("/%3Cscript%3Ealert(1)%3C/script%3E"),
                 door.request("/evil")]
        for status, headers, body in pages:
            assert "<script" not in body.lower(), (status, body[:300])
            assert "nosniff" in headers.get("x-content-type-options", "") and "default-src 'none'" in headers.get(
                "content-security-policy", ""), headers
            assert headers.get("referrer-policy") == "no-referrer", headers
        assert "&lt;script&gt;" in pages[0][2], pages[0][2]
    finally:
        door.close()
        world.close()


def test_board_host_and_write_checks_unchanged():
    """The boards keep their own checks, which the front door must not have weakened: a matrix of what
    host_refusal and write_refusal answer for the headers a browser, curl and a foreign page send."""
    serve = SERVE()
    names = serve.allowed_hosts({})
    ok = lambda host: serve.host_refusal({"Host": host}, True, names)  # noqa: E731
    for host in ("127.0.0.1:8831", "localhost:8831", "[::1]:8831", "127.0.0.1", "LOCALHOST:1"):
        assert ok(host) is None, host
    for host in ("evil.example", "evil.localhost:8831", "shopapp.localhost", "127.0.0.1.evil.example", "a@127.0.0.1", ""):
        refusal = ok(host)
        assert refusal and refusal[0] == 403, host
    assert serve.host_refusal({"Host": "evil.example"}, False, names) is None, "requests from other machines carry the token"
    good = {"Host": "127.0.0.1:8831", "Content-Type": "application/json", "Origin": "http://127.0.0.1:8831",
            "Sec-Fetch-Site": "same-origin"}
    assert serve.write_refusal(good, True, names) is None
    assert serve.write_refusal({**good, "Content-Type": "text/plain"}, True, names)[0] == 415
    assert serve.write_refusal({**good, "Sec-Fetch-Site": "cross-site"}, True, names)[0] == 403
    assert serve.write_refusal({**good, "Origin": "http://evil.example"}, True, names)[0] == 403
    assert serve.write_refusal({**good, "Host": "evil.example", "Origin": "http://evil.example"}, True, names)[0] == 403


# ── what is written, and where the port is ────────────────────────────
def test_record_written_atomically_and_unknown_keys_ignored():
    world = World()
    try:
        fd = FD()
        door = fd.FrontDoor(SERVE(), state=world.state)
        repo = TMP / "rec" / "atomic"
        repo.mkdir(parents=True)
        board = SimpleNamespace(slug="atomic", name="Atomic", repo=repo)
        port = free_port()
        door.write_record(board, port, "127.0.0.1")
        path = world.state / "servers" / f"{port}.json"
        stop, bad = threading.Event(), []

        def read_loop() -> None:
            while not stop.is_set():
                try:
                    json.loads(path.read_text())
                except (OSError, ValueError) as exc:  # a missing or torn record
                    bad.append(repr(exc))

        reader = threading.Thread(target=read_loop, daemon=True)
        reader.start()
        for _ in range(300):
            door.write_record(board, port, "127.0.0.1")
        stop.set()
        reader.join(timeout=5)
        assert not bad, f"{len(bad)} reads found the record missing or torn, first: {bad[:1]}"
        assert not list((world.state / "servers").glob("*.tmp")), "no .tmp left behind"
        rec = json.loads(path.read_text())
        assert rec["v"] == 1 and rec["service"] == "status-board" and rec["slug"] == "atomic" and rec["port"] == port, rec
        assert rec["pid"] == os.getpid() and rec["host"] == "127.0.0.1" and rec["repo"] == str(repo), rec
        # a later version adds keys: they are ignored, not refused
        world.add("future", extra={"some_key_of_a_later_version": {"x": 1}})
        listing = Door(world)
        try:
            assert listing.request("/future")[0] == 302
        finally:
            listing.close()
    finally:
        world.close()


def test_port_for_unchanged_except_front_port_value():
    """For 10 000 random paths port_for equals the old formula, except where the old formula gave the front
    door's port: there it gives 8920. Runs in a process with no STATUS_BOARD_FRONT_PORT, so the default port is
    the one skipped. It only computes: nothing is bound."""
    code = (
        "import sys, random, zlib\n"
        f"sys.path.insert(0, {str(BOARD_DIR)!r})\n"
        "sys.dont_write_bytecode = True\n"
        "import serve\n"
        "from pathlib import Path\n"
        "rng = random.Random(14112026)\n"
        "moved = bad = 0\n"
        "for i in range(10000):\n"
        "    p = '/Users/x/' + ''.join(rng.choice('abcdefghij/-_') for _ in range(rng.randint(3, 40)))\n"
        "    old = 8800 + zlib.crc32(str(Path(p)).encode()) % 120\n"
        "    new = serve.port_for(Path(p))\n"
        "    if old == 8800:\n"
        "        moved += 1\n"
        "        bad += new != 8920\n"
        "    else:\n"
        "        bad += new != old\n"
        "print(moved, bad)\n"
    )
    env = {k: v for k, v in env_for().items() if k != "STATUS_BOARD_FRONT_PORT"}
    done = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    moved, bad = map(int, done.stdout.split())
    assert bad == 0, f"{bad} of 10000 paths differ from the old formula other than where it gave 8800"
    assert moved >= 20, f"only {moved} of 10000 paths hashed to 8800: the test did not look at the skip"


def test_election_only_in_the_default_home_or_with_the_port_set_explicitly():
    """The existing suite starts real servers in a temporary STATUS_BOARD_HOME with a clean environment: they must
    not try to hold the front door's port. Pure: nothing is bound. The default home is the ACCOUNT's, from the
    password database: a HOME pointing at a temporary folder is not it (the review, finding 5)."""
    fd = FD()
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    default = account_home / ".claude" / "status-board"
    elsewhere = TMP / "some-other-home"
    env_home = {"HOME": str(FAKE_HOME)}
    assert fd.election_enabled(default, env_home) is True, "the account's own home is the default, whatever HOME says"
    assert fd.election_enabled(default, {}) is True
    assert fd.election_enabled(Path(FAKE_HOME) / ".claude" / "status-board", env_home) is False, (
        "a HOME that points at a temporary folder is not the default home: such a process must not bind the real port")
    assert fd.election_enabled(elsewhere, env_home) is False, "a board in another home is another universe"
    assert fd.election_enabled(elsewhere, {**env_home, "STATUS_BOARD_FRONT_PORT": "29999"}) is True
    assert fd.election_enabled(elsewhere, {**env_home, "STATUS_BOARD_FRONT_PORT": ""}) is False


# ── processes: the election, the banner, the line the hook prints ─────
class Board:
    """serve.py of the plugin under test, in this run's home, on a free port, with its stdout collected."""

    def __init__(self, name: str, *, host: str | None = None, front: int | None = None, poll: str | None = "0.2",
                 home: Path | None = None):
        self.repo = TMP / "boards-repos" / name
        self.repo.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        extra = {} if front is None else {"STATUS_BOARD_FRONT_PORT": str(front)}
        if poll is None:
            env = {k: v for k, v in env_for(**extra).items() if k != "STATUS_BOARD_FRONT_POLL_S"}
        else:
            env = env_for(STATUS_BOARD_FRONT_POLL_S=poll, **extra)
        if home is not None:
            env["STATUS_BOARD_HOME"] = str(home)
        cmd = [sys.executable, "-B", str(SERVE_PY), "--repo", str(self.repo), "--port", str(self.port)]
        if host:
            cmd += ["--host", host]
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(TMP))
        PROCESSES.append(self.proc)
        self.lines: list[str] = []
        self.err: list[str] = []
        for stream, sink in ((self.proc.stdout, self.lines), (self.proc.stderr, self.err)):
            threading.Thread(target=lambda s=stream, k=sink: [k.append(line.rstrip("\n")) for line in s], daemon=True).start()
        self.wait_listening()

    def wait_listening(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
                return
            except OSError:
                assert self.proc.poll() is None, f"serve.py exited: {self.err}"
                time.sleep(0.05)
        raise AssertionError(f"board did not listen on {self.port}: out={self.lines} err={self.err}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def listeners(port: int) -> list[tuple[int, str]]:
    """(pid, address) of every process listening on this port, from lsof."""
    done = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True,
                          env={"PATH": "/usr/sbin:/usr/bin:/bin"})
    out, pid = [], 0
    for line in done.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n"):
            out.append((pid, line[1:]))
    return out


def door_state(port: int = FRONT) -> str:
    """'door' when a front door answers on this port, else why not."""
    fd = FD()
    return fd.probe_state(port)


def wait_for(predicate, timeout: float, step: float = 0.05) -> float | None:
    """Seconds until predicate() is true, or None."""
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return time.time() - start
        time.sleep(step)
    return None


def test_election_two_boards_one_holder_then_takeover_within_6s_after_kill():
    """Real servers, the default 5 s retry (no STATUS_BOARD_FRONT_POLL_S): kill the board that holds the front door,
    and another takes it. The seconds it took are printed."""
    first = second = None
    try:
        first = Board("alpha", poll=None)
        assert wait_for(lambda: door_state() == "door", 5) is not None, "the first board never held the front door"
        second = Board("beta", poll=None)
        time.sleep(0.5)
        holders = listeners(FRONT)
        assert [pid for pid, _ in holders] == [first.proc.pid], f"one holder, the first: {holders}"
        body = http_get(FRONT, "/")
        assert "alpha" in body and "beta" in body, body
        os.kill(first.proc.pid, signal.SIGKILL)
        killed = time.time()
        first.proc.wait()
        took = wait_for(lambda: [p for p, _ in listeners(FRONT)] == [second.proc.pid] and door_state() == "door", 8)
        assert took is not None, f"nobody took the front door within 8 s: {listeners(FRONT)}"
        elapsed = time.time() - killed
        print(f"      takeover: {elapsed:.2f} s after kill -9 of the holder (default retry 5 s)")
        assert elapsed <= 6.0, f"takeover took {elapsed:.2f} s"
        after = http_get(FRONT, "/")
        assert "beta" in after, after
    finally:
        for board in (first, second):
            if board:
                board.stop()


def http_get(port: int, path: str, host: str | None = None) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    body = conn.getresponse().read().decode("utf-8", "replace")
    conn.close()
    return body


class NonBoard:
    """Something else on the front door's port: 'http' answers 200 with no marker, 'silent' accepts and says nothing."""

    def __init__(self, port: int, mode: str):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        SOCKETS.append(self.sock)
        self.mode, self.conns, self.closed = mode, [], False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            if self.mode == "http":
                try:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhello")
                finally:
                    conn.close()

    def close(self) -> None:
        self.closed = True
        self.sock.close()
        for conn in self.conns:
            conn.close()


def test_election_retries_quietly_when_port_held_by_non_board():
    blocker, board = None, None
    try:
        blocker = NonBoard(FRONT, "http")
        board = Board("quiet")
        time.sleep(1.5)  # several retries at 0.2 s
        assert board.proc.poll() is None and not board.err, f"the board must stay up and say nothing on stderr: {board.err}"
        assert board.lines and all("Traceback" not in line for line in board.lines), board.lines
        assert door_state() == "other"
        blocker.close()
        took = wait_for(lambda: door_state() == "door", 5)
        assert took is not None, "the board never took the front door once it was free"
        assert [pid for pid, _ in listeners(FRONT)] == [board.proc.pid]
    finally:
        if blocker:
            blocker.close()
        if board:
            board.stop()


def test_front_door_binds_127_0_0_1_only_even_when_board_binds_0_0_0_0():
    board = None
    try:
        board = Board("lanboard", host="0.0.0.0")
        assert wait_for(lambda: door_state() == "door", 5) is not None, f"no front door: {board.lines} {board.err}"
        addresses = [addr for _, addr in listeners(FRONT)]
        assert addresses and all(a == f"127.0.0.1:{FRONT}" for a in addresses), addresses
        own = [addr for _, addr in listeners(board.port)]
        assert any(a.startswith("*:") for a in own), f"the board itself was asked for 0.0.0.0: {own}"
    finally:
        if board:
            board.stop()


def banner_of(board: Board) -> list[str]:
    assert wait_for(lambda: any("board →" in line for line in board.lines) and len(board.lines) >= 3, 10) is not None, board.lines
    return board.lines


def test_banner_by_name_when_front_door_answers_and_by_port_with_reason_when_not():
    fd = FD()
    # 1. the board wins the front door itself, or one answers: by name
    board = Board("bannerone")
    try:
        lines = banner_of(board)
        assert lines[0] == f"bannerone board → http://localhost:{FRONT}/bannerone", lines
        assert lines[1] == (f"  same board, by port: http://127.0.0.1:{board.port}   ·   "
                            f"every board: http://localhost:{FRONT}"), lines
    finally:
        board.stop()
    assert wait_for(lambda: door_state() != "door", 5) is not None
    # 2. something that is not a board holds the port: by port, with the reason
    for mode, reason in (("http", f"port {FRONT} is held by something that is not a board"),
                         ("silent", f"port {FRONT} did not answer")):
        blocker = NonBoard(FRONT, mode)
        board = Board(f"banner{mode}")
        try:
            lines = banner_of(board)
            assert lines[0] == f"banner{mode} board → http://127.0.0.1:{board.port}", lines
            assert lines[1] == (f"  (the address by name, http://localhost:{FRONT}/banner{mode}, is not available: "
                                f"{reason})"), lines
        finally:
            board.stop()
            blocker.close()
        time.sleep(0.3)
    # 3. nobody holds it (and none could be started): the third reason, as a pure function
    assert FD().why_unavailable("free", FRONT) == f"no board holds port {FRONT} yet"
    assert fd.why_unavailable("other", FRONT).endswith("something that is not a board")
    assert fd.why_unavailable("silent", FRONT) == f"port {FRONT} did not answer"


def load_revive():
    spec = importlib.util.spec_from_file_location("revive_board_under_test", REVIVE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revive_line_uses_address_by_name():
    """revive-board.py derives its port from the repo path, which is always inside 8800 to 8920, so this calls its
    `start()` with a port of ours. That is the function that makes the line."""
    revive = load_revive()
    serve = revive.load_serve()
    repo = TMP / "revived" / "alpha-revive"
    repo.mkdir(parents=True)
    board_dir = Path(serve.STATE) / "boards" / "alpha-revive"
    board_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    holder = None
    try:
        os.environ["STATUS_BOARD_FRONT_POLL_S"] = "0.2"
        line = revive.start(serve, repo, "alpha-revive", port, board_dir)
        assert line == (f'project-init: status board "alpha-revive" was not running; started it again: '
                        f"http://localhost:{FRONT}/alpha-revive (by port: http://127.0.0.1:{port})"), line
    finally:
        for proc in subprocess.run(["pgrep", "-f", f"serve.py.*--port {port}"], capture_output=True, text=True).stdout.split():
            try:
                os.kill(int(proc), signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.5)
        # when the front door cannot be reached, the line is today's line, unchanged
    blocker = NonBoard(FRONT, "http")
    try:
        port2 = free_port()
        repo2 = TMP / "revived" / "beta-revive"
        repo2.mkdir(parents=True)
        (Path(serve.STATE) / "boards" / "beta-revive").mkdir(parents=True, exist_ok=True)
        line2 = revive.start(serve, repo2, "beta-revive", port2, Path(serve.STATE) / "boards" / "beta-revive")
        assert line2 == (f'project-init: status board "beta-revive" was not running; started it again: '
                         f"http://127.0.0.1:{port2}"), line2
    finally:
        blocker.close()
        for proc in subprocess.run(["pgrep", "-f", f"serve.py.*--port {port2}"], capture_output=True, text=True).stdout.split():
            try:
                os.kill(int(proc), signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.5)


# ── after the review (14 Nov 2026, P/urls/REVIEW-urls.md): the six fixes and the opener policy ──────────
# Each test below failed on the front door as first delivered. Port safety is as above: every port is the
# operating system's, checked against 8800 to 8920 and the owner's ports, and every stand-in listens on one of them.
import stat as _stat


def parse_raw(data: bytes) -> tuple[int, dict]:
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    status = int(head[0].split()[1])
    return status, {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in head[1:])}


def raw_exchange(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(data)
        out = b""
        try:
            while chunk := conn.recv(65536):
                out += chunk
        except OSError:
            pass
        return out


def assert_hardened(headers: dict, where: str) -> None:
    csp = headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp, (where, csp)
    assert headers.get("x-frame-options") == "DENY", (where, headers.get("x-frame-options"))
    assert headers.get("cross-origin-opener-policy") == "same-origin", (where, headers.get("cross-origin-opener-policy"))
    assert headers.get("x-content-type-options") == "nosniff" and headers.get("referrer-policy") == "no-referrer", (where, headers)


def mode_of(path: Path) -> int:
    return _stat.S_IMODE(os.stat(path).st_mode)


def test_f1_every_response_of_the_door_forbids_framing_and_openers():
    """Findings 1 (the door's half) and the opener policy: a page of another origin framed `/<name>` and one click on
    the board behind it recorded a ruling; a site that opened the door in a window kept a handle on it."""
    world, door = served()
    try:
        world.add("up-board")
        world.add("sleepy", up=False)
        world.add("ledger-a")
        world.add("ledger-b")
        cases = [("GET", "/", 200, None), ("GET", "/up-board", 302, None), ("GET", "/nope", 404, None),
                 ("GET", "/sleepy", 503, None), ("GET", "/ledger", 300, None), ("POST", "/up-board", 405, None),
                 ("GET", "/", 403, "evil.example"), ("HEAD", "/", 200, None), ("GET", "/_boards.json", 200, None)]
        for method, path, status, host in cases:
            got, headers, _ = door.request(path, method=method, host=host)
            assert got == status, (method, path, got)
            assert_hardened(headers, f"{method} {path}")
        # the responses the standard library builds by itself: an unknown method, a request line that is too long
        status, headers = parse_raw(raw_exchange(door.port, b"TRACE / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"))
        assert status == 501, status
        assert_hardened(headers, "TRACE (501)")
        status, headers = parse_raw(raw_exchange(door.port, b"GET /" + b"a" * 65600 + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"))
        assert status == 414, status
        assert_hardened(headers, "long request line (414)")
    finally:
        door.close()
        world.close()


class StandIn:
    """A local program that holds a port and answers like a front door: the marker and nothing else, or a response
    that it replays (one it fetched from the true door). `held` counts the requests it was asked."""

    def __init__(self, port: int | None = None, replay: bytes | None = None):
        marker = json.dumps({"service": FD().SERVICE, "v": 1, "boards": []}).encode()
        body = replay if replay is not None else marker
        self.held = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                outer.held += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.port = port or free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_f2_a_true_door_is_believed_and_a_stand_in_that_answers_the_marker_is_not():
    world = World()
    door = Door(world)
    stand = StandIn()
    try:
        key = door.door.ensure_key()
        assert key and len(key) >= 16, "the home has a secret that only its owner's account can read"
        fd = FD()
        assert fd.probe_state(door.port, world.state) == "door"
        assert fd.probe_state(stand.port, world.state) == "other", "the marker alone proves nothing"
        assert fd.address_for("x", stand.port, world.state) is None and fd.address_for("x", door.port, world.state)
        # the proof answers a challenge, and the secret itself never travels
        status, _, body = door.request("/_boards.json?c=" + "ab" * 16)
        data = json.loads(body)
        assert status == 200 and data.get("proof") and key.hex() not in body, body
        listed = json.loads(door.request("/_boards.json")[2])
        assert "proof" not in listed, "no challenge, no proof"
        assert all("pid" not in row for row in listed["boards"]), "rows carry no pid"
    finally:
        stand.close()
        door.close()
        world.close()


def test_f2_an_impostor_that_replays_what_it_read_from_the_true_door_is_not_believed():
    world = World()
    door = Door(world)
    try:
        door.door.ensure_key()
        replayed = door.request("/_boards.json?c=" + "cd" * 16)[2].encode()  # what a stranger can read from the true door
        stand = StandIn(replay=replayed)
        try:
            fd = FD()
            assert fd.probe_state(stand.port, world.state) == "other", "a fresh challenge needs a fresh proof"
            assert fd.probe_state(door.port, world.state) == "door"
        finally:
            stand.close()
    finally:
        door.close()
        world.close()


def test_f2_the_door_changes_hands_and_the_secret_follows_it():
    """The secret belongs to the home, not to whichever board holds the door: a true door is never taken for a false
    one, in the moment of the change nobody answers at all ('free'), and the new holder proves itself with the same key."""
    world = World()
    first = Door(world)
    port = first.port
    second = None
    try:
        key = first.door.ensure_key()
        fd = FD()
        assert fd.probe_state(port, world.state) == "door"
        first.close()
        assert fd.probe_state(port, world.state) == "free", "in the gap nothing answers: not 'other'"
        second = Door(world, port=port)
        assert second.door.ensure_key() == key and fd.probe_state(port, world.state) == "door"
    finally:
        for each in (first, second):
            if each is not None:
                each.close()
        world.close()


def test_f2_boards_that_start_at_once_end_with_one_secret():
    world = World()
    code = ("import sys\nfrom pathlib import Path\nsys.dont_write_bytecode = True\nsys.path.insert(0, sys.argv[1])\n"
            "import serve, front_door\n"
            "print(front_door.FrontDoor(serve, state=Path(sys.argv[2])).ensure_key().hex(), flush=True)\n")
    try:
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, str(BOARD_DIR), str(world.state)], env=env_for(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        PROCESSES.extend(procs)
        outs = [p.communicate(timeout=60) for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[1][-200:] for o in outs]
        keys = {o[0].strip() for o in outs}
        assert len(keys) == 1 and len(next(iter(keys))) == 64, keys
        assert mode_of(world.state / "servers" / "front.key") == 0o600
        assert (world.state / "servers" / "front.key").read_bytes().hex() == next(iter(keys)), "nobody read a half-written key"
    finally:
        world.close()


def test_f2_a_missing_unreadable_or_loose_secret_means_the_door_is_not_believed():
    world = World()
    door = Door(world)
    try:
        fd = FD()
        keyfile = world.state / "servers" / "front.key"
        assert fd.probe_state(door.port, world.state) == "other", "no secret yet: it cannot be checked, so it is not believed"
        door.door.ensure_key()
        assert fd.probe_state(door.port, world.state) == "door"
        keyfile.chmod(0o644)  # readable by others: no longer a secret
        assert fd.probe_state(door.port, world.state) == "other"
        keyfile.chmod(0o000)
        assert fd.probe_state(door.port, world.state) == "other"
        keyfile.chmod(0o600)
        assert fd.probe_state(door.port, world.state) == "door"
    finally:
        door.close()
        world.close()


def test_f2_a_stand_in_door_gets_the_port_address_in_the_banner_and_the_revive_line():
    stand = StandIn(port=FRONT)
    board = None
    port2 = None
    try:
        board = Board("standin-board")
        lines = banner_of(board)
        assert lines[0] == f"standin-board board → http://127.0.0.1:{board.port}", lines
        assert lines[1] == (f"  (the address by name, http://localhost:{FRONT}/standin-board, is not available: "
                            f"port {FRONT} is held by something that is not a board)"), lines
        board.stop()
        revive = load_revive()
        serve = revive.load_serve()
        repo = TMP / "revived" / "standin-revive"
        repo.mkdir(parents=True)
        (Path(serve.STATE) / "boards" / "standin-revive").mkdir(parents=True, exist_ok=True)
        port2 = free_port()
        line = revive.start(serve, repo, "standin-revive", port2, Path(serve.STATE) / "boards" / "standin-revive")
        assert line == (f'project-init: status board "standin-revive" was not running; started it again: '
                        f"http://127.0.0.1:{port2}"), line
    finally:
        stand.close()
        if board:
            board.stop()
        if port2:
            for pid in subprocess.run(["pgrep", "-f", f"serve.py.*--port {port2}"], capture_output=True, text=True).stdout.split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
            time.sleep(0.5)


def test_f3_the_door_redirects_only_to_a_port_where_a_board_of_ours_listens():
    """A record decides where /<name> goes: a forged one, or a stale one whose pid and port were both reused, sent it
    to whatever listened on that port. Now the process listening there must be the record's pid, and a serve.py."""
    fd = FD()
    board = Board("realboard")
    outsiders = [NonBoard(free_port(), "http") for _ in range(3)]
    servers = HOME / "servers"
    forged = []
    shown = None
    try:
        assert wait_for(lambda: (servers / f"{board.port}.json").exists(), 8) is not None, "the board wrote its record"
        real_record = json.loads((servers / f"{board.port}.json").read_text())
        repo = TMP / "forged-repo"
        repo.mkdir(exist_ok=True)
        base = {**real_record, "repo": str(repo)}
        specs = [
            # a forged board: a live-looking pid that is not the listener, on a port where something else listens
            {**base, "slug": "forged", "name": "forged", "port": outsiders[0].sock.getsockname()[1], "pid": 1},
            # the test's own pid, which does listen there, but is not a serve.py
            {**base, "slug": "pidmine", "name": "pidmine", "port": outsiders[1].sock.getsockname()[1], "pid": os.getpid()},
            # the real board's slug and repo, a later start, an impostor's port: it must not win, nor make a clash
            {**real_record, "port": outsiders[2].sock.getsockname()[1], "pid": 1, "started": now_iso(60)},
        ]
        for spec in specs:
            path = servers / f"{spec['port']}.json"
            path.write_text(json.dumps(spec))
            forged.append(path)
        shown = Door(World(), real_check=True, state=HOME)
        got = shown.request("/realboard")
        assert (got[0], got[1].get("location")) == (302, f"http://127.0.0.1:{board.port}/"), (
            f"the board of ours is reached: {got[0]} {got[1].get('location')} {got[2][:400]!r}")
        for name in ("forged", "pidmine"):
            status, headers, body = shown.request(f"/{name}")
            assert status == 503 and "location" not in headers, (name, status, headers)
            assert "held by something that is not a board" in body, f"the page says why: {body[-300:]}"
        listing = shown.request("/")[2]
        assert listing.count("held by something that is not a board") == 2 and "not running" not in listing.split("forged")[1][:400], (
            "the list marks them, and does not call them up")
    finally:
        if shown:
            shown.close()
        for path in forged:
            path.unlink(missing_ok=True)
        for outsider in outsiders:
            outsider.close()
        board.stop()


def test_f4_port_for_always_skips_8800_whatever_the_front_port_is_set_to():
    """With STATUS_BOARD_FRONT_PORT set to anything else (every test does), a path that hashes to 8800 got 8800 again,
    and a board would hold the door's default address. Pure: nothing is bound."""
    import zlib

    fd, serve = FD(), SERVE()
    found = {8800: [], 8850: []}
    i = 0
    while min(len(v) for v in found.values()) < 3 and i < 200000:
        i += 1
        path = Path(f"/Users/x/project-{i}")
        port = 8800 + zlib.crc32(str(path).encode()) % 120
        if port in found and len(found[port]) < 3:
            found[port].append(path)
    assert all(len(v) == 3 for v in found.values()), "the search found paths for 8800 and 8850"
    saved = fd.FRONT_PORT
    try:
        for configured in (saved, 8850):  # the scratch port of this run, and a port inside the span (a number only)
            fd.FRONT_PORT = configured
            for path in found[8800]:
                assert serve.port_for(path) == 8920, (configured, path, serve.port_for(path))
            for path in found[8850]:  # re-check finding 4: only 8800 is skipped, whatever the environment says
                assert serve.port_for(path) == 8850, (configured, path)
    finally:
        fd.FRONT_PORT = saved


def test_f6_the_records_folder_is_0700_and_its_files_0600():
    world = World()
    try:
        servers = world.state / "servers"
        servers.chmod(0o755)  # as an older version, or a default umask, left it
        fd = FD()
        door = fd.FrontDoor(SERVE(), state=world.state)
        repo = TMP / "perm" / "permproj"
        repo.mkdir(parents=True)
        port = free_port()
        rec = door.write_record(SimpleNamespace(slug="permproj", name="permproj", repo=repo), port, "127.0.0.1")
        assert rec, "the record was written"
        assert mode_of(servers) == 0o700, oct(mode_of(servers))
        assert mode_of(servers / f"{port}.json") == 0o600, oct(mode_of(servers / f"{port}.json"))
        door.ensure_key()
        assert mode_of(servers / "front.key") == 0o600
    finally:
        world.close()


# ── after the re-check (14 Nov 2026, P/urls/REVIEW-urls-recheck.md): the door must work with many boards ───────────
# Each test below failed on the front door as delivered after the first fixes. Port safety is as above: every port is
# the operating system's. `FakeBoards` are tiny processes whose command line ends in `serve.py` and that only listen:
# to the door they are boards of ours, and twenty of them cost nothing. `Stubs` puts a logging `lsof` and `ps` in
# front of the real ones on PATH, so that a test can count how many ran.
FAKE_SERVE = ("import socket, sys, time\ns = socket.socket()\ns.bind(('127.0.0.1', int(sys.argv[1])))\ns.listen(64)\n"
              "time.sleep(900)\n")


class Stubs:
    """`lsof` and `ps` that log every run (one line each in a file) and then run the real one. A flag file makes lsof
    exit 1 (`fail`) or hang (`slow`). They are found first on this process's PATH, which is what the door's subprocess
    calls use; `close()` puts PATH back."""

    def __init__(self) -> None:
        self.dir = TMP / f"stubs-{random.randrange(10**9)}"
        self.dir.mkdir()
        self.log = {"lsof": self.dir / "lsof.log", "ps": self.dir / "ps.log"}
        self.fail, self.slow = self.dir / "fail", self.dir / "slow"
        for tool, real in (("lsof", "/usr/sbin/lsof"), ("ps", "/bin/ps")):
            guard = (f'if [ -f "{self.fail}" ]; then exit 1; fi\nif [ -f "{self.slow}" ]; then sleep 30; fi\n'
                     if tool == "lsof" else "")
            script = self.dir / tool
            script.write_text(f'#!/bin/sh\necho "$$ $*" >> "{self.log[tool]}"\n{guard}exec {real} "$@"\n')
            script.chmod(0o755)
        self.saved = os.environ["PATH"]
        os.environ["PATH"] = f"{self.dir}:{self.saved}"

    def runs(self, tool: str = "lsof") -> int:
        try:
            return len(self.log[tool].read_text().splitlines())
        except OSError:
            return 0

    def close(self) -> None:
        os.environ["PATH"] = self.saved


class FakeBoards:
    """`count` processes that listen on free ports, and a live record for each in `world`."""

    def __init__(self, world: World, count: int, prefix: str = "board") -> None:
        script = TMP / "fakeboards" / "serve.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(FAKE_SERVE)
        self.world, self.procs, self.records = world, [], []
        for i in range(count):
            self.add(f"{prefix}{i:02d}", script)

    def add(self, slug: str, script: Path | None = None) -> dict:
        script = script or TMP / "fakeboards" / "serve.py"
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-B", str(script), str(port)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        PROCESSES.append(proc)
        self.procs.append(proc)
        assert wait_for(lambda: socket_accepts(port), 10) is not None, f"fake board {slug} never listened"
        (self.world.state / "boards" / slug).mkdir(exist_ok=True)
        repo = self.world.state.parent / "repos" / slug
        repo.mkdir(parents=True, exist_ok=True)
        rec = {"v": 1, "service": "status-board", "slug": slug, "name": slug, "repo": str(repo), "main_repo": None,
               "port": port, "host": "127.0.0.1", "pid": proc.pid, "started": now_iso(), "plugin_version": "9.9.9"}
        (self.world.state / "servers" / f"{port}.json").write_text(json.dumps(rec))
        self.records.append(rec)
        return rec

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def socket_accepts(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def timed(door: Door, path: str) -> tuple[float, int, str]:
    began = time.perf_counter()
    status, _, body = door.request(path)
    return time.perf_counter() - began, status, body


def burst(door: Door, path: str, count: int) -> tuple[float, list[int]]:
    """`count` requests sent at once (as a page of another site could send them with image tags); (seconds, statuses)."""
    out: list[int] = []
    lock = threading.Lock()

    def one() -> None:
        try:
            status = door.request(path)[0]
        except OSError:
            status = 0
        with lock:
            out.append(status)

    threads = [threading.Thread(target=one) for _ in range(count)]
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return time.perf_counter() - began, out


def test_g1_a_challenge_is_answered_at_once_without_the_rows_and_a_starting_board_believes_the_door():
    """Re-check finding 1: the proof waited for the rows, and the rows ran lsof and ps for every live record; with
    about ten boards a board's probe (0.5 s) gave up and the true door was taken as silent."""
    world = World()
    boards = FakeBoards(world, 20)
    stubs = Stubs()
    door = Door(world, real_check=True)
    late = None
    try:
        fd = FD()
        door.door.ensure_key()
        took, status, body = timed(door, "/_boards.json?c=" + "ab" * 16)
        data = json.loads(body)
        assert status == 200 and data.get("proof") and "boards" not in data, f"a challenge gets the proof and nothing else: {body[:200]}"
        assert took < 0.15, f"the proof took {took:.3f} s with 20 live records"
        assert stubs.runs("lsof") == 0, "answering a challenge started a check of the listeners"
        probe_began = time.perf_counter()
        assert fd.probe_state(door.port, world.state) == "door"
        probe_took = time.perf_counter() - probe_began
        assert probe_took < 0.2, f"a probe took {probe_took:.3f} s"
        assert "boards" in json.loads(door.request("/_boards.json")[2]), "without a challenge the list is still there"
        late = Board("late-board", front=door.port, home=world.state)  # a board that starts beside the true door
        lines = banner_of(late)
        assert lines[0] == f"late-board board → http://localhost:{door.port}/late-board", f"the door was not believed: {lines}"
    finally:
        if late:
            late.stop()
        door.close()
        stubs.close()
        boards.close()
        world.close()


def test_g2_the_list_with_20_boards_is_fast_cold_and_warm_and_no_burst_of_requests_starts_more_than_one_check():
    """Re-check finding 2: a page of another site could make the door run hundreds of lsof (an image tag per request,
    no lock on the cache); some connections were reset."""
    world = World()
    boards = FakeBoards(world, 20)
    stubs = Stubs()
    door = Door(world, real_check=True)
    started = time.monotonic()
    second = None
    try:
        cold, status, body = timed(door, "/")
        assert status == 200 and cold < 0.3, f"the cold list took {cold:.3f} s"
        assert "not checked yet" in body and "not a board" not in body, "a board never seen is 'not checked yet'"
        assert wait_for(lambda: door.request("/")[2].count('class="up"') == 20, 10) is not None, "all 20 end up listed as up"
        warm, _, body = timed(door, "/")
        assert warm < 0.1 and "not checked yet" not in body, f"the warm list took {warm:.3f} s"
        runs_after_cold = stubs.runs("lsof")
        # The code promises a rate (LISTEN_MIN_INTERVAL_S: at most one check a second), not a count: on a loaded machine the
        # wait above lasts seconds, and one check a second is what it is allowed. The bound is 1 + ceil(elapsed), 15 Nov 2026.
        allowed = 1 + math.ceil(time.monotonic() - started)
        assert 1 <= runs_after_cold <= allowed, f"the first list ran lsof {runs_after_cold} times in {allowed - 1} s"
        seconds, statuses = burst(door, "/", 100)
        assert statuses.count(200) == 100, f"100 at once, warm: {sorted(set(statuses))} in {seconds:.2f} s"
        assert stubs.runs("lsof") == runs_after_cold, "a warm burst ran lsof"
        # a second door, cold, in the same home: 100 at once start one check, not one per request
        second = Door(world, real_check=True)
        before = stubs.runs("lsof")
        second_started = time.monotonic()
        seconds, statuses = burst(second, "/", 100)
        assert statuses.count(200) == 100, f"100 at once, cold: {sorted(set(statuses))} in {seconds:.2f} s"
        assert wait_for(lambda: second.request("/")[2].count('class="up"') == 20, 10) is not None
        ran = stubs.runs("lsof") - before
        allowed = 1 + math.ceil(time.monotonic() - second_started)  # one check, then at most one a second (same promise)
        assert 1 <= ran <= allowed, f"100 requests at once on a cold door ran lsof {ran} times in {allowed - 1} s"
        assert stubs.runs("ps") <= 6, f"ps ran {stubs.runs('ps')} times: one per check, not one per record"
    finally:
        if second:
            second.close()
        door.close()
        stubs.close()
        boards.close()
        world.close()


def test_g2_a_slow_or_failing_lsof_keeps_the_last_known_answer_and_a_new_board_is_not_checked_yet():
    world = World()
    boards = FakeBoards(world, 3)
    stubs = Stubs()
    door = Door(world, real_check=True)
    try:
        listeners = door.door.listeners
        listeners.refresh_s, listeners.min_interval_s, listeners.retry_s = 0.3, 0.05, 0.3  # tests only: a refresh every moment
        assert wait_for(lambda: door.request("/")[2].count('class="up"') == 3, 10) is not None
        stubs.fail.touch()  # lsof now fails
        time.sleep(1.2)
        body = door.request("/")[2]
        assert body.count('class="up"') == 3 and "not a board" not in body, "a failing lsof must not turn a board into 'not a board'"
        newcomer = boards.add("newcomer")
        body = door.request("/")[2]
        assert "not checked yet" in body and "not a board" not in body, body[-500:]
        status, _, page_body = door.request("/newcomer")
        assert status == 503 and "not checked yet" in page_body and "not a board" not in page_body, (status, page_body[-300:])
        stubs.fail.unlink()
        stubs.slow.touch()  # lsof now hangs: the pages stay fast and keep what they knew
        time.sleep(0.8)
        took, _, body = timed(door, "/")
        assert took < 0.3 and body.count('class="up"') == 3, f"{took:.3f} s, {body.count('class=\"up\"')} up"
        stubs.slow.unlink()
        assert wait_for(lambda: door.request(f"/newcomer")[0] == 302, 40) is not None, "once lsof answers again, the new board is up"
        assert newcomer["port"]
    finally:
        door.close()
        stubs.close()
        boards.close()
        world.close()


class Relay:
    """A program on one port that forwards every GET to a true door on another port and sends its answer back."""

    def __init__(self, target: int) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                conn = http.client.HTTPConnection("127.0.0.1", target, timeout=5)
                conn.request("GET", self.path)
                data = conn.getresponse().read()
                conn.close()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_g3_the_proof_is_bound_to_the_port_so_a_relay_to_a_true_door_is_not_believed():
    world = World()
    door = Door(world)
    relay = Relay(door.port)
    try:
        fd = FD()
        key = door.door.ensure_key()
        assert fd.proof_for(key, "ab" * 16, 1234) != fd.proof_for(key, "ab" * 16, 1235), "the proof says which port it is for"
        assert fd.probe_state(door.port, world.state) == "door"
        assert fd.probe_state(relay.port, world.state) == "other", "a relay forwards the proof of another port"
        assert fd.address_for("x", relay.port, world.state) is None
    finally:
        relay.close()
        door.close()
        world.close()


def test_g4_port_for_skips_only_8800_and_a_door_port_inside_the_boards_range_is_refused():
    """Re-check finding 4: with the door's port set inside the span, a path that hashed to it got 8920 in a process
    with the variable and its own port in one without; a hook and the server it revives would disagree."""
    import zlib

    fd, serve = FD(), SERVE()
    assert hasattr(serve, "front_port_problem"), "serve.py has no check of the door's port"
    found = {8800: [], 8850: [], 8920: []}
    i = 0
    while min(len(v) for v in found.values()) < 2 and i < 400000:
        i += 1
        path = Path(f"/Users/x/project-{i}")
        port = 8800 + zlib.crc32(str(path).encode()) % 120
        if port in found and len(found[port]) < 2:
            found[port].append(path)
    saved = fd.FRONT_PORT
    try:
        for configured in (saved, 8800, 8850, 8920, 8921):
            fd.FRONT_PORT = configured
            for path in found[8800]:
                assert serve.port_for(path) == 8920, (configured, path)
            for path in found[8850]:
                assert serve.port_for(path) == 8850, f"port_for depends on the environment: {configured} gave {serve.port_for(path)}"
    finally:
        fd.FRONT_PORT = saved
    for bad in (8801, 8850, 8919, 8920):
        line = serve.front_port_problem(bad)
        assert line and str(bad) in line and "\n" not in line and "8801" in line and "8920" in line, (bad, line)
    for fine in (8800, 8921, 29999, 80):
        assert serve.front_port_problem(fine) is None, fine


def test_g4_a_server_started_with_a_door_port_inside_the_range_says_so_in_one_line_and_binds_nothing():
    """Run only when serve.py has the check: without it this process would try to hold a port in 8800 to 8920."""
    serve = SERVE()
    assert hasattr(serve, "front_port_problem"), "serve.py has no check of the door's port: not started, it would bind"
    repo = TMP / "refused-door" / "refusedproj"
    repo.mkdir(parents=True)
    port = free_port()
    env = {k: v for k, v in env_for().items() if k != "STATUS_BOARD_FRONT_PORT"}
    env["STATUS_BOARD_FRONT_PORT"] = "8850"  # a number only: serve.py must refuse before it binds anything
    done = subprocess.run([sys.executable, "-B", str(SERVE_PY), "--repo", str(repo), "--port", str(port)], env=env,
                          capture_output=True, text=True, timeout=30, cwd=str(TMP))
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    assert done.returncode == 2 and len(lines) == 1 and "STATUS_BOARD_FRONT_PORT=8850" in lines[0], (done.returncode, lines)
    assert not socket_accepts(port), "the board started anyway"
    assert not (HOME / "servers" / f"{port}.json").exists(), "a refused start wrote a record"


# ── after the second re-check (15 Nov 2026, REVIEW-urls-recheck2.md, finding 1): the check must outlive an exception ──
def test_g5_a_check_that_raises_does_not_end_the_thread_and_a_dead_thread_is_started_again():
    fd = FD()
    calls: list[int] = []

    def raising_once(keys):
        calls.append(len(calls))
        if len(calls) == 1:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")  # a ps line that is not UTF-8
        return {key: True for key in keys}

    listeners = fd.Listeners(check=raising_once)
    listeners.min_interval_s, listeners.retry_s = 0.05, 0.2  # tests only
    key = (os.getpid(), 1111)
    try:
        assert listeners.ask(key) is None
        assert wait_for(lambda: listeners.ask(key) is True, 6) is not None, "the check after the one that raised never ran"
        assert len(calls) >= 2 and listeners._thread.is_alive()
    finally:
        listeners.stop()
    # a thread that died anyway (a BaseException nothing catches) is started again by the next request
    died: list[int] = []

    def kills_the_thread(keys):
        died.append(1)
        if len(died) == 1:
            raise SystemExit
        return {k: True for k in keys}

    again = fd.Listeners(check=kills_the_thread)
    again.min_interval_s, again.retry_s = 0.05, 0.2
    try:
        again.ask((os.getpid(), 2222))
        assert wait_for(lambda: again._thread is not None and not again._thread.is_alive(), 6) is not None, "the thread did not die"
        assert wait_for(lambda: again.ask((os.getpid(), 3333)) is True, 6) is not None, "a new request did not start the thread again"
    finally:
        again.stop()


def test_g5_output_that_is_not_utf8_is_read_not_raised():
    fd = FD()
    out = fd._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok\\n')"], 10)
    assert out is not None and "ok" in out, out


# ── release 2.3.1: no response of the front door carries a path ───────────────────────────────────────────────────────
# A finding of the automated review of 15 Nov 2026 (11:39), decided by the owner at 16:40: the door shows each board's name, port
# and whether it is up, and never an absolute path (a project's, a main checkout's, the plugin's, the status-board home, the home
# folder of the account). Inside the process the rows keep their paths: `resolve`, the election, board_link.py use them.
ABS_PATH = re.compile(r"(?<![\w:/.%-])/(?:Users|home|private|tmp|var|opt|usr|etc|Volumes|root|Applications|Library)/[\w.@~-]")


def paths_world() -> tuple[World, Path, Path]:
    """Boards of every kind the door answers about: up, down, a worktree and its main checkout, two projects of one name, a
    prefix that matches several, and a board folder with no record."""
    world = World()
    base = world.state.parent
    deep = base / "deep" / "nested" / "alpha"
    main, linked = base / "wt" / "mainproj", base / "wt" / "linked-tree"
    for folder in (deep, main, linked, base / "beta", base / "a" / "gamma", base / "b" / "gamma"):
        folder.mkdir(parents=True)
    world.add("alpha", name="Alpha", repo=deep)
    world.add("beta", name="Beta", repo=base / "beta", up=False)
    world.add("gamma", repo=base / "a" / "gamma", up=False, started=now_iso(-7200))
    world.add("gamma", repo=base / "b" / "gamma", up=False, started=now_iso(-60), main_repo=str(main))
    world.add("notes-a")
    world.add("notes-b")
    world.add("mainproj", repo=main)
    world.add("linked-tree", repo=linked, main_repo=str(main))
    world.add("ghost", record=False)

    def drain(listener: socket.socket) -> None:  # the door connects to every board that is up on every request: accept them
        while True:
            try:
                conn, _ = listener.accept()
                conn.close()
            except OSError:
                return

    for listener in world.sockets:
        threading.Thread(target=drain, args=(listener,), daemon=True).start()
    return world, main, linked


def private_strings(world: World) -> list[str]:
    found = {str(world.state), str(world.state.parent), str(TMP), str(FAKE_HOME), str(PLUGIN), str(BOARD_DIR), str(HOME)}
    for path in (world.state / "servers").glob("*.json"):
        rec = json.loads(path.read_text())
        found.update(str(v) for v in (rec["repo"], rec.get("main_repo")) if v)
    found.add(str(Path.home()))
    found.add(pwd.getpwuid(os.getuid()).pw_dir)
    return sorted(s for s in found if len(s) > 4)


def raw_request(port: int, payload: bytes) -> tuple[int, dict, str]:
    """A request the standard library's own error answers are built for: sent as bytes, read to the end."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
    return int(lines[0].split()[1]), headers, body.decode("utf-8", "replace")


def every_answer(door: Door, foreign: Door) -> list[tuple[str, int, tuple]]:
    host = f"127.0.0.1:{door.port}"
    return [
        ("GET /", 200, door.request("/")),
        ("HEAD /", 200, door.request("/", method="HEAD")),
        ("GET /_boards.json (no challenge)", 200, door.request("/_boards.json")),
        ("GET /_boards.json?c= (a challenge)", 200, door.request("/_boards.json?c=" + "ab" * 16)),
        ("GET /alpha (302)", 302, door.request("/alpha")),
        ("GET /nope (404)", 404, door.request("/nope")),
        ("GET /notes (300, several boards)", 300, door.request("/notes")),
        ("GET /gamma (300, two projects of one name)", 300, door.request("/gamma")),
        ("GET /beta (503, not running)", 503, door.request("/beta")),
        ("GET /ghost (503, no record)", 503, door.request("/ghost")),
        ("GET /alpha (503, a port that is not a board)", 503, foreign.request("/alpha")),
        ("GET / with a Host that is not ours (403)", 403, door.request("/", host="evil.example")),
        ("POST / (405)", 405, door.request("/", method="POST", body=b"x")),
        ("an unsupported method (501)", 501, raw_request(door.port, f"FOO / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())),
        ("a request line of 70 000 bytes (414)", 414, raw_request(door.port, b"GET /" + b"a" * 70000 + f" HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())),
        ("a request that is not HTTP (400)", 400, raw_request(door.port, b"\x00\x01\x02 \x03\r\n\r\n")),
    ]


def test_p1_no_response_of_the_door_carries_an_absolute_path_on_any_route_or_status():
    world, _main, _linked = paths_world()
    door, foreign = Door(world), Door(world, verify=lambda row: False)
    try:
        private = private_strings(world)
        assert any("deep" in p for p in private) and any(str(PLUGIN) == p for p in private), private
        for label, status, (got, headers, body) in every_answer(door, foreign):
            assert got == status, (label, got, status, body[:200])
            text = "\n".join(f"{k}: {v}" for k, v in headers.items()) + "\n" + body
            for secret in private:
                assert secret not in text, f"{label}: carries {secret!r}: {text[:400]}"
            assert not ABS_PATH.search(text), f"{label}: carries an absolute path: {ABS_PATH.search(text).group(0)} in {text[:400]}"
    finally:
        foreign.close()
        door.close()
        world.close()


def test_p2_the_json_rows_hold_only_names_ports_state_and_the_name_of_the_main_project():
    world, _main, _linked = paths_world()
    door = Door(world)
    try:
        status, headers, body = door.request("/_boards.json")
        data = json.loads(body)
        assert status == 200 and data["service"] == FD().SERVICE and data["v"] == 1 and data["front_port"] == door.port, data
        want = {"slug", "name", "port", "host", "started", "up", "known", "foreign", "unchecked", "worktree_of"}
        by_slug = {}
        for row in data["boards"]:
            assert set(row) == want, (sorted(row), sorted(want ^ set(row)))
            by_slug.setdefault(row["slug"], []).append(row)
        assert by_slug["linked-tree"][0]["worktree_of"] == "mainproj", by_slug["linked-tree"]
        assert by_slug["mainproj"][0]["worktree_of"] is None and by_slug["alpha"][0]["worktree_of"] is None
        assert by_slug["alpha"][0]["up"] is True and by_slug["alpha"][0]["name"] == "Alpha" and by_slug["alpha"][0]["port"] > 0
        assert by_slug["beta"][0]["up"] is False and by_slug["ghost"][0]["known"] is False, by_slug
        assert len(by_slug["gamma"]) == 2, "two projects of one name are two rows"
    finally:
        door.close()
        world.close()


def test_p3_inside_the_process_the_rows_keep_their_paths():
    """board_link.py calls FrontDoor.resolve in its own process and compares row["repo"]; the election and the revive hook use the
    rows too. Only what leaves over the socket changes."""
    world, main, linked = paths_world()
    door = Door(world)
    try:
        rows = {(r["slug"], r["repo"]): r for r in door.door.rows()}
        assert ("alpha", str(world.state.parent / "deep" / "nested" / "alpha")) in rows, sorted(rows)
        kind, found = door.door.resolve("linked-tree")
        assert kind == "one" and found[0]["repo"] == str(linked) and found[0]["main_repo"] == str(main), (kind, found)
        kind, found = door.door.resolve("gamma")
        assert kind == "clash" and len({r["repo"] for r in found}) == 2, (kind, found)
    finally:
        door.close()
        world.close()


def test_p4_the_pages_that_used_a_path_still_help_without_one():
    world, main, _linked = paths_world()
    door = Door(world)
    try:
        _, _, down = door.request("/beta")
        text = html.unescape(down)
        assert "Beta" in down or "beta" in down, down
        assert "next Claude Code session in that project" in down, down
        assert 'python3 "<the plugin\'s status-board folder>/serve.py" --repo "<the project\'s folder>" &' in text, text
        _, _, clash = door.request("/gamma")
        assert clash.count("<li>") == 2 and "worktree of mainproj" in clash and "not a worktree" in clash, clash
        assert clash.count("started ") == 2 and "was on " in clash, clash
        assert "share one board folder" in clash, clash
        _, _, listing = door.request("/")
        assert "worktree of mainproj" in listing and listing.count("<td>gamma</td>") == 2, listing
    finally:
        door.close()
        world.close()


def test_p5_the_short_addresses_resolve_as_before_old_door_and_new_side_by_side():
    if OLD_PLUGIN is None:
        raise NotRun("needs --old-plugin <the plugin directory of 2.3.0>")
    spec = importlib.util.spec_from_file_location("front_door_2_3_0", OLD_PLUGIN / "status-board" / "front_door.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    world, _main, _linked = paths_world()
    FD().make_key(world.state)
    new_door, old_door = Door(world), Door(world, module=old)
    try:
        names = ["alpha", "ALPHA", "al", "beta", "gamma", "notes", "notes-a", "mainproj", "main", "linked-tree", "ghost", "nope", "a/b", "my%20app", "alpha/"]
        for name in names:
            a = new_door.request("/" + name)
            b = old_door.request("/" + name)
            ports_a = re.sub(r":\d+", ":PORT", a[1].get("location", ""))
            ports_b = re.sub(r":\d+", ":PORT", b[1].get("location", ""))
            assert (a[0], ports_a) == (b[0], ports_b), (name, a[0], b[0], a[1].get("location"), b[1].get("location"))
            if a[1].get("location"):
                assert a[1]["location"].split(":")[-1].split("/")[0] == b[1]["location"].split(":")[-1].split("/")[0], name
        for name in names:
            kind_new, found_new = new_door.door.resolve(name)
            kind_old, found_old = old_door.door.resolve(name)
            assert kind_new == kind_old and [(r["slug"], r["port"], r["repo"]) for r in found_new] == [(r["slug"], r["port"], r["repo"]) for r in found_old], name
        # what the old one sent and the new one does not: the paths
        old_rows = json.loads(old_door.request("/_boards.json")[2])["boards"]
        assert any("repo" in row for row in old_rows), "the stand-in for the old door must be the one that sent paths"
        # a board of either version probes a door of either version, and believes it
        assert FD().probe_state(new_door.port, world.state) == "door"
        assert old.probe_state(new_door.port, world.state) == "door", "an old board probing the new door"
        assert FD().probe_state(old_door.port, world.state) == "door", "a new board probing the old door"
        assert old.probe_state(old_door.port, world.state) == "door"
    finally:
        old_door.close()
        new_door.close()
        world.close()


def test_p6_nothing_in_the_plugin_reads_the_doors_rows_over_the_socket():
    """The door's JSON is read by one thing, a board's probe, for its proof; no file reads `boards` or `repo` from it."""
    mentions = []
    for path in PLUGIN.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "tests" in path.parts or path.suffix not in (".py", ".md", ".json", ".tsx", ".ts", ".sh", ".html", ".js"):
            continue
        if "_boards.json" in path.read_text(errors="replace"):
            mentions.append(path.relative_to(PLUGIN).as_posix())
    assert mentions == ["status-board/front_door.py"], mentions
    source = (BOARD_DIR / "front_door.py").read_text()
    probe = source[source.index("def probe_state"): source.index("def probe(")]
    assert '"boards"' not in probe and '"repo"' not in probe and "'repo'" not in probe, "the probe reads only the service marker and the proof"


def test_p7_the_readme_says_what_the_list_shows_and_that_it_holds_no_paths():
    text = " ".join((PLUGIN / "README.md").read_text().split())
    assert re.search(r"front door[^.]*lists? (?:every|each) board by (?:its )?name, port", text, re.I) or "name, port and whether it is up" in text, \
        "README.md does not say what the front door lists"
    assert re.search(r"no (?:folder|path)s? (?:of a project|is shown|are shown|leaves)|never (?:shows|sends|gives) (?:a|an|any) (?:absolute )?(?:folder )?path|holds no (?:folder )?paths?", text, re.I) or "no project path" in text, \
        "README.md does not say the door shows no paths"


def main() -> int:
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and ARGS.only in n)
    failed = 0
    not_run = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except NotRun as why:
                not_run += 1
                print(f"NOT RUN  {name}: {why}")
            except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
                failed += 1
                reason = " ".join(str(exc).split())[:300] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        for proc in PROCESSES:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for sock in SOCKETS:
            try:
                sock.close()
            except OSError:
                pass
        left = sum(1 for proc in PROCESSES if proc.poll() is None)
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed - not_run} passed, {failed} failed, processes left running: {left}"
          + (f", {not_run} not run (they need --old-plugin)" if not_run else "") + f", plugin under test: {PLUGIN}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
