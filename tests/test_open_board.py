#!/usr/bin/env python3
"""The opener: how the owner's browser gets a token for the board, never the key, and never through an address, a link, drawn text or a command line.

    python3 tests/test_open_board.py --plugin <checkout of the plugin>

Release 2.4.0 (a board answers only its own page): a board keeps its data behind `servers/board.key`, readable by
the owner's account only. The key itself never leaves the account's files. The browser gets a TOKEN for that one board and that one run of it, once for
each tab, from `status-board/open_board.py`: it first asks the board's port to prove it holds the key (one GET with a random challenge), then writes an
owner-only local HTML form that posts the token to the board's `POST /_open`, hands only the FILE's path to the system's opener (`open`, `xdg-open`),
prints one sentence (never an address, never the key), and removes the file after use or after 60 seconds, by a cleaner that is given the path on its
standard input and not on its command line.

Every run has a temporary HOME and STATUS_BOARD_HOME, a scratch git project, a scratch record of a board that is a process nobody started,
a stand-in `open` first on PATH that records its arguments, a stand-in key (never a real one) and a stand-in board (`ProofServer`) on a port the OS gave,
which answers the one GET `/_proof` the opener sends. No test binds a port from 8800 to 8920 and none sends a request to anything else.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
PLUGIN = Path(ap.parse_args().plugin).resolve()
OPENER = PLUGIN / "status-board" / "open_board.py"
sys.dont_write_bytecode = True

TMP = Path(tempfile.mkdtemp(prefix="open-board-")).resolve()
FAKE_HOME = TMP / "fakehome"
STUBS = TMP / "stubs"
PROCESSES: list[subprocess.Popen] = []
SENTENCES = {
    "opened": "Opened the board in your browser.",
    "down": "The board is not running; it starts with the next session in this project.",
    # a key file that is there and is not private or not usable: the session's hook stops nothing and says what to do; once the key is closed or deleted
    # a new session restarts the board if it still cannot prove itself
    "key": ("Make servers/board.key in the status-board home readable by its owner only, or delete it, then start a new session in this project, "
            "which restarts the board if it cannot prove itself: this board's key file is not private or not usable."),
    # no key file at all: a board of an earlier release, or a key deleted while the board runs; nothing to make private, nothing to delete: a new session
    # restarts the board, and the new server makes the key
    "nokey": ("Start a new session in this project: it restarts this board, which makes the key. This board has no key because it was started "
              "by an earlier release or its key file was deleted."),
    "error": ("Try the button once more, and tell the session if it happens again: the opener stopped on a problem of its own, so nothing "
              "was opened."),
    "form": ("The page that opens the board could not be written in the servers folder of the status-board home, so nothing was opened; "
             "that folder must be yours and writable by you."),
}
NO_OPENER = re.compile(r"No browser opener \(open or xdg-open\) is available here, so the board cannot be opened from Claude Code on this machine\.")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not 8800 <= port <= 8920, port
    return port


FRONT_PORT = free_port()


# what is true when the port does not prove it holds the key: an older board, a board with another key, or another program is on it; a new session
# restarts the board when the server on the port is this project's own recorded server, and says so when something else holds the port
PROOF_FAIL = ("Start a new session in this project: it restarts this board if the server on this port is this project's own, and if something else "
              "holds the port, that session says so. This port is not answering as this project's board, so nothing was opened.")
# what is true when the opener's own connect failed, when the board gave no answer, and when it answered in a way no board does: none of them promises
# a restart, because the hook that starts a board at the start of a session restarts on a statement by the listener only
UNASKED_SENTENCE = ("Try the button once more: this computer could not connect to the board just now, so nothing was opened. Nothing was stopped, "
                    "and the board may be running.")
MUTE_SENTENCE = ("Try the button once more in a moment: the board accepts a connection but gave no answer, so nothing was opened. If it stays like this its "
                 "server is hung: stop it (its process id is in this board's file in the servers folder of the status-board home), then start a new "
                 "session in this project.")
ODD_SENTENCE = ("Nothing was opened: the program on this port answers the proof in a way this board never does. If it is this project's board and it is "
                "wrong, stop its server (its process id is in this board's file in the servers folder of the status-board home), then start a new "
                "session in this project.")
SERVERS: list = []


class ProofServer:
    """A stand-in for the board's own `GET /_proof?c=<c>`, on the board's recorded port, in this process: `right` answers
    200 `{"run": "<16 hex>", "proof": HMAC-SHA256(raw key, "proof:v2:<port>:<run>:<c>")}` (version 2 of the proof); the other modes are a listener that does not hold the key or that misbehaves."""

    def __init__(self, port: int, key_of, mode: str = "right") -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.port, self.mode, self.requests = port, mode, []
        self.run = os.urandom(8).hex()  # the board's run: a new one at each start, not a secret
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status: int, body: bytes, kind: str = "application/json", extra: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, dict(self.headers)))
                match = re.fullmatch(r"/_proof\?c=([0-9a-f]{32})", self.path)
                if not match:
                    return self.reply(400, b'{"ok": false}')
                challenge = match.group(1)
                key = key_of()
                run = outer.run

                def digest(port: int, run_: str, c: str, k: bytes = key) -> str:
                    return hmac.new(k, f"proof:v2:{port}:{run_}:{c}".encode(), hashlib.sha256).hexdigest()

                right = digest(outer.port, run, challenge)
                mode = outer.mode
                if mode == "right":
                    return self.reply(200, json.dumps({"run": run, "proof": right}).encode())
                if mode == "wrong":  # another port's digest
                    return self.reply(200, json.dumps({"run": run, "proof": digest(outer.port + 1, run, challenge)}).encode())
                if mode == "upper":
                    return self.reply(200, json.dumps({"run": run, "proof": right.upper()}).encode())
                if mode == "replay":  # a digest it read from the true board for another challenge
                    return self.reply(200, json.dumps({"run": run, "proof": digest(outer.port, run, "0" * 32)}).encode())
                if mode == "other_run":  # the digest of one run, the answer says another (a listener that read a proof of an earlier run)
                    return self.reply(200, json.dumps({"run": os.urandom(8).hex(), "proof": right}).encode())
                if mode == "no_run":  # the v1 answer: a digest and no run
                    return self.reply(200, json.dumps({"proof": hmac.new(key, f"proof:v1:{outer.port}:{challenge}".encode(), hashlib.sha256).hexdigest()}).encode())
                if mode == "run_upper":
                    return self.reply(200, json.dumps({"run": run.upper(), "proof": digest(outer.port, run.upper(), challenge)}).encode())
                if mode == "run_short":
                    return self.reply(200, json.dumps({"run": run[:15], "proof": digest(outer.port, run[:15], challenge)}).encode())
                if mode == "run_long":
                    return self.reply(200, json.dumps({"run": run + "0", "proof": digest(outer.port, run + "0", challenge)}).encode())
                if mode == "run_not_text":
                    return self.reply(200, json.dumps({"run": 1234567890123456, "proof": right}).encode())
                if mode == "run_with_colon":  # a run made to shift the fields of the message
                    return self.reply(200, json.dumps({"run": "a:" + run[2:], "proof": digest(outer.port, "a:" + run[2:], challenge)}).encode())
                if mode == "not_json":
                    return self.reply(200, b"hello", "text/plain")
                if mode == "other_shape":
                    return self.reply(200, json.dumps({"run": run, "proof": 12345}).encode())
                if mode == "no_proof_field":
                    return self.reply(200, json.dumps({"run": run, "digest": right}).encode())
                if mode == "empty":
                    return self.reply(200, b"")
                if mode == "refused_503":
                    return self.reply(503, b'{"ok": false}')
                if mode == "refused_400":
                    return self.reply(400, b'{"ok": false}')
                if mode == "redirect":
                    return self.reply(302, b"", "text/plain", {"Location": f"http://127.0.0.1:{outer.port}/_proof?c={challenge}"})
                if mode == "big":  # the right digest, in a 1 MB answer
                    return self.reply(200, json.dumps({"run": run, "proof": right, "pad": "x" * 1_000_000}).encode())
                if mode == "hang":
                    time.sleep(12)
                    return None
                if mode == "drip":  # the right answer, one byte every half second
                    body = json.dumps({"run": run, "proof": right}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.5)
                    return None
                return self.reply(500, b"")

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):  # a client that cut the connection is the point of some modes
                pass

        self.httpd = Server(("127.0.0.1", port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        SERVERS.append(self)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class World:
    """A temporary STATUS_BOARD_HOME with one project and, optionally, its board: a record whose server is a process nobody started."""

    def __init__(self, up: bool = True, with_board: bool = True, key: bool | str = True, proof: str | None = "right") -> None:
        self.root = Path(tempfile.mkdtemp(dir=TMP, prefix="w-"))
        self.state = self.root / "home"
        (self.state / "servers").mkdir(parents=True, mode=0o700)
        os.chmod(self.state / "servers", 0o700)
        self.project = self.root / "shop-app"
        self.project.mkdir()
        git = ["git", "-C", str(self.project), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.live = subprocess.Popen(["sleep", "600"])
        PROCESSES.append(self.live)
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.port = free_port()
        self.key_hex = ""
        if with_board:
            board = self.state / "boards" / "shop-app"
            board.mkdir(parents=True)
            (board / "repo.json").write_text(json.dumps({"repo": str(self.project.resolve())}))
            (board / "narrative.json").write_text(json.dumps({"headline": "x"}))
            rec = {"v": 1, "service": "status-board", "slug": "shop-app", "name": "shop-app", "repo": str(self.project.resolve()), "main_repo": None,
                   "port": self.port, "host": "127.0.0.1", "pid": self.live.pid if up else dead.pid, "started": "2026-11-15T00:00:00+01:00",
                   "plugin_version": "2.4.0"}
            (self.state / "servers" / f"{self.port}.json").write_text(json.dumps(rec))
        if key:
            self.make_key(0o600 if key is True else int(key, 8) if isinstance(key, str) else 0o600)
        self.proof_server = None
        if proof and with_board and up:  # the board's own listener, as the board's server answers /_proof on it
            self.proof_server = ProofServer(self.port, lambda: bytes.fromhex(self.key_hex) if self.key_hex else b"", proof)

    def make_key(self, mode: int = 0o600, size: int = 32) -> None:
        data = os.urandom(size)
        self.key_hex = data.hex()
        path = self.state / "servers" / "board.key"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)

    @property
    def log(self) -> Path:
        return self.root / "stub-open.log"

    def env(self, with_opener: bool = True, opener_exit: int = 0, **extra: str) -> dict:
        STUBS.mkdir(exist_ok=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        for name in ("open", "xdg-open"):
            stub = bin_dir / name
            if with_opener:
                stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{self.log}'\nexit {opener_exit}\n")
                stub.chmod(0o755)
            elif stub.exists():
                stub.unlink()
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        if not (tools / "git").exists():
            (tools / "git").symlink_to(shutil.which("git"))  # the only tool the opener's rules need; no `open`, no `xdg-open`
        base = f"/usr/bin:/bin:{os.path.dirname(sys.executable)}"
        env = {"HOME": str(FAKE_HOME), "PATH": f"{bin_dir}:{base}" if with_opener else str(tools), "STATUS_BOARD_HOME": str(self.state),
               "STATUS_BOARD_FRONT_PORT": str(FRONT_PORT), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def run(self, *args: str, with_opener: bool = True, opener_exit: int = 0, umask: int | None = None, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(OPENER), "--start", str(self.project), *args], capture_output=True, text=True, timeout=30,
                              env=self.env(with_opener, opener_exit, **extra), cwd=str(TMP),
                              preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)

    def forms(self) -> list[Path]:
        return sorted((self.state / "servers").glob("open-*.html"))

    def opener_args(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def test_it_opens_the_board_with_a_form_in_an_owner_only_file_and_prints_one_sentence():
    world = World()
    done = world.run("--cleanup-after", "30")
    assert done.returncode == 0 and done.stdout == SENTENCES["opened"] + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    forms = world.forms()
    assert len(forms) == 1, forms
    path = forms[0]
    assert re.fullmatch(r"open-[0-9a-f]{16}\.html", path.name), path.name
    assert (path.stat().st_mode & 0o777) == 0o600 and ((world.state / "servers").stat().st_mode & 0o777) == 0o700, "0600 in a 0700 folder"
    text = path.read_text()
    assert f'method="post" action="http://127.0.0.1:{world.port}/_open"' in text, text
    assert f'name="n" value="{path.name[5:-5]}"' in text and 'name="t" value="v2.' in text, text
    assert world.key_hex not in text and 'name="k"' not in text, "the form holds a token for this board, never the key"
    assert "document.forms[0].submit()" in text and "<noscript>" in text and "<button" in text, "submitted by one line of script, with a button where none runs"
    assert 'name="referrer"' in text, "no Referer from the local page"


# ── the token: the form carries a token for this board and this run, never the key ──────────────────────────────────────────────
import hashlib
import hmac
import threading
from urllib.parse import parse_qs


def form_fields(path: Path) -> dict[str, str]:
    """The hidden fields of the form, as the browser would send them."""
    text = path.read_text()
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', text))


def token_digest(key: bytes, port: int, run: str, issued: int) -> str:
    """The server's side of the interface, written out here (Version 2): HMAC-SHA256 with the RAW 32 bytes of board.key over
    browser:v2:<port>:<run>:<issued>."""
    return hmac.new(key, f"browser:v2:{port}:{run}:{issued}".encode(), hashlib.sha256).hexdigest()


def token_valid(key: bytes, token: str, port: int, run: str, now: int, max_age: int = 2_592_000) -> bool:
    """Version 2 of the token: five dot-joined fields, `v2`, decimal numbers with no padding, a 16-hex run that is THIS server's run, a lowercase
    digest compared in constant time, `0 <= now - issued <= max_age` with 60 seconds allowed for an `issued` in the future (and for `/_open` 120 s)."""
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "v2" or not all(re.fullmatch(r"(?:0|[1-9][0-9]*)", p) for p in (parts[1], parts[3])):
        return False
    if not re.fullmatch(r"[0-9a-f]{16}", parts[2]) or not re.fullmatch(r"[0-9a-f]{64}", parts[4]):
        return False
    if int(parts[1]) != port or parts[2] != run:
        return False
    issued = int(parts[3])
    if not hmac.compare_digest(parts[4], token_digest(key, port, run, issued)):
        return False
    return -60 <= now - issued <= max_age


def test_opener_form_holds_a_token_not_the_key():
    world = World()
    done = world.run("--cleanup-after", "30")
    assert done.stdout == SENTENCES["opened"] + "\n", done.stdout
    path = world.forms()[0]
    fields = form_fields(path)
    assert set(fields) == {"t", "n"}, f"exactly two fields: {sorted(fields)}"
    assert re.fullmatch(r"[0-9a-f]{16}", fields["n"]) and fields["n"] == path.name[5:-5]
    assert re.fullmatch(r"v2\.[1-9][0-9]*\.[0-9a-f]{16}\.[1-9][0-9]*\.[0-9a-f]{64}", fields["t"]), fields["t"]
    assert world.key_hex not in path.read_text() and world.key_hex not in done.stdout + done.stderr, "the key is in the form, or was printed"
    assert path.read_text().count('type="hidden"') == 2


def test_opener_token_verifies_on_its_port_only():
    world = World()
    before = int(time.time())
    world.run("--cleanup-after", "30")
    after = int(time.time())
    token = form_fields(world.forms()[0])["t"]
    key = bytes.fromhex(world.key_hex)
    version, port, run, issued, digest = token.split(".")
    assert version == "v2" and int(port) == world.port and before <= int(issued) <= after, (token, before, after)
    assert run == world.proof_server.run, "the token carries the run the board gave in its proof, not another"
    assert digest == token_digest(key, world.port, run, int(issued)), "HMAC-SHA256 over browser:v2:<port>:<run>:<issued> with the raw key bytes"
    assert token_valid(key, token, world.port, run, int(time.time())), "valid on its own port and its own run"
    other = world.port + 1 if world.port < 65000 else world.port - 1
    assert not token_valid(key, token, other, run, int(time.time())), "worthless on another port"
    assert not token_valid(key, token.replace(f".{port}.", f".{other}.", 1), other, run, int(time.time())), "and a changed port field does not verify"
    assert not token_valid(key, token, world.port, os.urandom(8).hex(), int(time.time())), "worthless on the same port after the board restarted (another run)"
    assert digest != hmac.new(world.key_hex.encode(), f"browser:v2:{world.port}:{run}:{issued}".encode(), hashlib.sha256).hexdigest(), "the key is the raw bytes, not its hex text"
    assert not token_valid(bytes.fromhex(os.urandom(32).hex()), token, world.port, run, int(time.time())), "and not another key's"


def test_opener_token_older_than_120s_is_refused_by_open():
    """`POST /_open` takes a token issued at most 120 s ago (60 s allowed for the future): the opener issues it when it makes the form, so it is
    fresh when the browser posts it and stale 3 minutes later; an `issued` far in the past or the future is never what it writes."""
    world = World()
    world.run("--cleanup-after", "30")
    token = form_fields(world.forms()[0])["t"]
    key = bytes.fromhex(world.key_hex)
    run, issued = token.split(".")[2], int(token.split(".")[3])
    assert token_valid(key, token, world.port, run, issued + 5, max_age=120 + 60), "fresh while the browser opens the form"
    assert not token_valid(key, token, world.port, run, issued + 181, max_age=120 + 60), "refused by /_open once it is older than 120 s (and the allowance)"
    assert abs(int(time.time()) - issued) < 10, "issued is the time of the opener's run, not a fixed or old value"


def test_two_openers_at_once_make_two_forms_and_each_cleanup_removes_only_its_own():
    """The cleanup child of one run unlinks the path it was given and nothing else, so a second run in parallel (a double press, two sessions
    of one project) keeps its own form until its own time; the names are random, the files are made O_EXCL, and no temporary file is left."""
    world = World()
    runs = []
    for seconds in ("2", "40"):
        runs.append(subprocess.Popen([sys.executable, "-B", str(OPENER), "--start", str(world.project), "--cleanup-after", seconds],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=world.env(), cwd=str(TMP)))
    results = [(p.communicate(timeout=30), p.returncode) for p in runs]
    assert all(out == SENTENCES["opened"] + "\n" and err == "" and code == 0 for (out, err), code in results), results
    forms = world.forms()
    assert len(forms) == 2 and len({p.name for p in forms}) == 2, forms
    key = bytes.fromhex(world.key_hex)
    for path in forms:
        assert (path.stat().st_mode & 0o777) == 0o600
        assert token_valid(key, form_fields(path)["t"], world.port, world.proof_server.run, int(time.time())), "each form holds a valid token"
        assert world.key_hex not in path.read_text()
    assert sorted(world.opener_args()) == sorted(str(p) for p in forms), "each run handed its own file to the opener"
    deadline = time.time() + 10
    while len(world.forms()) > 1 and time.time() < deadline:
        time.sleep(0.2)
    left = world.forms()
    assert len(left) == 1, f"the short-lived form should be gone and the other still there: {left}"
    assert not list((world.state / "servers").glob("*.tmp")), "no temporary file is left"


class Recorder:
    """Another program of the owner's account that holds the board's port (a reused pid, a board that is gone): it records what it receives."""

    def __init__(self, port: int) -> None:
        self.received: list[bytes] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            try:
                conn.settimeout(2)
                self.received.append(conn.recv(65536))
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self.running = False
        self.sock.close()
        self.thread.join(timeout=2)


def test_the_scenario_of_finding_1_another_program_on_the_port_gets_no_form_and_only_the_challenge():
    """The record's pid is alive but is not the board (a reused pid) and another program holds the port: the opener once said it had
    opened the board and the browser posted the token to that program, which then held a token that works on the real board for 30 days. Now
    the opener asks the listener to prove it holds the key before it writes a form: this program cannot, so there is no form, nothing is opened,
    the sentence is said and the exit code is 1; what the program received is one GET with a random challenge, no token, no key."""
    world = World(proof=None)  # the record's pid is a live `sleep`: alive and not the board
    program = Recorder(world.port)
    try:
        done = world.run("--cleanup-after", "30")
    finally:
        program.close()
    assert done.returncode == 1 and done.stdout == PROOF_FAIL + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args(), "a form was written or the system's opener was run"
    assert len(program.received) == 1, f"the program should have received exactly the one challenge: {program.received}"
    request = program.received[0].decode("latin-1")
    assert re.match(rf"GET /_proof\?c=[0-9a-f]{{32}} HTTP/1\.[01]\r\n", request), request
    assert f"Host: 127.0.0.1:{world.port}" in request, request
    assert "v1." not in request and "v2." not in request and world.key_hex not in request and "t=" not in request.split("\r\n\r\n")[0].split(" ")[1], "no token and no key reached it"
    assert world.key_hex not in done.stdout + done.stderr


def test_a_record_whose_pid_is_dead_gives_no_form_and_connects_to_nothing():
    world = World(up=False, proof=None)
    program = Recorder(world.port)
    try:
        done = world.run("--cleanup-after", "30")
    finally:
        program.close()
    assert done.stdout == SENTENCES["down"] + "\n" and not world.forms() and not world.opener_args(), (done.stdout,)
    assert not program.received, "the opener connected to a port whose board is not running"


def test_the_right_proof_gives_the_form_after_one_challenge():
    world = World()
    done = world.run("--cleanup-after", "30")
    assert done.returncode == 0 and done.stdout == SENTENCES["opened"] + "\n", (done.stdout, done.stderr)
    assert len(world.forms()) == 1 and len(world.opener_args()) == 1
    requests = world.proof_server.requests
    assert len(requests) == 1, requests
    path, headers = requests[0]
    assert re.fullmatch(r"/_proof\?c=[0-9a-f]{32}", path), path
    assert headers.get("Host") == f"127.0.0.1:{world.port}", headers
    assert world.key_hex not in path + json.dumps(headers), "the key was sent"
    assert not any(name.lower() in ("x-board-token", "authorization", "cookie") for name in headers), headers


def test_proof_v2_carries_run_and_verifies():
    """Version 2 of the proof: the answer carries `run` (16 lowercase hex) and the digest is over proof:v2:<port>:<run>:<c>. The opener checks
    both, and the token it then makes holds that same run: the run the board gave in its proof, not a made-up or an older one."""
    world = World()
    done = world.run("--cleanup-after", "30")
    assert done.returncode == 0 and done.stdout == SENTENCES["opened"] + "\n", (done.stdout, done.stderr)
    (path, _headers), = world.proof_server.requests
    challenge = path.split("=")[1]
    key = bytes.fromhex(world.key_hex)
    run = world.proof_server.run
    assert re.fullmatch(r"[0-9a-f]{16}", run)
    expected = hmac.new(key, f"proof:v2:{world.port}:{run}:{challenge}".encode(), hashlib.sha256).hexdigest()
    assert expected != hmac.new(key, f"proof:v1:{world.port}:{challenge}".encode(), hashlib.sha256).hexdigest(), "v2 is not v1's message"
    assert form_fields(world.forms()[0])["t"].split(".")[2] == run, "the token holds the run the board gave"
    # another board's run (the digest is right for ITS run) is refused when the answer names a run the digest does not cover
    other = World(proof="other_run")
    refused = other.run("--cleanup-after", "30")
    assert refused.returncode == 1 and refused.stdout == PROOF_FAIL + "\n" and not other.forms(), "a run the digest does not cover was believed"


def test_opener_refuses_a_listener_without_the_key():
    """A fake server on the board's port that follows the v2 shape to the letter but does not hold the key (it has another 32 bytes, and it may even
    know the run, which is not a secret) gets no form and nothing is opened."""
    world = World(proof="right")
    world.proof_server.close()
    stranger = ProofServer(world.port, lambda: os.urandom(32), "right")
    stranger.run = world.proof_server.run  # it knows the run: the run is public
    done = world.run("--cleanup-after", "30")
    assert done.returncode == 1 and done.stdout == PROOF_FAIL + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args(), "a form was written or the opener was run for a listener without the key"
    assert len(stranger.requests) == 1 and "t=" not in stranger.requests[0][0], "it received the challenge and nothing else"


def test_token_v2_format():
    """`v2.<port>.<run>.<issued>.<digest>`: five fields joined by single dots; the numbers plain decimal; the run 16 lowercase hex; the digest 64."""
    world = World()
    world.run("--cleanup-after", "30")
    token = form_fields(world.forms()[0])["t"]
    fields = token.split(".")
    assert len(fields) == 5 and fields[0] == "v2", token
    assert fields[1] == str(world.port) and re.fullmatch(r"[1-9][0-9]{9}", fields[3]), token
    assert re.fullmatch(r"[0-9a-f]{16}", fields[2]) and re.fullmatch(r"[0-9a-f]{64}", fields[4]), token
    assert token.islower() or token.startswith("v2."), token
    assert ".." not in token and " " not in token and token == token.strip()


def test_nothing_the_opener_says_is_an_address():
    """What the opener says (and so what the row toasts and what a session repeats) is never an address:
    no scheme, no host, no port, no key, in any of its sentences."""
    said = {}
    world = World()
    said["opened"] = world.run("--cleanup-after", "30").stdout
    said["down"] = World(up=False).run().stdout
    said["no board"] = World(with_board=False).run().stdout
    said["no key file"] = World(key=False).run().stdout
    said["key not private"] = World(key="644").run().stdout
    said["error"] = run_with_a_problem_of_its_own(World()).stdout
    said["no opener"] = World().run(with_opener=False).stdout
    with_resource_limit = World()
    said["form"] = subprocess.run([sys.executable, "-B", str(OPENER), "--start", str(with_resource_limit.project)], capture_output=True, text=True, timeout=30,
                                  env=with_resource_limit.env(True, 0), cwd=str(TMP), preexec_fn=small_files).stdout
    said["not proven"] = World(proof="wrong").run().stdout
    assert set(said) == {"opened", "down", "no board", "no key file", "key not private", "error", "no opener", "form", "not proven"}
    for label, text in said.items():
        assert text.strip() and text.count("\n") == 1, (label, text)
        assert not re.search(r"https?:|://|localhost|127\.0\.0\.1|::1|\?k=|:\d{2,5}\b|\b\d{4,5}\b", text), f"{label}: the sentence holds an address or a port: {text!r}"
        assert "<" not in text and ".html" not in text, f"{label}: it names a file: {text!r}"


def test_the_form_has_its_own_tab_and_puts_nothing_in_any_storage():
    """The opener always opens a fresh tab and nothing in the form could put the token into another tab.
    The file's name is new each time (16 random hex), so no tab of the browser can already be on that address and the browser must make one;
    the form has no `target`, so its post navigates the tab the file opened in, never another; it has no script but the one line that submits it
    (no `window.open`, no storage, no BroadcastChannel, no `postMessage`, no reference to an opener); the opener's command line is the file's path
    alone, so `open` / `xdg-open` hand it to the default browser as a new document."""
    world = World()
    world.run("--cleanup-after", "30")
    world.run("--cleanup-after", "30")
    first, second = world.forms()
    assert first.name != second.name, "two runs, two names: no tab can already hold the second"
    for form in (first, second):
        text = form.read_text()
        assert not re.search(r"\btarget\s*=", text), "the form posts into its own tab"
        assert not re.search(r"window\.open|localStorage|sessionStorage|BroadcastChannel|postMessage|opener|indexedDB|document\.cookie|<a |<iframe|<link|<img|src=", text), text
        scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
        assert scripts == ["document.forms[0].submit()"], scripts
    args = world.opener_args()
    assert len(args) == 2 and all(re.fullmatch(r"\S*/servers/open-[0-9a-f]{16}\.html", line) for line in args), args


def run_with_a_problem_of_its_own(world: "World") -> subprocess.CompletedProcess:
    """The opener, run from a copy of the plugin's status-board folder whose `answer_ledger.py` raises when it is loaded: an exception of the opener's own
    (not a board that is down, not a key). The exception's text holds a stand-in secret that must not be printed."""
    copy = world.root / "broken-copy"
    shutil.copytree(OPENER.parent, copy / "status-board", ignore=shutil.ignore_patterns("__pycache__"))
    with open(copy / "status-board" / "answer_ledger.py", "a") as handle:
        handle.write("\nraise RuntimeError('boom abababababababab')\n")
    return subprocess.run([sys.executable, "-B", str(copy / "status-board" / "open_board.py"), "--start", str(world.project)], capture_output=True, text=True,
                          timeout=30, env=world.env(True, 0), cwd=str(TMP))


