#!/usr/bin/env python3
"""The session-start hook asks the board for its proof, and restarts only a board of its own that cannot give it.

    python3 tests/test_revive_proof.py --plugin <checkout of the plugin> [--old-plugin <a checkout of 2.3.1>]

Before this release `hooks/revive-board.py` took any listener that answered `GET /` with the board page's title for the board and did nothing. So a 2.3.1
board, a 2.4.0 board with no usable key and one holding an older key were never restarted, though the opener sends the owner to "a session in this
project", and a program that took a stopped board's port and served the title kept the real board down, silently. Now the hook asks `GET /_proof` and
restarts a board of its OWN that cannot give the proof; anything else on the port is reported, never stopped.

THE RULE above every other, and what these tests hold: a process is signalled only when all five facts are established, and again right before the
signal; anything that cannot be established means report, stop nothing. The five facts: F1 the record `servers/<port>.json` (a regular file of this
account in a private folder; this project's repo; a loopback board); F2 the pid is alive; F3 it is this account's; F4 it is the process that listens on
the port; F5 its argv is `python .../status-board/serve.py` with a `--repo` that is this project's folder.

What runs here: every test has a temporary HOME and STATUS_BOARD_HOME and ports the operating system gave, each checked to be OUTSIDE 8800 to 8920 before
anything starts. The hook derives its port from the project's path, which is always inside that range, so the hook runs from a COPY of the plugin in which
`port_for` returns the test's own port (and the front door's port is the test's own too). The older server is the REAL `serve.py` of 2.3.1 (a second
checkout, `--old-plugin`), started on that port; the newer server is a stand-in beside the copy's `serve.py` name that answers `/_proof` as the board's own
server does (this tree has the hook and the opener, not the server). The other stand-ins are scripts named `status-board/serve.py` in a folder of their own
that serve the title and misbehave on the proof in one way each. Every process is started detached (a grandchild, so that it is adopted and reaped
as a real board is), and every process a test started is stopped at the end. The one function that signals is replaced or wrapped, so that each test
says how many signals were sent and to which pid.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
ap.add_argument("--old-plugin", help="a checkout of 2.3.1: its real serve.py is the older server")
args, _ = ap.parse_known_args()
PLUGIN = Path(args.plugin).resolve()
OLD = Path(args.old_plugin).resolve() if args.old_plugin else None
TMP = Path(tempfile.mkdtemp(prefix="revive-proof-")).resolve()
HOOK = PLUGIN / "hooks" / "revive-board.py"
OPENER = PLUGIN / "status-board" / "open_board.py"
SAFE_PATH = f"/usr/bin:/bin:/usr/sbin:/sbin:{Path(sys.executable).parent}"
PIDS: list[int] = []  # every process a test started, and nothing else: only these are ever signalled by the tests
PORTS: list[int] = []


class Skip(Exception):
    pass


# ── ports, processes ─────────────────────────────────────────────────────────────────────────────────────────────────
def free_port() -> int:
    """A port the operating system gave, never one of the owner's range."""
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if not 8800 <= port <= 8920:
            PORTS.append(port)
            return port


def assert_outside_the_owners_range(*ports: int) -> None:
    for port in ports:
        assert not 8800 <= port <= 8920, f"port {port} is in the owner's range: nothing is started"


LAUNCH = ("import json, subprocess, sys\n"
          "p = subprocess.Popen(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL, stdout=open(sys.argv[2], 'ab'), stderr=subprocess.STDOUT,\n"
          "                     start_new_session=True, close_fds=True, cwd=sys.argv[3], env=json.loads(sys.argv[4]))\n"
          "print(p.pid)\n")


def spawn(argv: list[str], env: dict, log: Path, cwd: Path) -> int:
    """Start a process detached, as a grandchild: it is adopted by the system and reaped when it ends, as a board the hook started is."""
    done = subprocess.run([sys.executable, "-c", LAUNCH, json.dumps(argv), str(log), str(cwd), json.dumps(env)], capture_output=True, text=True, timeout=30)
    pid = int(done.stdout.strip())
    PIDS.append(pid)
    return pid


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for(condition, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def started_by_the_hook() -> list[int]:
    """Boards the hook under test started and detached: they are not in PIDS, but their command line holds this run's own temporary folder."""
    shown = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30).stdout
    found = []
    for line in shown.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit() and str(TMP) in command and int(pid_text) != os.getpid():
            found.append(int(pid_text))
    return found


def stop_everything() -> int:
    """Stop what the tests started, and the boards the hook started inside this run's own temporary folder (only those pids); the number still running afterwards."""
    for pid in started_by_the_hook():
        if pid not in PIDS:
            PIDS.append(pid)
    for pid in PIDS:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    return sum(1 for pid in PIDS if alive(pid))


# ── the stand-in servers ──────────────────────────────────────────────────────────────────────────────────────────────
SHIM = r'''
import json, os, secrets, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib, hmac

STATE = Path(os.environ.get("STATUS_BOARD_HOME") or (Path.home() / ".claude" / "status-board"))


def port_for(repo):
    return int(os.environ["REVIVE_TEST_PORT"])


def slug_for(repo):
    return Path(repo).name.lower().replace(" ", "-")


def usable_key(path):
    try:
        st = os.lstat(path)
        if st.st_uid != os.getuid() or (st.st_mode & 0o077) or not (st.st_mode & 0o100000) or st.st_size != 32:
            return None
        return Path(path).read_bytes()
    except OSError:
        return None


def main():
    argv = sys.argv[1:]
    repo = Path(argv[argv.index("--repo") + 1]).resolve()
    port = int(argv[argv.index("--port") + 1])
    mode = os.environ.get("REVIVE_TEST_SHIM", "normal")
    servers = STATE / "servers"
    servers.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = servers / "board.key"
    if not os.path.lexists(key_path):
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, os.urandom(32))
        os.close(fd)
    run = secrets.token_hex(8)
    held = {"key": usable_key(key_path)}  # the key it started with; a server that has none reads the file again (a key the owner closes heals it)
    with open(os.environ["REVIVE_TEST_STARTS"], "a") as fh:
        fh.write(f"{os.getpid()} {mode}\n")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body, kind="text/html"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self.send(200, b"<!doctype html><title>status board</title>")
            if self.path.startswith("/_proof?c=") and mode != "noproof":
                challenge = self.path.split("=", 1)[1]
                if held["key"] is None and mode != "nokey":
                    held["key"] = usable_key(key_path)
                key = held["key"] if mode != "nokey" else None
                if key is None:
                    return self.send(503, b'{"ok": false}', "application/json")
                digest = hmac.new(key, f"proof:v2:{port}:{run}:{challenge}".encode(), hashlib.sha256).hexdigest()
                return self.send(200, json.dumps({"run": run, "proof": digest}).encode(), "application/json")
            return self.send(404, b"not found", "text/plain")

    record = {"v": 1, "service": "status-board", "slug": repo.name.lower(), "name": repo.name, "repo": str(repo), "main_repo": None, "port": port,
              "host": "127.0.0.1", "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "plugin_version": "2.4.0"}
    tmp = servers / f".{port}.json.tmp"
    tmp.write_text(json.dumps(record))
    os.chmod(tmp, 0o600)
    os.replace(tmp, servers / f"{port}.json")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''

STANDIN = r'''
import hashlib, hmac, json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