def small_files() -> None:
    """For a child process: no file may grow (the write of a form fails with EFBIG; the signal that would kill it is ignored)."""
    import resource
    import signal

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def cleaner_lines() -> list[str]:
    """The command lines of the cleaners that are running, as `ps` gives them to every account (the part of a cleaner's code that says what it is)."""
    out = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=20).stdout
    return [line for line in out.splitlines() if " -c " in line and "os.unlink" in line and "time.sleep" in line
            and Path(line.split()[0]).name.lower().startswith("python")]


def test_the_cleaners_command_line_holds_no_path_and_no_nonce():
    """The form's path holds the nonce `n`, and `ps` shows every command line to every account. The system's
    opener needs the path (the stub's log shows it: that argument cannot be avoided), but the cleaner is given it on its standard input, so its
    command line, as `ps` gives it, has neither the path nor the nonce nor the folder."""
    world = World()
    done = world.run("--cleanup-after", "40")
    assert done.stdout == SENTENCES["opened"] + "\n", (done.stdout, done.stderr)
    (form,) = world.forms()
    nonce = form.name[5:-5]
    time.sleep(0.5)
    lines = cleaner_lines()
    assert lines, "no cleaner is running: the test would pass for nothing"
    for line in lines:
        assert nonce not in line and str(form) not in line and str(world.state) not in line and ".html" not in line and "servers" not in line, \
            f"the cleaner's command line shows the path or the nonce: {line[:300]}"
    assert nonce in " ".join(world.opener_args()), "the system's opener is given the path: that argument is the one that cannot be avoided"


def test_the_cleaner_reads_the_path_from_its_standard_input_and_removes_only_that_file():
    world = World()
    decoy = world.state / "servers" / "open-0123456789abcdef.html"
    decoy.write_text("not ours")
    notes = world.state / "servers" / "board-notes.txt"
    notes.write_text("not a form")
    world.run("--cleanup-after", "1")
    (form,) = [path for path in world.forms() if path != decoy]
    deadline = time.time() + 8
    while form.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not form.exists(), "the cleaner did not remove the form it was given"
    assert decoy.exists() and notes.exists(), "the cleaner removed a file it was not given"


def test_the_docstring_says_the_systems_opener_needs_the_path_and_what_makes_knowing_n_useless():
    """The opener's own argument cannot be avoided, and the docstring says so, and says whose half makes the nonce useless."""
    text = OPENER.read_text().split('"""')[1]
    flat = " ".join(text.split())
    assert "the system's opener needs the path" in flat and "on every account's `ps`" in flat, "the docstring does not say that the opener's argument is shown by `ps`"
    assert "standard input" in flat and "not as an argument" in flat, "the docstring does not say how the cleaner gets the path"
    assert "the token is checked before the file is taken" in flat and "makes knowing `n` useless" in flat, "the docstring does not say that the server's half makes n useless"
    assert "The server's half (the board's own server)" in flat, "the docstring does not say whose half that is"