mode = os.environ.get("STANDIN_MODE", "title")
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
STATE = Path(os.environ["STATUS_BOARD_HOME"])
argv = sys.argv[1:]
port = int(argv[argv.index("--port") + 1])
seen = {"proofs": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body, kind="text/plain", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with open(os.environ["STANDIN_REQUESTS"], "a") as fh:
            fh.write(f"{time.time():.3f} {self.path}\n")
        if self.path == "/":
            return self.send(200, b"<!doctype html><title>status board</title>", "text/html")
        if not self.path.startswith("/_proof?c="):
            return self.send(404, b"not found")
        seen["proofs"] += 1
        challenge = self.path.split("=", 1)[1]
        run = "0123456789abcdef"
        if mode == "hang":
            time.sleep(12)
            return self.send(404, b"late")
        if mode == "redirect":
            return self.send(302, b"", extra={"Location": "/"})
        if mode == "large":
            return self.send(200, json.dumps({"run": run, "proof": "0" * 64, "pad": "x" * 1_000_000}).encode(), "application/json")
        if mode == "nokey" or (mode == "flaky" and seen["proofs"] == 1):
            return self.send(503, b'{"ok": false}', "application/json")
        if mode == "flaky":
            key = (STATE / "servers" / "board.key").read_bytes()
            digest = hmac.new(key, f"proof:v2:{port}:{run}:{challenge}".encode(), hashlib.sha256).hexdigest()
            return self.send(200, json.dumps({"run": run, "proof": digest}).encode(), "application/json")
        if mode == "wrong":
            return self.send(200, json.dumps({"run": run, "proof": "f" * 64}).encode(), "application/json")
        if mode == "page":
            return self.send(200, b"<!doctype html><title>status board</title>", "text/html")
        return self.send(404, b"not found")


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


# ── a world: a home, a project with a board, a copy of the plugin for the hook ─────────────────────────────────────────
class World:
    def __init__(self, repo_name: str = "shop-app", default_home: bool = False, shim: str = "normal") -> None:
        self.root = Path(tempfile.mkdtemp(dir=TMP, prefix="w-"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.default_home = default_home
        self.state = (self.home / ".claude" / "status-board") if default_home else (self.root / "state")
        self.servers = self.state / "servers"
        self.servers.mkdir(mode=0o700, parents=True)
        os.chmod(self.servers, 0o700)
        self.project = self.root / repo_name
        self.project.mkdir()
        git = ["git", "-C", str(self.project), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.slug = repo_name.lower().replace(" ", "-")
        self.board = self.state / "boards" / self.slug
        self.board.mkdir(parents=True)
        (self.board / "narrative.json").write_text("{}")
        (self.board / "repo.json").write_text(json.dumps({"repo": str(self.project.resolve())}))
        self.port = free_port()
        self.front = free_port()
        assert_outside_the_owners_range(self.port, self.front)  # BEFORE anything is started
        self.signal_log = self.root / "signals.log"
        self.starts = self.root / "starts.log"
        self.requests = self.root / "requests.log"
        for path in (self.signal_log, self.starts, self.requests):
            path.write_text("")
        self.shim = shim
        self.plugin = self.root / "plugin"
        self.make_plugin(self.plugin)

    # the copy of the plugin the hook runs from: its serve.py is the stand-in newer server, its port_for is the test's own, its signal is logged
    def make_plugin(self, target: Path) -> None:
        shutil.copytree(PLUGIN / "hooks", target / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(PLUGIN / "status-board", target / "status-board", ignore=shutil.ignore_patterns("__pycache__"))
        (target / "status-board" / "serve.py").write_text(SHIM)
        hook = target / "hooks" / "revive-board.py"
        text = hook.read_text()
        guard = 'if __name__ == "__main__":'
        assert text.count(guard) == 1
        hook.write_text(text.replace(guard, "_real_signal_process = signal_process\n\n\ndef signal_process(*a, **k):\n"
                                            "    with open(os.environ['REVIVE_TEST_SIGNAL_LOG'], 'a') as fh:\n"
                                            "        fh.write(f'{a[0]}\\n')\n"
                                            "    return _real_signal_process(*a, **k)\n\n\n" + guard))

    def patch_hook(self, old: str, new: str) -> None:
        hook = self.plugin / "hooks" / "revive-board.py"
        text = hook.read_text()
        assert text.count(old) == 1, old
        hook.write_text(text.replace(old, new))

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.home), "PATH": SAFE_PATH, "REVIVE_TEST_PORT": str(self.port), "STATUS_BOARD_FRONT_PORT": str(self.front),
               "REVIVE_TEST_SIGNAL_LOG": str(self.signal_log), "REVIVE_TEST_STARTS": str(self.starts), "REVIVE_TEST_SHIM": self.shim,
               "STANDIN_REQUESTS": str(self.requests), "PYTHONDONTWRITEBYTECODE": "1", "STATUS_BOARD_FRONT_POLL_S": "0.2"}
        if not self.default_home:
            env["STATUS_BOARD_HOME"] = str(self.state)
        env.update(extra)
        return env

    # ── processes ────────────────────────────────────────────────────────────────────────────────────────────────────
    def start_old_server(self, repo: Path | None = None) -> int:
        """The REAL serve.py of 2.3.1, on this world's port, started the way the hook starts one (detached, `--repo --port`)."""
        if OLD is None:
            raise Skip("needs --old-plugin")
        assert_outside_the_owners_range(self.port, self.front)
        repo = repo or self.project
        pid = spawn([sys.executable, str(OLD / "status-board" / "serve.py"), "--repo", str(repo), "--port", str(self.port)], self.env(), self.root / "old.log", self.board)
        assert wait_for(lambda: listening(self.port) and (self.servers / f"{self.port}.json").exists(), 15), (self.root / "old.log").read_text()[-600:]
        return pid

    def start_standin(self, mode: str, repo: Path | None = None, record: dict | bool = True, name: str = "standin") -> int:
        """A script named `status-board/serve.py` in a folder of its own, serving the title and misbehaving on the proof in one way; with a record (unless
        `record` is False; a dict changes fields of it)."""
        assert_outside_the_owners_range(self.port, self.front)
        folder = self.root / name / "status-board"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "serve.py").write_text(STANDIN)
        repo = repo or self.project
        pid = spawn([sys.executable, str(folder / "serve.py"), "--repo", str(repo), "--port", str(self.port)], self.env(STANDIN_MODE=mode, STATUS_BOARD_HOME=str(self.state)),
                    self.root / f"{name}.log", self.board)
        assert wait_for(lambda: listening(self.port), 10), (self.root / f"{name}.log").read_text()[-600:]
        if record:
            self.write_record(pid, **(record if isinstance(record, dict) else {}))
        return pid

    def write_record(self, pid, **fields) -> Path:
        rec = {"v": 1, "service": "status-board", "slug": self.slug, "name": self.slug, "repo": str(self.project.resolve()), "main_repo": None, "port": self.port,
               "host": "127.0.0.1", "pid": pid, "started": "2026-11-15T00:00:00+01:00", "plugin_version": "2.3.1"}
        rec.update(fields)
        path = self.servers / f"{self.port}.json"
        path.write_text(json.dumps(rec))
        os.chmod(path, 0o600)
        return path

    def make_key(self, mode: int = 0o600, size: int = 32) -> bytes:
        key = os.urandom(size)
        path = self.servers / "board.key"
        if os.path.lexists(path):
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, key)
        os.close(fd)
        os.chmod(path, mode)
        return key

    def title_only_listener(self, with_record: bool = False) -> subprocess.Popen:
        """`python3 -m http.server` serving a page with the board's title: a program that is not a board (its argv has no serve.py)."""
        page = self.root / "page"
        page.mkdir(exist_ok=True)
        (page / "index.html").write_text("<!doctype html><title>status board</title>")
        pid = spawn([sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1", "--directory", str(page)], self.env(), self.root / "http.log", page)
        assert wait_for(lambda: listening(self.port), 10)
        if with_record:
            self.write_record(pid)
        return pid  # type: ignore[return-value]

    # ── the hook ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def run_hook(self, **env_extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(self.plugin / "hooks" / "revive-board.py")], input=json.dumps({"cwd": str(self.project), "hook_event_name": "SessionStart"}),
                              capture_output=True, text=True, timeout=90, env=self.env(**env_extra), cwd=str(TMP))

    def signals(self) -> list[int]:
        return [int(line) for line in self.signal_log.read_text().split()]

    def started_servers(self) -> list[str]:
        return self.starts.read_text().splitlines()

    def home_text(self) -> str:
        """The status-board home as the hook says it: with `~` for the home folder where it is under it."""
        return "~/.claude/status-board" if self.default_home else str(self.state.resolve())

    def lines(self, done: subprocess.CompletedProcess) -> list[str]:
        return [line for line in done.stdout.splitlines() if line.strip()]

    def load_hook(self):
        spec = importlib.util.spec_from_file_location(f"revive_hook_{time.time_ns()}", self.plugin / "hooks" / "revive-board.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def board_line(world: World, text: str) -> str:
    return f'project-init: status board "{world.slug}"{text}'


def restarted_line(world: World, reason: str, pid: int) -> re.Pattern:
    return re.compile(re.escape(board_line(world, f" could not prove it is this project's board ({reason}); stopped its server (pid {pid}) and started it again: ")) + r"http\S+.*")


def key_state(world: World) -> str:
    path = world.servers / "board.key"
    return "absent" if not os.path.lexists(path) else oct(stat.S_IMODE(os.lstat(path).st_mode))


def run_opener(world: World, stub_log: Path) -> subprocess.CompletedProcess:
    """The opener for real, with a stand-in `open` first on PATH that only records its argument."""
    bin_dir = world.root / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / ("open" if sys.platform == "darwin" else "xdg-open")
    stub.write_text(f"#!/bin/sh\necho \"$1\" >> '{stub_log}'\n")
    os.chmod(stub, 0o755)
    env = world.env(PATH=f"{bin_dir}:{SAFE_PATH}")
    return subprocess.run([sys.executable, "-B", str(OPENER), "--start", str(world.project), "--cleanup-after", "5"], capture_output=True, text=True, timeout=60, env=env, cwd=str(TMP))


# ── nothing used to restart a board that cannot prove itself ─────────────────────────────────────────────────────────
def test_old_server_on_port_is_restarted_by_the_hook_and_then_opens():
    """The update from 2.3.1: the REAL 2.3.1 server runs on the port with its record, no key file exists yet (no board of 2.4.0 has started in this home).
    The hook finds the record, the pid, the owner, the listener and the argv all its own, signals it once, starts the new server (which makes the key),
    asks the proof once more and tells the owner. The opener then proves and opens."""
    world = World()
    old = world.start_old_server()
    assert key_state(world) == "absent"
    done = world.run_hook()
    assert done.returncode == 0 and done.stderr == "", (done.returncode, done.stderr)
    (line,) = world.lines(done)
    assert restarted_line(world, "an older version", old).fullmatch(line), line
    assert world.signals() == [old], "exactly one signal, to the old server's pid"
    assert wait_for(lambda: not alive(old), 5), "the old server is gone"
    assert len(world.started_servers()) == 1 and listening(world.port), "one new server answers on the port"
    assert key_state(world) == "0o600", "the new server made the key"
    stub = world.root / "opened.log"
    opened = run_opener(world, stub)
    assert opened.returncode == 0 and opened.stdout == "Opened the board in your browser.\n", (opened.stdout, opened.stderr)
    assert len(stub.read_text().split()) == 1, "the system's opener was given one form"


def test_old_server_is_restarted_when_the_key_exists_too():
    """The next project's first session after the update: the key is there (another board made it); the 2.3.1 board answers `/_proof` with its page or
    a 404: `old`. Its own, so restarted, the same way."""
    world = World()
    world.make_key()
    old = world.start_old_server()
    done = world.run_hook()
    (line,) = world.lines(done)
    assert restarted_line(world, "an older version", old).fullmatch(line), line
    assert world.signals() == [old] and wait_for(lambda: not alive(old), 5)


def test_key_0644_is_reported_not_restarted_then_closed_heals():
    """A key that others can read: no proof can be checked and a restart cannot help (a new server refuses that key too): report, stop nothing, and the
    board is untouched. When the owner closes the key (`chmod 600`) the board reads it again (a server with no key does), and the proof passes with no
    restart and no word."""
    world = World()
    world.make_key(0o644)
    board = spawn([sys.executable, str(world.plugin / "status-board" / "serve.py"), "--repo", str(world.project), "--port", str(world.port)], world.env(),
                  world.root / "board.log", world.board)  # a board that started while the key was 0644: it has no usable key, and reads the file again when it has none
    assert wait_for(lambda: listening(world.port) and (world.servers / f"{world.port}.json").exists(), 10)
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == board_line(world, f' cannot be checked: {world.home_text()}/servers/board.key must be readable by its owner only. Run chmod 600 on it, or '
                                     f'delete it to get a new one, then start a session here again.'), line
    assert world.signals() == [] and alive(board) and key_state(world) == "0o644", "nothing was stopped, and the key was not touched"
    os.chmod(world.servers / "board.key", 0o600)  # the owner closes the key; no restart
    silent = world.run_hook()
    assert silent.stdout == "" and world.signals() == [] and alive(board), (silent.stdout, world.signals())
    assert world.started_servers() == [f"{board} normal"], "and no new server was started"


def test_key_deleted_while_boards_run_wrong_digest_restarts_own_board_once():
    """The key is deleted as the opener's old sentence allowed, and another board makes a new one while this board still holds the old one: its digest is
    not the new key's (`wrong`). It is the project's own recorded server, so it is restarted, once."""
    world = World()
    first = spawn([sys.executable, str(world.plugin / "status-board" / "serve.py"), "--repo", str(world.project), "--port", str(world.port)], world.env(), world.root / "a.log", world.board)
    assert wait_for(lambda: listening(world.port) and (world.servers / f"{world.port}.json").exists(), 10)
    os.unlink(world.servers / "board.key")
    world.make_key()  # another board's start made a new key
    done = world.run_hook()
    (line,) = world.lines(done)
    assert restarted_line(world, "an older key", first).fullmatch(line), line
    assert world.signals() == [first] and wait_for(lambda: not alive(first), 5)
    again = world.run_hook()
    assert again.stdout == "" and world.signals() == [first], "proven now: no second restart"


# ── the rule: never a signal unless all five facts are established ────────────────────────────────────────────────────
def line_o(world: World) -> str:
    return board_line(world, f" not started: port {world.port} is held by a program that is not this board's server. Nothing was stopped; stop that program, "
                             f"then start a session here again.")


def line_u_prefix(world: World) -> str:
    return board_line(world, f": port {world.port} answers like a board, but this could not be confirmed as this project's own server (")


def line_u_suffix(world: World) -> str:
    return f"). Nothing was stopped. If it is yours, stop it (its pid is in {world.home_text()}/servers/{world.port}.json), then start a session here again."


def test_listener_serving_the_title_without_a_record_is_reported_never_signalled():
    world = World()
    pid = world.title_only_listener(with_record=False)
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == line_o(world), line
    assert world.signals() == [] and alive(pid), "a program with no record is never stopped"
    assert key_state(world) == "absent", "and the hook made nothing"


def test_live_process_of_this_account_not_serve_py_is_never_signalled():
    """`python3 -m http.server` serves the title and a forged record names its pid: F1 to F4 hold, F5 (its argv) does not."""
    world = World()
    pid = world.title_only_listener(with_record=True)
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == line_o(world), line
    assert world.signals() == [] and alive(pid), "a process that is not serve.py is never stopped, whatever it serves"


def test_serve_py_of_another_project_is_never_signalled():
    """A `serve.py` that is some other project's (its `--repo` differs) holds our derived port (120 ports: collisions happen), and a record names it."""
    world = World()
    other = world.root / "other-project"
    other.mkdir()
    pid = world.start_standin("title", repo=other)
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == line_o(world), line
    assert world.signals() == [] and alive(pid)


def test_another_accounts_pid_in_record_is_never_signalled():
    """`kill(pid, 0)` says PermissionError: alive and another account's (simulated by an injected kill). F2/F3 fail: nothing is signalled."""
    world = World()
    pid = world.start_standin("title")
    module = world.load_hook()
    sent: list = []
    module.kill = lambda p, s: (_ for _ in ()).throw(PermissionError()) if s == 0 else sent.append((p, s))
    module.signal_process = lambda *a, **k: sent.append(a)
    verdict = module.establish(world.project.resolve(), world.port, world.state.resolve())
    assert verdict.kind == "not_own" and verdict.fact in ("F2", "F3"), verdict
    serve = module.load_serve()
    line = module.restart_own(serve, world.project.resolve(), world.slug, world.port, world.board, world.state.resolve(), "an older version")
    assert line == line_o(world) and sent == [] and alive(pid), (line, sent)


def test_record_that_is_wrong_in_any_field_is_never_signalled():
    """F1: each way the record can be wrong, with a stand-in that is otherwise its own: no signal, and a report."""
    world = World()
    pid = world.start_standin("title")
    good = json.loads((world.servers / f"{world.port}.json").read_text())
    cases = {
        "service": ({"service": "something-else"}, "O"),
        "port": ({"port": world.port + 1}, "O"),
        "repo": ({"repo": str(world.root / "elsewhere")}, "O"),
        "pid text": ({"pid": str(pid)}, "O"),
        "pid one": ({"pid": 1}, "O"),
        "pid bool": ({"pid": True}, "O"),
    }
    for label, (fields, kind) in cases.items():
        fields = dict(fields)
        world.write_record(fields.pop("pid", pid), **fields)
        done = world.run_hook()
        assert world.lines(done) == [line_o(world)], (label, done.stdout)
        assert world.signals() == [] and alive(pid), label
    os.unlink(world.servers / f"{world.port}.json")  # a record that is a link to a real one
    real = world.root / "real-record.json"
    real.write_text(json.dumps(good))
    os.chmod(real, 0o600)
    os.symlink(real, world.servers / f"{world.port}.json")
    done = world.run_hook()
    assert world.lines(done) == [line_o(world)] and world.signals() == [] and alive(pid), "a link is not a regular file of the account's"
    os.unlink(world.servers / f"{world.port}.json")
    (world.servers / f"{world.port}.json").write_text("{not json")
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line.startswith(line_u_prefix(world)) and line.endswith(line_u_suffix(world)) and world.signals() == [] and alive(pid), line
    world.write_record(pid)
    os.chmod(world.servers, 0o755)  # the folder of the records is not private
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line.startswith(line_u_prefix(world)) and "servers folder" in line and world.signals() == [] and alive(pid), line
    os.chmod(world.servers, 0o700)
    done = world.run_hook()
    assert len(world.signals()) == 1, "and with the record right again it is its own: the control, that the stand-in could be restarted at all"


def test_a_pid_that_is_not_listening_on_the_port_is_never_signalled():
    """F4: the record names a live process of this account that is a `serve.py` for this repo but is not the one that listens on the port."""
    world = World()
    holder = world.title_only_listener(with_record=False)
    folder = world.root / "bystander" / "status-board"
    folder.mkdir(parents=True)
    (folder / "serve.py").write_text("import time\ntime.sleep(600)\n")
    bystander = spawn([sys.executable, str(folder / "serve.py"), "--repo", str(world.project), "--port", str(world.port)], world.env(), world.root / "by.log", world.root)
    assert wait_for(lambda: alive(bystander), 5)
    world.write_record(bystander)
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == line_o(world), line
    assert world.signals() == [] and alive(bystander) and alive(holder), "neither was stopped"


def test_tools_missing_or_failing_mean_cannot_establish_and_nothing_is_stopped():
    """F3/F4/F5 each need a tool of the base system by its absolute path: removed (a path that is not there) each gives the report that says it could not
    be confirmed, never a signal."""
    for constant, fact in (("PS", "F3"), ("LSOF", "F4")):
        world = World()
        pid = world.start_standin("title")
        world.patch_hook(f'{constant} = "/' + ("bin/ps" if constant == "PS" else "usr/sbin/lsof") + '"', f'{constant} = "/nonexistent/tool-{constant.lower()}"')
        done = world.run_hook()
        (line,) = world.lines(done)
        assert line.startswith(line_u_prefix(world)) and line.endswith(line_u_suffix(world)), (constant, line)
        assert fact in line, (constant, line)
        assert world.signals() == [] and alive(pid), constant


def test_pid_reused_between_the_check_and_the_signal_is_not_signalled():
    """The facts are measured again right before the signal. A listener that stops being the pid's (here: lsof's second answer says the pid no longer
    listens, as when the pid has been reused by another process) is not signalled."""
    world = World()
    pid = world.start_standin("title")
    module = world.load_hook()
    sent: list = []
    module.signal_process = lambda *a, **k: sent.append(a)
    real = module.run_tool
    calls = {"lsof": 0}

    def run(argv, timeout=2.0):
        if argv[0] == module.LSOF and "-iTCP:%d" % world.port in " ".join(argv):
            calls["lsof"] += 1
            if calls["lsof"] >= 2:
                return 1, ""  # lsof's "no match": the pid does not listen there any more
        return real(argv, timeout)

    module.run_tool = run
    serve = module.load_serve()
    line = module.restart_own(serve, world.project.resolve(), world.slug, world.port, world.board, world.state.resolve(), "an older version")
    assert calls["lsof"] == 2, "F4 is measured twice"
    assert sent == [] and alive(pid), (sent, line)
    assert line == line_o(world), line


# ── the edges ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_hang_on_proof_is_silent_and_restarts_own_board():
    world = World()
    world.make_key()
    pid = world.start_standin("hang")
    started = time.time()
    done = world.run_hook()
    took = time.time() - started
    (line,) = world.lines(done)
    assert restarted_line(world, "it does not answer the proof", pid).fullmatch(line), line
    assert took < 25 and world.signals() == [pid], (took, world.signals())


def test_redirect_and_large_answer_are_not_proven():
    for mode in ("redirect", "large"):
        world = World()
        world.make_key()
        pid = world.start_standin(mode)
        done = world.run_hook()
        (line,) = world.lines(done)
        assert restarted_line(world, "it does not answer the proof", pid).fullmatch(line), (mode, line)
        assert world.signals() == [pid], mode


def test_503_twice_then_restart():
    world = World()
    world.make_key()
    pid = world.start_standin("nokey")
    done = world.run_hook()
    (line,) = world.lines(done)
    assert restarted_line(world, "no usable key", pid).fullmatch(line), line
    asked = [float(row.split()[0]) for row in world.requests.read_text().splitlines() if "/_proof" in row]
    assert len(asked) >= 2 and asked[1] - asked[0] >= 0.45, f"asked once more after half a second: {asked}"
    assert world.signals() == [pid]


def test_503_then_200_proven_no_restart():
    world = World()
    world.make_key()
    pid = world.start_standin("flaky")
    done = world.run_hook()
    assert done.stdout == "" and world.signals() == [] and alive(pid), (done.stdout, world.signals())
    asked = [row for row in world.requests.read_text().splitlines() if "/_proof" in row]
    assert len(asked) == 2, "a 503 and then the proof"


def test_phone_mode_board_reported_not_restarted():
    world = World()
    world.make_key()
    pid = world.start_standin("wrong", record={"host": "0.0.0.0"})
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == board_line(world, " runs in phone mode and cannot prove itself; nothing was stopped. Stop it and start it again with --host."), line
    assert world.signals() == [] and alive(pid)


def test_sigterm_ignored_is_reported_no_sigkill():
    world = World()
    world.make_key()
    pid = world.start_standin("ignore_term")
    started = time.time()
    done = world.run_hook()
    took = time.time() - started
    (line,) = world.lines(done)
    assert line == board_line(world, f": its server (pid {pid}) did not stop within 3 seconds; nothing else was done. Stop it yourself, then start a session here again."), line
    assert world.signals() == [pid], "one signal: no second, and no SIGKILL"
    assert alive(pid) and listening(world.port), "the process that ignores SIGTERM is still there"
    assert 3 <= took < 15, took
    assert world.started_servers() == [], "no new server was started over it"


def test_restart_still_not_proven_reports_once_no_loop():
    world = World(shim="noproof")  # the server this hook starts answers no proof (a stand-in for a build that cannot)
    world.make_key()
    pid = world.start_standin("wrong")
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line == board_line(world, f" was started again but still cannot prove itself (an older version); see {world.board / 'serve.log'}."), line
    assert world.signals() == [pid] and len(world.started_servers()) == 1, "one signal, one start, no loop"
    assert wait_for(lambda: not alive(pid), 5)


def test_second_restart_within_10_minutes_is_refused():
    world = World(shim="noproof")
    world.make_key()
    world.start_standin("wrong")
    first = world.run_hook()
    assert len(world.lines(first)) == 1 and len(world.signals()) == 1
    stamp = world.board / ".revive-restarted"
    assert stamp.is_file() and stat.S_IMODE(stamp.stat().st_mode) == 0o600, "the stamp is made 0600"
    pid2 = [int(row.split()[0]) for row in world.started_servers()][0]
    world.write_record(pid2)
    second = world.run_hook()
    (line,) = world.lines(second)
    assert line == board_line(world, f" cannot prove itself and was already restarted in the last 10 minutes; see {world.board / 'serve.log'}."), line
    assert len(world.signals()) == 1 and alive(pid2), "no second signal"
    old = time.time() - 700
    os.utime(stamp, (old, old))
    third = world.run_hook()
    assert len(world.signals()) == 2, "after ten minutes it may restart again"


def test_lock_unwritable_stops_nothing():
    """The check, the stop and the start run under the board folder's lock. A lock that cannot be made means stop nothing (the plain start of a free port
    still starts anyway, as before)."""
    world = World()
    world.make_key()
    pid = world.start_standin("wrong")
    os.chmod(world.board, 0o500)
    try:
        done = world.run_hook()
    finally:
        os.chmod(world.board, 0o700)
    (line,) = world.lines(done)
    assert line.startswith(line_u_prefix(world)) and "folder" in line and line.endswith(line_u_suffix(world)), line
    assert world.signals() == [] and alive(pid)


def test_storm_five_hooks_at_once_one_restart():
    world = World()
    old = world.start_old_server() if OLD is not None else None
    if old is None:
        raise Skip("needs --old-plugin")
    go = world.root / "go"
    wait = ("import os, subprocess, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.005)\n"
            "sys.exit(subprocess.run(sys.argv[2:], stdin=sys.stdin).returncode)\n")
    body = json.dumps({"cwd": str(world.project), "hook_event_name": "SessionStart"})
    procs = [subprocess.Popen([sys.executable, "-c", wait, str(go), sys.executable, "-B", str(world.plugin / "hooks" / "revive-board.py")], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=world.env(), cwd=str(TMP)) for _ in range(5)]
    go.write_text("go")
    outs = [p.communicate(body, timeout=90)[0] for p in procs]
    assert world.signals() == [old], f"exactly one signal: {world.signals()}"
    assert len(world.started_servers()) == 1, "one new server"
    told = [line for out in outs for line in out.splitlines() if line.strip()]
    assert len(told) <= 5 and sum("stopped its server" in line for line in told) == 1, told
    assert all(("stopped its server" in line) or ("was not running" in line) or ("started it again" in line) or "restarted in the last" in line or "cannot" in line for line in told), told


# ── proof_outcome, the opener's classification the hook uses ──────────────────────────────────────────────────────────
def test_proof_outcome_kinds():
    """`proof_outcome(key, port) -> (kind, run)` beside `board_proves_itself`, sharing its request code: proven; old (no route, or the page, or not that
    JSON); nokey (503); wrong (a digest that is not the key's); silent (a timeout, a redirect, more than 1 kB, a refused or reset connection)."""
    sys.path.insert(0, str(PLUGIN / "status-board"))
    import open_board

    world = World()
    key = world.make_key()
    cases = {"title": "old", "page": "old", "nokey": "nokey", "wrong": "wrong", "redirect": "silent", "large": "silent", "hang": "silent"}
    for mode, expected in cases.items():
        pid = world.start_standin(mode, record=False, name=f"po-{mode}")
        started = time.time()
        kind, run = open_board.proof_outcome(key, world.port)
        assert kind == expected and (run is None), (mode, kind, run)
        assert time.time() - started < 4, f"{mode}: the exchange is 2 seconds in all"
        os.kill(pid, signal.SIGKILL)
        assert wait_for(lambda: not listening(world.port), 5)
    refused, _ = open_board.proof_outcome(key, world.port)
    assert refused == "silent", "a refused connection is silent"
    proven_world = World(shim="normal")
    pkey = proven_world.make_key()
    pid = spawn([sys.executable, str(proven_world.plugin / "status-board" / "serve.py"), "--repo", str(proven_world.project), "--port", str(proven_world.port)], proven_world.env(), proven_world.root / "p.log", proven_world.board)
    assert wait_for(lambda: listening(proven_world.port), 10)
    kind, run = open_board.proof_outcome(pkey, proven_world.port)
    assert kind == "proven" and re.fullmatch(r"[0-9a-f]{16}", run or ""), (kind, run)
    assert open_board.board_proves_itself(pkey, proven_world.port) == run, "the opener's function still answers with the run"
    assert open_board.proof_outcome(os.urandom(32), proven_world.port) == ("wrong", None)


# ── F5 and the facts on this system ───────────────────────────────────────────────────────────────────────────────────
def test_f5_path_with_spaces_is_established_exactly():
    """On macOS `ps -o command=` joins argv with single spaces, so a `--repo` holding spaces cannot be told from several words. The hook reads the exact
    argv instead (`sysctl kern.procargs2`, the same bytes `ps` is made from, NUL-separated), so a path with spaces is established, never guessed."""
    if sys.platform != "darwin":
        raise Skip("macOS only: Linux reads /proc/<pid>/cmdline, which is exact (see the parser tests)")
    world = World(repo_name="shop app")
    pid = world.start_standin("title")
    shown = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    assert f"--repo {world.project.resolve()} --port" in shown, shown
    module = world.load_hook()
    argv = module.read_argv(pid)
    assert argv is not None and argv[argv.index("--repo") + 1] == str(world.project), argv
    assert module.establish(world.project.resolve(), world.port, world.state.resolve()).kind == "own"
    other = world.root / "shop"
    other.mkdir()
    assert module.establish(other.resolve(), world.port, world.state.resolve()).kind != "own", "a folder that is a prefix of ours is not ours"
    world.write_record(pid, repo=str(other.resolve()))
    assert module.establish(other.resolve(), world.port, world.state.resolve()).kind != "own"


def test_f5_relative_repo_is_resolved_against_the_processs_working_folder():
    spec = importlib.util.spec_from_file_location("revive_pure", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = TMP / "rel-project"
    repo.mkdir(exist_ok=True)
    ok = ["/usr/bin/python3", "/x/status-board/serve.py", "--repo", "rel-project", "--port", "1"]
    assert module.argv_is_serve_py_of(ok, repo, str(TMP)) is True
    assert module.argv_is_serve_py_of(ok, repo, str(TMP / "rel-project")) is False, "another working folder: another folder"
    assert module.argv_is_serve_py_of(ok, repo, None) is None, "a relative --repo with no working folder cannot be established"
    both = ["/usr/bin/python3", "/x/status-board/serve.py", "--repo", str(repo), "--repo", str(TMP), "--port", "1"]
    assert module.argv_is_serve_py_of(both, repo, None) is False, "two --repo: not the form this hook knows"
    assert module.argv_is_serve_py_of(["/usr/bin/python3", "/x/other.py", "--repo", str(repo)], repo, None) is False
    assert module.argv_is_serve_py_of(["/bin/sleep", "/x/status-board/serve.py", "--repo", str(repo)], repo, None) is False, "not a python"
    assert module.argv_is_serve_py_of(["python3", "/x/status-board/serve.py", f"--repo={repo}"], repo, None) is True


def test_linux_parsers_on_fixtures():
    """`/proc/<pid>/cmdline` is NUL-separated and exact; the listener is the `LISTEN` row (state 0A) of `/proc/net/tcp` for the local port, matched to a
    `socket:[inode]` among the process's descriptors. Pure functions, run here on fixtures (this system is not Linux)."""
    spec = importlib.util.spec_from_file_location("revive_pure2", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.argv_from_cmdline(b"python3\0/x y/status-board/serve.py\0--repo\0/a b\0--port\08873\0") == ["python3", "/x y/status-board/serve.py", "--repo", "/a b", "--port", "8873"]
    table = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
             "   0: 0100007F:22A9 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 424242 1 0000000000000000 100 0 0 10 0\n"
             "   1: 0100007F:22AA 00000000:0000 01 00000000:00000000 00:00000000 00000000  1000        0 5555 1 0000000000000000 100 0 0 10 0\n"
             "   2: 00000000:22AB 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 777 1 0000000000000000 100 0 0 10 0\n")
    assert module.listening_inodes_linux(table, 0x22A9) == {424242}, "127.0.0.1:8873 in state LISTEN"
    assert module.listening_inodes_linux(table, 0x22AA) == set(), "an established connection is not a listener"
    assert module.listening_inodes_linux(table, 0x22AB) == set(), "a wildcard listener is not 127.0.0.1"
    assert module.listening_inodes_linux("garbage", 0x22A9) is None, "a table that cannot be read cannot be established"


# ── the words ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_each_line_matches_its_case():
    """The eight lines of the section, built by the hook's own builders with the same arguments as the cases above; each is one line, names the board, and
    ends where it says."""
    module = World().load_hook()
    state = Path("/Users/someone/.claude/status-board")
    lines = {
        "D": module.line_restarted("shop-app", "an older version", 4242, "http://localhost:8800/shop-app (by port: http://127.0.0.1:8873)"),
        "O": module.line_not_own("shop-app", 8873),
        "U": module.line_cannot_establish("shop-app", 8873, "F4: that pid does not hold the port", state),
        "K": module.line_key_not_private("shop-app", state),
        "S": module.line_did_not_stop("shop-app", 4242),
        "R": module.line_still_not_proven("shop-app", "an older version", Path("/b/serve.log")),
        "T": module.line_rate_limited("shop-app", Path("/b/serve.log")),
        "P": module.line_phone_mode("shop-app"),
    }
    expected = {
        "D": 'project-init: status board "shop-app" could not prove it is this project\'s board (an older version); stopped its server (pid 4242) and started it again: http://localhost:8800/shop-app (by port: http://127.0.0.1:8873)',
        "O": 'project-init: status board "shop-app" not started: port 8873 is held by a program that is not this board\'s server. Nothing was stopped; stop that program, then start a session here again.',
        "U": 'project-init: status board "shop-app": port 8873 answers like a board, but this could not be confirmed as this project\'s own server (F4: that pid does not hold the port). Nothing was stopped. If it is yours, stop it (its pid is in /Users/someone/.claude/status-board/servers/8873.json), then start a session here again.',
        "K": 'project-init: status board "shop-app" cannot be checked: /Users/someone/.claude/status-board/servers/board.key must be readable by its owner only. Run chmod 600 on it, or delete it to get a new one, then start a session here again.',
        "S": 'project-init: status board "shop-app": its server (pid 4242) did not stop within 3 seconds; nothing else was done. Stop it yourself, then start a session here again.',
        "R": 'project-init: status board "shop-app" was started again but still cannot prove itself (an older version); see /b/serve.log.',
        "T": 'project-init: status board "shop-app" cannot prove itself and was already restarted in the last 10 minutes; see /b/serve.log.',
        "P": 'project-init: status board "shop-app" runs in phone mode and cannot prove itself; nothing was stopped. Stop it and start it again with --host.',
    }
    for case, text in expected.items():
        assert lines[case] == text, (case, lines[case])
        assert "\n" not in lines[case]


def test_the_lines_name_the_home_the_hook_resolved():
    """`~` for the home folder where the status-board home is under it (the default), the resolved path where STATUS_BOARD_HOME put it elsewhere."""
    for default_home in (True, False):
        world = World(default_home=default_home)
        world.make_key(0o644)
        world.start_standin("title", record=False, name="board")
        (line,) = world.lines(world.run_hook())
        if default_home:
            assert "~/.claude/status-board/servers/board.key" in line and str(world.home) not in line, line
        else:
            assert f"{world.state.resolve()}/servers/board.key" in line and "~" not in line, line


def test_opener_sentences_promise_only_what_the_hook_does():
    """The opener's sentences say that a new session in this project restarts the board; here is what the hook does in each case they cover. A key file
    that is missing (the update): the hook restarts the board, which makes the key. A port that does not prove itself: the hook restarts it if its server
    is this project's own, and says so if something else holds the port. A key that is not private: the hook stops nothing and says what to do, and the
    sentence says what to do first."""
    sys.path.insert(0, str(PLUGIN / "status-board"))
    import open_board

    no_file, not_private, not_proven = open_board.NO_KEY_FILE, open_board.NO_KEY, open_board.NOT_PROVEN
    for text in (no_file, not_private, not_proven):
        assert "\n" not in text and "(a session in this project does it)" not in text and "stop this board's server" not in text.lower().replace("stop the server", ""), text
        assert re.search(r"/(?:Users|home|private|tmp|var)/|~", text) is None, "no path of one machine"
    assert no_file.startswith("Start a new session in this project: it restarts this board") and "nothing to" not in no_file.lower()
    assert "readable by its owner only" in not_private and "delete it" in not_private and "new session in this project" in not_private
    assert not_proven.startswith("Start a new session in this project: it restarts this board if") and "if something else holds the port, that session says so" in not_proven
    for text in (no_file, not_private, not_proven):
        assert text.index("session") <= 110 or "readable by its owner only" in text[:110], "the fix comes first (a toast shows about 110 characters)"
    # the key file is missing, a 2.3.1 board runs: the new session restarts it (what NO_KEY_FILE promises)
    world = World()
    old = world.start_old_server() if OLD is not None else None
    if old is None:
        raise Skip("needs --old-plugin")
    assert "board.key" not in key_state(world) and key_state(world) == "absent"
    assert world.lines(world.run_hook())[0].startswith(board_line(world, " could not prove it is this project's board (an older version)")) and world.signals() == [old]
    # something else holds the port: the session says so (what NOT_PROVEN promises), and stops it not
    other = World()
    pid = other.title_only_listener(with_record=False)
    assert other.lines(other.run_hook()) == [line_o(other)] and other.signals() == [] and alive(pid)
    # a key that is not private: stop nothing, say what to do (what NO_KEY promises)
    closed = World()
    closed.make_key(0o644)
    standin = closed.start_standin("title", name="board")
    (line,) = closed.lines(closed.run_hook())
    assert "must be readable by its owner only" in line and closed.signals() == [] and alive(standin)


# ── the stamp and what else lives in a board's folder ─────────────────────────────────────────────────────────────────
def test_stamp_never_served_and_nothing_else_trips_over_it():
    """`boards/<slug>/.revive-restarted` (0600, made just before a signal) and the lock sit in the board's folder. The board's server (the real 2.3.1 one)
    never serves that folder; the ledger, the helper behind the row and the status line, and the keeper find the same board and the same answers with the
    files there as without them."""
    world = World()
    if OLD is None:
        raise Skip("needs --old-plugin")
    old = world.start_old_server()
    stamp = world.board / ".revive-restarted"
    stamp.write_text("")
    os.chmod(stamp, 0o600)
    (world.board / ".revive.lock").write_text("123")
    import http.client

    for path in ("/.revive-restarted", f"/boards/{world.slug}/.revive-restarted", f"/{world.slug}/.revive-restarted", "/../boards/x", "/.revive.lock"):
        conn = http.client.HTTPConnection("127.0.0.1", world.port, timeout=3)
        conn.request("GET", path)
        answer = conn.getresponse()
        assert answer.status != 200 or b"123" not in answer.read(), path
        conn.close()
    sys.path.insert(0, str(PLUGIN / "status-board"))
    os.environ["STATUS_BOARD_HOME"] = str(world.state)
    helper_out = []
    for extra in (False, True):
        if not extra:
            stamp.unlink()
            (world.board / ".revive.lock").unlink()
        else:
            stamp.write_text("")
            os.chmod(stamp, 0o600)
            (world.board / ".revive.lock").write_text("123")
        done = subprocess.run([sys.executable, "-B", str(PLUGIN / "status-board" / "board_link.py"), "--start", str(world.project), "--no-agent-check"], capture_output=True, text=True,
                              timeout=30, env=world.env(), cwd=str(TMP), stdin=subprocess.DEVNULL)
        said = json.loads(done.stdout)
        said.pop("seen_paths", None)
        helper_out.append(said)
    assert helper_out[0] == helper_out[1] and helper_out[0]["url"], helper_out
    import answer_ledger

    (world.board / "answers.json").write_text(json.dumps({"Q?": {"answer": "yes", "at": "2026-11-15T10:00:00+01:00", "from": "browser"}}))
    found = answer_ledger.find_board(world.project.resolve())
    assert found[0] is not None and Path(found[0]).name == world.slug, found
    assert alive(old)


def test_plain_start_of_a_free_port_is_as_before():
    """Nothing listens: the hook starts the board as before (and `start()` still returns the line the other suites pin)."""
    world = World()
    done = world.run_hook()
    (line,) = world.lines(done)
    assert line.startswith(board_line(world, " was not running; started it again: http")), line
    assert world.signals() == [] and len(world.started_servers()) == 1 and listening(world.port)


def test_every_port_these_tests_used_is_outside_the_owners_range():
    assert PORTS, "no port was used"
    assert all(not 8800 <= port <= 8920 for port in PORTS), [port for port in PORTS if 8800 <= port <= 8920]


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    tests.sort(key=lambda item: item[0] == "test_every_port_these_tests_used_is_outside_the_owners_range")  # the meta test last
    passed, failed, skipped = 0, 0, 0
    for name, fn in tests:
        try:
            fn()
        except Skip as why:
            skipped += 1
            print(f"SKIP  {name}: {why}")
        except BaseException as exc:  # noqa: BLE001 - a failing test must not end the run
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {str(exc)[:700]}")
        else:
            passed += 1
            print(f"PASS  {name}")
        finally:
            stop_everything()
    left = stop_everything()
    shutil.rmtree(TMP, ignore_errors=True)
    note = f", {skipped} not run (they need --old-plugin)" if skipped else ""
    print(f"{passed} passed, {failed} failed{note}, processes left running: {left}, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