def test_the_challenge_is_random_each_time():
    world = World()
    world.run("--cleanup-after", "30")
    world.run("--cleanup-after", "30")
    challenges = [path.split("=")[1] for path, _ in world.proof_server.requests]
    assert len(challenges) == 2 and challenges[0] != challenges[1] and all(re.fullmatch(r"[0-9a-f]{32}", c) for c in challenges), challenges


def test_a_listener_that_does_not_prove_it_gets_no_form_and_nothing_is_opened():
    """A listener that answers 200 with a wrong digest (another port's, a replayed one, upper case), with a run that does not match the digest or is
    missing or malformed (the v1 answer has none), with no JSON, with another shape, with a redirect, with a 400 or a 503, with an empty body,
    with 1 MB, that hangs, that drips: no form, nothing opened, exit 1, the one sentence, within seconds."""
    modes = ["wrong", "upper", "replay", "other_run", "no_run", "run_upper", "run_short", "run_long", "run_not_text", "run_with_colon",
             "not_json", "other_shape", "no_proof_field", "empty", "refused_503", "refused_400", "redirect", "big", "hang", "drip"]
    # what the opener says follows what the listener did: a statement (no proof route, no usable key, another key) gets the sentence that promises a
    # restart by the hook; an answer no board gives and no answer in time each get a sentence of their own, which promises no restart
    odd = {"refused_400", "redirect", "big"}
    mute = {"hang", "drip"}
    for mode in modes:
        world = World(proof=mode)
        started = time.time()
        done = world.run("--cleanup-after", "30")
        took = time.time() - started
        expected = ODD_SENTENCE if mode in odd else MUTE_SENTENCE if mode in mute else PROOF_FAIL
        assert done.returncode == 1 and done.stdout == expected + "\n" and done.stderr == "", (mode, done.returncode, done.stdout, done.stderr)
        assert not world.forms() and not world.opener_args(), f"{mode}: a form was written or the opener was run"
        assert took < 6, f"{mode}: it took {took:.1f} s"
        assert world.key_hex not in done.stdout + done.stderr
        if mode == "redirect":
            assert len(world.proof_server.requests) == 1, "the redirect was followed"


def test_nothing_listening_on_the_port_says_the_board_is_not_running():
    """The connection is refused: the one error that says nothing listens, so the sentence is the board's not running (a session start brings it back),
    not the one about a port that does not prove itself."""
    world = World(proof=None)  # the record's pid is alive; no program holds the port
    done = world.run("--cleanup-after", "30")
    assert done.returncode == 1 and done.stdout == SENTENCES["down"] + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args()


def test_a_connect_that_fails_gets_its_own_sentence_and_nothing_is_opened():
    """The opener's own connect fails with an error other than refused (injected in its process: nothing is exhausted): the machine could not ask, so the
    sentence says so and says to try again; it does not say the port is not answering as this project's board, and it opens nothing."""
    world = World(proof="right")
    inject = Path(tempfile.mkdtemp(dir=TMP, prefix="inject-"))
    (inject / "sitecustomize.py").write_text(
        "import errno, os, socket\n_real = socket.socket.connect\n\n\ndef connect(self, address):\n"
        "    if isinstance(address, tuple) and address[1] == int(os.environ['INJECT_PORT']):\n"
        "        raise OSError(errno.EADDRNOTAVAIL, os.strerror(errno.EADDRNOTAVAIL))\n    return _real(self, address)\n\n\nsocket.socket.connect = connect\n")
    done = world.run("--cleanup-after", "30", PYTHONPATH=str(inject), INJECT_PORT=str(world.port))
    assert done.returncode == 1 and done.stdout == UNASKED_SENTENCE + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args(), "nothing was written or opened"
    assert "not answering as this project's board" not in done.stdout


def test_the_proof_is_bound_to_the_port_the_run_and_the_challenge():
    """A digest for another port, another run or another challenge is refused (the modes `wrong`, `other_run` and `replay` above); the right one is
    HMAC-SHA256 over the ASCII string proof:v2:<port>:<run>:<c> with the RAW key: a board that signs with the key's hex text does not pass."""
    world = World(proof="right")
    assert world.run("--cleanup-after", "30").stdout == SENTENCES["opened"] + "\n"
    hexed = World(proof="right")
    hexed.proof_server.close()
    hexed.proof_server = ProofServer(hexed.port, lambda: hexed.key_hex.encode(), "right")  # signs with the hex text of the key
    done = hexed.run("--cleanup-after", "30")
    assert done.returncode == 1 and done.stdout == PROOF_FAIL + "\n" and not hexed.forms(), "a proof made with the key's hex text was believed"


def test_the_docstring_says_what_the_opener_now_sends():
    text = OPENER.read_text().split('"""')[1]
    assert "GET /_proof?c=" in text and "carries no secret" in text, "the docstring does not say what the opener sends to the port"
    assert "proof:v2:" in text and "compare_digest" in text and "no redirect" in text and "1 kB" in text, "the docstring does not say how the proof is checked"
    assert "browser:v2:" in text and "run" in text and "proof:v1:" not in text and "browser:v1:" not in text, "the docstring still describes version 1"


def test_the_key_is_never_printed_and_never_on_a_command_line():
    """The command line of the system's opener holds the FILE's path and nothing else; `ps`, which every account can read, shows no key, not
    for the opener, not for the child that removes the file."""
    world = World()
    done = world.run("--cleanup-after", "20")
    assert world.key_hex not in done.stdout + done.stderr, "the key was printed"
    args = world.opener_args()
    assert len(args) == 1 and args[0] == str(world.forms()[0]), f"the opener is given the file's path only: {args}"
    assert world.key_hex not in " ".join(args) and "?k=" not in " ".join(args) and "http" not in " ".join(args), args
    listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    assert world.key_hex not in listing, "a process list shows the key"
    assert any("open-" in line and "20" in line for line in listing.splitlines()), "the child that removes the file is running"


def test_the_file_is_removed_after_the_time_it_is_given():
    world = World()
    world.run("--cleanup-after", "2")
    assert len(world.forms()) == 1
    deadline = time.time() + 10
    while world.forms() and time.time() < deadline:
        time.sleep(0.2)
    assert not world.forms(), "the form with the key is still there after its time"


def test_the_file_is_closed_whatever_the_umask():
    world = World()
    world.run("--cleanup-after", "30", umask=0)
    path = world.forms()[0]
    assert (path.stat().st_mode & 0o777) == 0o600, oct(path.stat().st_mode)


def test_a_board_that_is_not_running_gives_one_sentence_and_writes_nothing():
    world = World(up=False)
    done = world.run()
    assert done.returncode != 0 and done.stdout == SENTENCES["down"] + "\n" and done.stderr == "", (done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args(), "no file, no opener"


def test_a_project_with_no_board_gives_the_same_sentence():
    world = World(with_board=False)
    done = world.run()
    assert done.returncode != 0 and done.stdout == SENTENCES["down"] + "\n" and done.stderr == "", (done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args()


def test_a_key_that_is_not_private_is_refused_and_never_written_into_a_form():
    for label, setup in (("a key the group can read", dict(key="640")), ("a key everyone can read", dict(key="644")),
                         ("a key that is too short", None)):
        world = World(**(setup or {}))
        if setup is None:
            world.make_key(0o600, size=8)
        done = world.run()
        assert done.returncode != 0 and done.stdout == SENTENCES["key"] + "\n" and done.stderr == "", (label, done.stdout, done.stderr)
        assert not world.forms() and not world.opener_args(), f"{label}: a form was made or the opener was run"
        assert not world.key_hex or world.key_hex not in done.stdout + done.stderr


def test_the_key_sentence_names_what_fixes_it_and_no_path_of_one_machine():
    """When the key file is there and is not private or not usable, the sentence names the two fixes (make servers/board.key under the status-board home
    readable by its owner only, or delete it) and what then happens: a new session in the project restarts the board if it cannot prove itself (the hook
    that starts a board at the start of a session does it); no path of one machine."""
    for label, setup in (("a key everyone can read", dict(key="644")), ("a key the group can read", dict(key="640"))):
        world = World(**setup)
        done = world.run()
        said = done.stdout.strip()
        assert said == SENTENCES["key"], (label, said)
        assert "servers/board.key" in said and "status-board home" in said, said
        assert "readable by its owner only" in said and "delete it" in said and "start a new session in this project" in said, said
        assert "stop this board's server" not in said, "the sentence that sent a person to stop a process by hand"
        assert str(world.root) not in said and not re.search(r"/(?:Users|home|private|tmp|var)/|~", said), f"a path of one machine in the sentence: {said}"
        # Measured in a real session (Claude Code 2.1.325): a toast is cut after about three lines of text (about 110 characters at 150 columns), and the row
        # toasts this sentence. So the fix comes FIRST and the diagnosis last: what a cut loses is the diagnosis, never the fix.
        assert said.startswith("Make servers/board.key") and said.index("readable by its owner only") + len("readable by its owner only") <= 110, "the fix is not in the first 110 characters"
        assert said.index("not private or not usable") > said.index("start a new session in this project"), "the diagnosis comes after the fix"


def test_with_no_key_file_at_all_the_sentence_does_not_send_the_owner_to_make_one_private_or_delete_it():
    """The update from 2.3.1: a board of the earlier release runs, and no key file exists yet (nothing of 2.4.0 has made one). There is nothing to make
    private and nothing to delete: the sentence says that a new session in the project restarts the board, which makes the key."""
    world = World(key=False)
    assert not (world.state / "servers" / "board.key").exists(), "the scenario has no key file"
    done = world.run()
    said = done.stdout.strip()
    assert done.returncode == 1 and said == SENTENCES["nokey"] and done.stderr == "", (done.returncode, said, done.stderr)
    assert said.startswith("Start a new session in this project: it restarts this board"), said
    lowered = said.lower()
    assert "readable" not in lowered and "private" not in lowered and "delete it" not in lowered and "make " not in lowered and "chmod" not in lowered, said
    assert str(world.root) not in said and not re.search(r"/(?:Users|home|private|tmp|var)/|~", said), f"a path of one machine in the sentence: {said}"
    assert said.index("restarts this board") + len("restarts this board") <= 110, "what to do is in the first 110 characters, which a toast shows"
    assert not world.forms() and not world.opener_args() and not (world.state / "servers" / "board.key").exists(), "the opener makes nothing"


def test_the_sentence_for_a_port_that_does_not_prove_itself_says_what_a_person_does():
    """An older board, a board with another key, or another program is on the port; a new session restarts the board when the server on the port is this
    project's own, and says so when something else holds it (the hook that starts a board at the start of a session does both). The words `not answering
    as this project's board` stay: the skill tells a session what to say by them."""
    world = World(proof="wrong")
    done = world.run()
    said = done.stdout.strip()
    assert done.returncode == 1 and said == PROOF_FAIL and done.stderr == "", (done.returncode, said)
    assert said.startswith("Start a new session in this project: it restarts this board if the server on this port is this project's own"), said
    assert "if something else holds the port, that session says so" in said, said
    assert "start the board again" not in said and "a session in this project does it" not in said, "the sentences that promised what no hook did"
    assert "not answering as this project's board" in said
    assert said.index("this project's own") + len("this project's own") <= 110, "what to do is in the first 110 characters, which a toast shows"


def test_an_exception_of_the_openers_own_gets_a_true_sentence_and_no_traceback():
    """`The board is not running` was said for any exception of the opener's own; it is not true of one. Now the sentence says that the opener stopped on a
    problem of its own and nothing was opened, never a traceback and never the text of the exception (the stand-in secret in it is not printed)."""
    world = World()
    done = run_with_a_problem_of_its_own(world)
    assert done.returncode == 1 and done.stdout == SENTENCES["error"] + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not re.search(r"abababab|Traceback|RuntimeError|boom", done.stdout + done.stderr), "the exception's text or a traceback was printed"
    assert "not running" not in done.stdout, "the sentence for a board that is down was said for something else"
    assert not world.forms() and not world.opener_args(), "something was written or opened"


def test_a_form_that_cannot_be_written_has_its_own_sentence_and_leaves_nothing_behind():
    """The key sentence's fix would be the wrong one for this cause, so it has a sentence of its own; a half-written file is removed."""
    world = World()
    done = subprocess.run([sys.executable, "-B", str(OPENER), "--start", str(world.project), "--cleanup-after", "30"], capture_output=True, text=True, timeout=30,
                          env=world.env(True, 0), cwd=str(TMP), preexec_fn=small_files)
    assert done.returncode == 1 and done.stdout == SENTENCES["form"] + "\n" and done.stderr == "", (done.returncode, done.stdout, done.stderr)
    assert not world.forms() and not world.opener_args(), "a half-written form was left, or the opener was run on it"


def test_a_key_file_that_is_a_link_is_read_as_read_key_reads_it_and_nothing_is_printed():
    world = World(key=False)
    target = world.root / "elsewhere.key"
    target.write_bytes(os.urandom(32))
    os.chmod(target, 0o600)
    (world.state / "servers" / "board.key").symlink_to(target)
    done = world.run("--cleanup-after", "30")
    assert target.read_bytes().hex() not in done.stdout + done.stderr + " ".join(world.opener_args()), "the key was printed or put on a command line"
    assert done.stdout.count("\n") == 1, "one sentence"


def test_without_a_browser_opener_it_says_how_to_open_the_board_without_a_key_and_leaves_no_file():
    world = World()
    done = world.run(with_opener=False)
    assert done.returncode != 0 and NO_OPENER.fullmatch(done.stdout.strip()) and done.stderr == "", (done.stdout, done.stderr)
    assert not world.forms(), "the form was left behind"
    assert world.key_hex not in done.stdout


def test_an_opener_that_fails_leaves_no_file_and_says_so_without_the_key():
    world = World()
    done = world.run(opener_exit=1)
    assert done.returncode != 0 and NO_OPENER.fullmatch(done.stdout.strip()), (done.stdout, done.stderr)
    assert not world.forms() and world.key_hex not in done.stdout + done.stderr


def test_it_sends_one_request_to_the_boards_port_and_never_connects_to_the_front_door():
    world = World()
    door = socket.socket()
    door.bind(("127.0.0.1", FRONT_PORT))
    door.listen(5)
    door.settimeout(0.3)
    try:
        done = world.run("--cleanup-after", "30")
        assert done.stdout == SENTENCES["opened"] + "\n", done.stdout
        assert len(world.proof_server.requests) == 1, "one GET to the board's own port: the challenge"
        try:
            conn, _ = door.accept()
        except socket.timeout:
            conn = None
        assert conn is None, "something connected to the front door's port: the opener asked the door"
    finally:
        door.close()


def test_it_writes_only_its_own_form_and_reads_no_board_content():
    world = World()
    board = world.state / "boards" / "shop-app"
    (board / "answers.json").write_text(json.dumps({"Q?": {"answer": "SECRET-ANSWER", "at": "2026-11-15T00:00:00+01:00"}}))
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in world.state.rglob("*") if p.is_file()}
    done = world.run("--cleanup-after", "30")
    assert "SECRET-ANSWER" not in done.stdout + done.stderr
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in world.state.rglob("*") if p.is_file()}
    changed = sorted(p.name for p in set(after) ^ set(before)) + sorted(p.name for p in before if p in after and before[p] != after[p])
    assert [name for name in changed if not re.fullmatch(r"open-[0-9a-f]{16}\.html", name)] == [], changed


def test_it_refuses_nothing_it_was_not_asked_and_needs_a_start_folder():
    world = World()
    done = subprocess.run([sys.executable, "-B", str(OPENER)], capture_output=True, text=True, timeout=30, env=world.env(), cwd=str(TMP))
    assert done.returncode != 0 and world.key_hex not in done.stdout + done.stderr and not world.forms(), (done.stdout, done.stderr)


def test_the_source_never_puts_the_key_on_a_command_line_or_in_an_address():
    text = OPENER.read_text()
    assert "?k=" not in text, "the key must not be put into an address"
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"(?:say|print)\([^)]*(?:\bkey\b|key_hex|key\.hex)", code), "nothing prints the key"
    for call in re.findall(r"(?:Popen|run|call)\(\[[^\]]*\]", code):
        assert "key" not in call.lower(), f"a command line is built with the key: {call}"
    assert "stderr.write" not in code, "nothing is written to stderr"


def main() -> int:
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
                failed += 1
                reason = " ".join(str(exc).split())[:300] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        for proc in PROCESSES:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for server in SERVERS:
            try:
                server.close()
            except Exception:  # noqa: BLE001 - a stand-in that hung is left to its daemon thread
                pass
        left = sum(1 for proc in PROCESSES if proc.poll() is None)
        subprocess.run(["pkill", "-f", str(TMP)], capture_output=True)  # a child that was to remove a form after its time
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed} passed, {failed} failed, processes left running: {left}, plugin under test: {PLUGIN}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
