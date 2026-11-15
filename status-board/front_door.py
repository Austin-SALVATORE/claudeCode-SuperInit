#!/usr/bin/env python3
"""The front door: one address for every board, each by its project's name.

The owner, 14 Nov 2026: with many projects at once he loses track of which
port is which project's board. Every board has its own port, derived from the
project's path (serve.py `port_for`), and that is right for the board and
useless to a person. So one port is fixed, `http://localhost:8800`, and it
lists every board and answers `/<name>` (the project's folder name, which is
the board's slug) with a 302 to that board's own address.

WHO SERVES IT
Nobody installs or starts anything. Every `serve.py` runs `keep_trying` in a
daemon thread: it tries to bind 127.0.0.1:8800 and, if the port is taken,
tries again every 5 s for as long as the board lives. Whichever board holds
the port serves the front door; when that board stops, its socket closes and
another takes over within one retry. Two that start together: the kernel
gives the port to one and EADDRINUSE to the other, which keeps retrying,
harmlessly.

WHAT IT KNOWS
`STATE/servers/<port>.json`, one record per port, written by `serve.py` at its
start (`FrontDoor.write_record`) and never deleted (it is the last-known
record). Read fresh on every request, so a board that starts later is listed
without a restart and the holder's own version does not matter as long as the
record keeps its shape: keys it does not know are ignored. Keyed by port
because only one process can listen on one; two projects with one folder name
therefore have two records, and the clash is visible.

IT ONLY REDIRECTS
A proxy under one origin would let a page of one board record an answer on
another (the board's `Origin == Host` check would pass, being asked of the same
front door) and would share every board's localStorage. A redirect leaves every
board with its own origin and its own checks. It redirects to `127.0.0.1`, not
to the name it was asked under, because every address this plugin has ever
printed is `127.0.0.1:<port>`: the owner's open tabs, and the page's saved
state, live on that origin, and `localhost:<port>` would quietly be a second,
empty one. 302 and `Cache-Control: no-store`, never 301: a board whose port
changes (a move, the 8800 skip in `port_for`) must not be sent to its old one
by a cache. A board that is down is not redirected to at all (503 with the
command that starts it): a redirect to a dead port is a browser error page that
says nothing about why.

SAFETY
GET and HEAD only. Binds 127.0.0.1 only, never 0.0.0.0, even for a board
started with --host 0.0.0.0 (on macOS a 0.0.0.0 bind sits beside a 127.0.0.1
one on the same port without EADDRINUSE, measured 14 Nov 2026, so a wider bind
would let two doors run and the election stop meaning anything). Answers to the
board's own `host_refusal` and `names` (127.0.0.1, localhost, ::1 and
STATUS_BOARD_HOSTS): a page that points a name of its own at this machine is
refused as it is on a board. There is no `<name>.localhost` form (the owner
chose one form only): it is a foreign name here like any other. Every value it
prints is HTML-escaped, and its pages carry a CSP that allows no script.

WHO IS BELIEVED (the review of 14 Nov 2026, finding 2)
Port 8800 needs no privilege: any program of any account can hold it while no
board does, and answer like a front door, and every board would then print an
address that program controls. So a board believes a door only on evidence a
stranger cannot fake. `servers/front.key` holds a random secret, made once per
home by whichever board starts first (written whole to a temporary file and
`link`ed into place, so boards that start together end with one key, and never
read half of one), mode 0600, in a folder that is 0700. The probe sends a fresh
challenge, `/_boards.json?c=<random>`, and the door answers with
HMAC-SHA256(secret, "<port>:<challenge>"); the secret itself never travels. The port
in it is the one the answering socket listens on and the one the probe asked, so a
relay that forwards a challenge to a true door on another port gets a proof for that
port, which the relay's own probe does not accept (the re-check of 14 Nov 2026,
finding 3). The answer to a challenge is the proof and nothing else, given at once:
the list of boards is not computed for it (finding 1: with about ten boards the rows
took longer than a starting board's probe waits, 0.5 s, and the true door was taken
for silent). Chosen over
"the secret in the response" because anything local can read a response and
replay it, and over "the listening socket's pid is in a record" because that
needs lsof for every probe and the records are the thing a forger writes. Because
the secret belongs to the home and not to the holder, it follows the door when
the door changes hands: in the moment of the change nobody answers ('free'), and
the next holder proves itself with the same key. A key that is missing, unreadable,
not the owner's, or readable by others means the door cannot be checked, so it is
not believed. Not covered: a program that can read the key (any process of the
owner's own account), which can make a proof for any port.

WHICH PORT /<name> GOES TO (finding 3)
A record says where a board is; nothing said that a board of ours listens there. A
forged record, or a stale one whose pid and port were both reused, sent /<name> to
whatever listened on that loopback port, and a forged live record under a real
slug made a clash page or won outright. Now the redirect, and the choice among
live records, require that the process listening on the record's port is the
record's pid and a `serve.py` (`listening_boards`: one lsof of every listener and
one ps). No request runs that check (the re-check of 14 Nov 2026, finding 2: a page
of another site could send hundreds of requests, each running an lsof and a ps per
live record: 190 lsof with 10 boards and 294 with 20, and some connections were
reset): one background thread (`Listeners`) does it, at most once a second while a
record has never been checked and once a minute otherwise, and a request only reads
its last answer. A check that fails or hangs keeps the last answer; a record never
checked is "not checked yet", which is neither up nor "not a board", and a request
for its name waits up to CHECK_WAIT_S for the first check, a list never. The standard
listen queue (5) is raised to 128 so that a burst is not reset. Chosen over
"each board answers /_board.json with its slug" because that adds a route to the
board's own handler, which is another release's business, and old boards would
have none; and over "only a port in 8801..8920" because a board started by hand
with --port may be anywhere. Where lsof is not installed the check cannot be made
and the earlier rule (a live pid, a port that accepts) stands; where it is and the
pid is not listed, the answer is no. Not covered: a forger who points a record at
the real pid and port of another project's board (the name then reaches a real
board of ours, the wrong one).

WHO TAKES PART
Only a server whose home is the default one (the ACCOUNT's `~/.claude/status-board`,
from the password database, not from HOME: a harness that points HOME at a temporary
folder must not bind the real port), or one
that was given STATUS_BOARD_FRONT_PORT explicitly (`election_enabled`). A board
in another home is another universe: a front door it held would list only that
home's boards. This is also what keeps every test server of the existing suite,
which runs in a temporary home with a clean environment, from trying to hold
the owner's port 8800.

`serve.py` is not imported here, it is handed in (`FrontDoor(serve)`): serve.py
imports this file for `FRONT_PORT`, and each importing the other at the top is a
cycle that also loads serve.py twice when it is run as a script. The helpers the
door needs (`slug_for`, `host_refusal`, `allowed_hosts`, `STATE`, `HERE`) are
therefore serve.py's own, not copies: `hooks/revive-board.py` has the reason.

Standard library only.
"""

from __future__ import annotations

import hmac
import html
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

DEFAULT_FRONT_PORT = 8800
FRONT_ENV = "STATUS_BOARD_FRONT_PORT"  # also how the tests keep clear of 8800
POLL_ENV = "STATUS_BOARD_FRONT_POLL_S"  # tests only: the retry is 5 s in life
DEFAULT_POLL_S = 5.0
FRONT_HOST = "127.0.0.1"  # never 0.0.0.0: see the docstring
SERVICE = "status-board-front-door"  # what `/_boards.json` says it is, so a probe can tell it from anything else
PROBE_TIMEOUT_S = 0.5
UP_CONNECT_TIMEOUT_S = 0.3
KEY_FILE = "front.key"  # the secret of a home: see "WHO IS BELIEVED"
# Who listens where is asked of lsof in ONE background thread, never by a request (the re-check of 14 Nov 2026,
# findings 1 and 2): at most one lsof and one ps per LISTEN_MIN_INTERVAL_S while something has not been checked yet, and
# one a minute (LISTEN_REFRESH_S) otherwise; after a failure, one per LISTEN_RETRY_S.
LISTEN_REFRESH_S = 60.0
LISTEN_MIN_INTERVAL_S = 1.0
LISTEN_RETRY_S = 10.0
LISTEN_COALESCE_S = 0.05  # after a wake, how long the thread waits before it looks at what was asked
LISTEN_TIMEOUT_S = 5.0  # lsof or ps that takes longer is ended with everything it started
LISTEN_FORGET_S = 600.0  # a record nobody has asked about for this long is no longer checked
CHECK_WAIT_S = 2.0  # how long a request for /<name> waits for a board it has never checked; a list never waits
LISTEN_QUEUE = 128  # the standard listen queue (5) reset connections when a page sent a burst
# On every response, including the ones the standard library builds itself (a 414, a 501). The CSP allows no script and
# no framing (frame-ancestors, and X-Frame-Options for a browser that ignores it): a page of another origin framed
# /<name>, the 302 led the frame to a board, and one click on it recorded a ruling (the review, finding 1). The opener
# policy cuts the handle a site that opened the door in a window keeps on it.
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)


def _front_port(env=os.environ) -> int:
    try:
        port = int(env.get(FRONT_ENV) or DEFAULT_FRONT_PORT)
    except ValueError:
        return DEFAULT_FRONT_PORT
    return port if 0 < port < 65536 else DEFAULT_FRONT_PORT


FRONT_PORT = _front_port()


def account_home() -> Path:
    """The account's home from the password database. Not HOME: a process whose
    HOME points at a temporary folder (the next test harness) would count as being
    in the default home and bind the real port (the review, finding 5)."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path.home()


def default_state() -> Path:
    return Path(os.environ.get("STATUS_BOARD_HOME") or account_home() / ".claude" / "status-board")


def election_enabled(state: Path, env=None) -> bool:
    """Does a server with this state home take part in the election? Yes with
    the front door's port set explicitly, or in the default home, which is the
    account's (`account_home`)."""
    env = os.environ if env is None else env
    if env.get(FRONT_ENV):
        return True
    try:
        return Path(state).resolve() == (account_home() / ".claude" / "status-board").resolve()
    except OSError:
        return False


# ── what a board is, and whether it is there ──────────────────────────
def is_up(rec: dict) -> bool:
    """Its process is alive and its port accepts a connection. A connect only,
    no HTTP request: `report.json` runs `git fetch`, and listing the boards
    must not start network traffic."""
    pid, port = rec.get("pid"), rec.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, someone else's
    try:
        socket.create_connection(("127.0.0.1", port), timeout=UP_CONNECT_TIMEOUT_S).close()
    except OSError:
        return False
    return True


def _lsof() -> str | None:
    return shutil.which("lsof", path=os.environ.get("PATH", "") + ":/usr/sbin:/usr/bin:/bin")


def _run(cmd: list[str], timeout: float, ok: tuple = (0,)) -> str | None:
    """A command's output, or None when it failed, is missing or did not finish in time. It runs in a process group of
    its own, so that one that hangs is ended together with whatever it started (a hung child would otherwise keep the
    pipe, and this call, open)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
                                start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
        return None
    return out if proc.returncode in ok else None


def listening_boards(keys) -> dict | None:
    """{(pid, port): is it a board of ours that listens there?} for these (pid, port) pairs, from ONE lsof of every
    listener on the machine and ONE ps for the pids that matter: the process listening on the port is the record's pid,
    and a serve.py. None when that could not be asked (lsof failed or took too long): the caller keeps what it knew.
    lsof lists only the account's own processes, so a program of another user on a port is not listed and is not ours.
    No lsof on this machine: the check cannot be made, and the earlier rule stands (see "WHICH PORT /<name> GOES TO")."""
    keys = set(keys)
    lsof = _lsof()
    if lsof is None:
        return {key: True for key in keys}
    out = _run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], LISTEN_TIMEOUT_S)
    if out is None:
        return None
    on_port: dict[int, set[int]] = {}
    pid = None
    for line in out.splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == "n" and pid is not None and line.rsplit(":", 1)[-1].isdigit():
            on_port.setdefault(int(line.rsplit(":", 1)[-1]), set()).add(pid)
    result: dict = {}
    candidates: set[int] = set()
    for key in keys:
        held_pid, held_port = key
        if isinstance(held_pid, int) and isinstance(held_port, int) and held_pid in on_port.get(held_port, ()):
            candidates.add(held_pid)
        else:
            result[key] = False
    commands: dict[int, str] = {}
    if candidates:
        listing = _run(["ps", "-o", "pid=,command=", "-p", ",".join(str(p) for p in sorted(candidates))], LISTEN_TIMEOUT_S, ok=(0, 1))
        if listing is None:
            return None
        for line in listing.splitlines():
            head, _, rest = line.strip().partition(" ")
            if head.isdigit():
                commands[int(head)] = rest
    for key in keys:
        if key not in result:
            result[key] = any(token.endswith("serve.py") for token in commands.get(key[0], "").split())
    return result


def board_listens(pid: object, port: object) -> bool:
    """One record, asked at once (not through the door's thread): True when a board of ours listens there."""
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    answer = listening_boards({(pid, port)})
    return bool(answer and answer.get((pid, port)))


class Listeners:
    """What the door knows about who listens where, kept by one background thread.

    A request never runs lsof or ps. It asks `ask(key)`, which reads the last answer and, if there is none or it is old,
    wakes the thread; the thread coalesces every wake into one lsof and one ps (`listening_boards`), at most one a second
    while something has never been checked, one a minute otherwise, one per ten seconds after a failure. So a burst of
    requests, from anyone, starts at most one check, and the work is the same with 1 board as with 100. When the check
    fails or hangs, the last answer is kept; a record never checked is None ("not checked yet"), which is not "no"."""

    def __init__(self, check=listening_boards) -> None:
        self.refresh_s, self.min_interval_s, self.retry_s = LISTEN_REFRESH_S, LISTEN_MIN_INTERVAL_S, LISTEN_RETRY_S
        self._check = check
        self._cond = threading.Condition()
        self._known: dict = {}
        self._asked: dict = {}
        self._fresh_at: float | None = None
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self.checks = 0

    def ask(self, key: tuple) -> bool | None:
        now = time.monotonic()
        with self._cond:
            self._asked[key] = now
            answer = self._known.get(key)
            stale = self._fresh_at is None or now - self._fresh_at > self.refresh_s
            if (self._thread is None or not self._thread.is_alive()) and not self._stop.is_set():  # and again if it died
                self._thread = threading.Thread(target=self._loop, name="front-door-listeners", daemon=True)
                self._thread.start()
        if answer is None or stale:
            self._wake.set()
        return answer

    def wait_known(self, keys, timeout: float) -> None:
        """Block until these keys have an answer, or `timeout`. Starts no work: the thread was woken by `ask`."""
        end = time.monotonic() + timeout
        with self._cond:
            while any(key not in self._known for key in keys):
                left = end - time.monotonic()
                if left <= 0:
                    return
                self._cond.wait(left)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        last_try, failed = 0.0, False
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                return
            # A request asks about its records one after the other: a moment's wait lets one check cover all of them.
            gap = max(LISTEN_COALESCE_S, (self.retry_s if failed else self.min_interval_s) - (time.monotonic() - last_try))
            if self._stop.wait(gap):
                return
            self._wake.clear()  # after the wait: everything asked meanwhile is covered by this one check
            now = time.monotonic()
            with self._cond:
                for key, when in list(self._asked.items()):
                    if now - when > LISTEN_FORGET_S:
                        del self._asked[key]
                        self._known.pop(key, None)
                keys = set(self._asked)
                due = any(key not in self._known for key in keys) or self._fresh_at is None or now - self._fresh_at > self.refresh_s
            if not keys or not due:
                continue
            last_try = time.monotonic()
            try:
                result = self._check(keys)
            except Exception:  # noqa: BLE001 - one bad listing (a line that is not UTF-8, a format nobody expected) is a failed check, not the end of the thread (the second re-check, finding 1)
                result = None
            with self._cond:
                self.checks += 1
                failed = result is None
                if result is not None:
                    self._known.update(result)
                    self._fresh_at = time.monotonic()
                self._cond.notify_all()


def main_repo_of(repo: Path) -> str | None:
    """The main checkout when `repo` is a linked worktree of another, else
    None. A linked worktree's git dir is not the common one."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) < 2:
        return None
    git_dir, common = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    return None if git_dir == common else str(common.parent)


def plugin_version(here: Path) -> str | None:
    try:
        return json.loads((here.parent / ".claude-plugin" / "plugin.json").read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


# ── the secret of a home, and asking whether a front door answers ─────
def read_key(state: Path) -> bytes | None:
    """The home's secret, or None when it cannot be trusted: missing, unreadable,
    not a regular file of this account, or readable by anyone else."""
    path = Path(state) / "servers" / KEY_FILE
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data if len(data) >= 16 else None


def private_dir(path: Path) -> bool:
    """The folder of the records and the key: 0700, the account's own."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.stat().st_uid != os.getuid():
            return False
        os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def make_key(state: Path) -> bytes | None:
    """The home's secret, made if there is none. Written whole to a temporary
    file and linked into place: `link` fails when the key exists, so boards that
    start at once all end with the first one's key, and none reads half of one."""
    existing = read_key(state)
    if existing:
        return existing
    folder = Path(state) / "servers"
    if not private_dir(folder):
        return None
    tmp = folder / f"{KEY_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        try:
            os.link(tmp, folder / KEY_FILE)
        except FileExistsError:
            pass
    except OSError:
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return read_key(state)


def proof_for(key: bytes, challenge: str, port: int) -> str:
    """HMAC(secret, "<port>:<challenge>"): the proof says which port it is for. A relay on one port that forwards a
    challenge to a true door on another gets a proof for the other, which the probe of the first does not accept
    (the re-check of 14 Nov 2026, finding 3)."""
    return hmac.new(key, f"{int(port)}:{challenge}".encode(), "sha256").hexdigest()


def probe_state(port: int | None = None, state: Path | None = None) -> str:
    """'door' when a front door of this home answers on this port and proves it
    (see "WHO IS BELIEVED"); otherwise why not: 'other' (something answers that
    does not prove it, including a door whose key cannot be read here), 'silent'
    (it accepts and does not answer, or answers rubbish), 'free' (nothing listens)."""
    port = port or FRONT_PORT
    challenge = secrets.token_hex(16)
    try:
        conn = http.client.HTTPConnection(FRONT_HOST, port, timeout=PROBE_TIMEOUT_S)
        try:
            conn.request("GET", f"/_boards.json?c={challenge}")
            body = conn.getresponse().read(65536)
        finally:
            conn.close()
    except ConnectionRefusedError:
        return "free"
    except (OSError, http.client.HTTPException):
        return "silent"
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("service") != SERVICE:
            return "other"
    except ValueError:
        return "other"
    key = read_key(state if state is not None else default_state())
    proof = data.get("proof")
    if key and isinstance(proof, str) and hmac.compare_digest(proof, proof_for(key, challenge, port)):
        return "door"
    return "other"


def probe(port: int | None = None, state: Path | None = None) -> bool:
    return probe_state(port, state) == "door"


def why_unavailable(state: str, port: int | None = None) -> str:
    port = port or FRONT_PORT
    if state == "free":
        return f"no board holds port {port} yet"
    if state == "other":
        return f"port {port} is held by something that is not a board"
    return f"port {port} did not answer"


def name_address(slug: str, port: int | None = None) -> str:
    return f"http://localhost:{port or FRONT_PORT}/{quote(slug)}"


def address_for(slug: str, port: int | None = None, state: Path | None = None) -> str | None:
    """The address by name, when a front door of this home answers and proves it;
    None when it does not (then a session says the port address alone, as before,
    and that the port is held by something that is not a board)."""
    return name_address(slug, port) if probe(port, state) else None


def banner_lines(name: str, slug: str, port: int, state: str, shown: str = FRONT_HOST) -> list[str]:
    """The two lines a server prints when it starts. `state` is what
    `probe_state` says of the front door at that moment ('door' when this
    process holds it itself)."""
    if state == "door":
        return [
            f"{name} board → {name_address(slug)}",
            f"  same board, by port: http://{FRONT_HOST}:{port}   ·   every board: http://localhost:{FRONT_PORT}",
        ]
    return [
        f"{name} board → http://{shown}:{port}",
        f"  (the address by name, {name_address(slug)}, is not available: {why_unavailable(state)})",
    ]


# ── pages ─────────────────────────────────────────────────────────────
STYLE = (
    "body{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem;"
    "color:#1d1d1f}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.35rem .6rem;"
    "border-bottom:1px solid #ddd;vertical-align:top}code{font:13px ui-monospace,monospace;background:#f2f2f2;"
    "padding:.1rem .3rem;border-radius:3px;word-break:break-all}.down{color:#8a5a00}.up{color:#1a7f37}"
    "@media(prefers-color-scheme:dark){body{background:#1c1c1e;color:#f2f2f2}code{background:#2c2c2e}"
    "td,th{border-color:#3a3a3c}}"
)
E = html.escape  # every value that is printed goes through this: names and paths are other people's text


def public_row(row: dict, slug_of) -> dict:
    """What the door says about a board over the socket: its name, its port, whether it is up, when it started, and for a worktree
    the NAME of the project it is a worktree of. Never a path: not the project's, not the main checkout's. The rows keep their
    paths inside the process (`resolve`, the election, board_link.py use them); only what leaves is made here, and every response
    that carries a row goes through this or prints the same fields (`render_list`, `clash_page`, `down_page`)."""
    main = row.get("main_repo")
    return {
        "slug": row["slug"], "name": row["name"], "port": row["port"], "host": row.get("host"), "started": row["started"],
        "up": bool(row["up"]), "known": bool(row["known"]), "foreign": bool(row.get("foreign")),
        "unchecked": bool(row.get("unchecked")), "worktree_of": slug_of(Path(main)) if main else None,
    }


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{E(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def render_list(rows: list[dict], slug_of) -> str:
    if not rows:
        return "<p>No board is known yet. A board appears here when its server starts.</p>"
    out = [
        "<table><tr><th>Board</th><th>Address by name</th><th></th><th>By port</th><th>Note</th><th>Started</th></tr>"
    ]
    for r in rows:
        shown = E(r["name"]) + (f" <code>{E(r['slug'])}</code>" if r["name"] != r["slug"] else "")
        if not r["known"]:  # no record at all
            out.append(
                f"<tr><td>{E(r['slug'])}</td><td><code>{E(name_address(r['slug']))}</code></td>"
                "<td class=\"down\">address not known yet</td><td></td><td></td><td></td></tr>"
            )
            continue
        note = f"worktree of {E(slug_of(Path(r['main_repo'])))}" if r.get("main_repo") else ""  # a name, never the folder
        status = ('<span class="up">up</span>' if r["up"] else
                  '<span class="down">the port is held by something that is not a board</span>' if r.get("foreign") else
                  '<span class="down">not checked yet</span>' if r.get("unchecked") else
                  '<span class="down">not running</span>')
        by_port = (
            f"<a href=\"http://{FRONT_HOST}:{int(r['port'])}/\">{FRONT_HOST}:{int(r['port'])}</a>" if r["up"] else
            f"<code>{FRONT_HOST}:{int(r['port'])}</code>"
        )
        link = f"<a href=\"/{E(quote(r['slug']))}\">{E(name_address(r['slug']))}</a>"
        out.append(
            f"<tr><td>{shown}</td><td>{link}</td><td>{status}</td><td>{by_port}</td>"
            f"<td>{note}</td><td>{E(r['started'])}</td></tr>"
        )
    out.append("</table>")
    return "".join(out)


class FrontDoor:
    """The door of one STATUS_BOARD_HOME. `serve` is serve.py as a module."""

    def __init__(self, serve, state: Path | None = None, port: int | None = None, verify=None):
        self.serve = serve
        self.state = Path(state) if state is not None else Path(serve.STATE)
        self.port = port or FRONT_PORT
        self.verify = verify  # tests give their sockets a check that says yes; None: the thread below asks lsof
        self.listeners = Listeners()

    def ensure_key(self) -> bytes | None:
        return make_key(self.state)

    def stop(self) -> None:
        self.listeners.stop()

    def make_server(self, address: tuple) -> ThreadingHTTPServer:
        """The door's server: a listen queue that holds a burst (the standard one is 5), threads that never keep the
        process alive."""

        class DoorServer(ThreadingHTTPServer):
            request_queue_size = LISTEN_QUEUE
            daemon_threads = True

        return DoorServer(address, self.handler())

    def check(self, rec: dict) -> bool | None:
        """Does a board of ours listen on this record's port? True, False, or None for "not checked yet": a record the
        thread has not looked at. Never runs a command: see `Listeners`."""
        if self.verify is not None:
            return bool(self.verify(rec))
        return self.listeners.ask((rec.get("pid"), rec.get("port")))

    # ── the records ───────────────────────────────────────────────────
    @property
    def records_dir(self) -> Path:
        return self.state / "servers"

    def write_record(self, board, port: int, host: str) -> dict | None:
        """Say that this process serves this board on this port. `.tmp` and
        `os.replace`, so a reader finds the old record or the new one and
        never half of one. A failure costs the board its name, not its start."""
        rec = {
            "v": 1,
            "service": "status-board",
            "slug": board.slug,
            "name": board.name,
            "repo": str(board.repo),
            "main_repo": main_repo_of(board.repo),
            "port": port,
            "host": host,
            "pid": os.getpid(),
            "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            "plugin_version": plugin_version(Path(self.serve.HERE)),
        }
        # 0700 and 0600 (the review, finding 6): the records hold the project's path, its main repo and the pid, and
        # nobody else's account has a use for them.
        if not private_dir(self.records_dir):
            return None
        tmp = self.records_dir / f"{port}.json.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(rec, indent=2))
            os.replace(tmp, self.records_dir / f"{port}.json")
        except OSError:
            return None
        self.ensure_key()  # the secret of the home: made by the first board, read by every later one
        return rec

    def read_records(self) -> list[dict]:
        out = []
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (isinstance(rec, dict) and rec.get("service") == "status-board" and isinstance(rec.get("slug"), str)
                    and isinstance(rec.get("port"), int) and isinstance(rec.get("repo"), str)):
                out.append(rec)
        return out

    # ── what is listed ────────────────────────────────────────────────
    def rows(self, wait: float = 0.0) -> list[dict]:
        """One row per (slug, project path): the live record if there is one,
        else the latest. A record that is dead and whose project no longer
        exists is left out (nothing deletes it). A board folder with no record
        is listed as having no address yet.

        A record that accepts a connection is `up` only when a board of ours listens there (see `Listeners`), `foreign`
        when something else does, and `unchecked` when that has not been looked at yet. `wait` is how long to wait for
        the unchecked ones (a request for /<name>); a list waits for nothing."""
        rows = self._rows()
        if wait > 0 and self.verify is None:
            pending = [(r["pid"], r["port"]) for r in rows if r.get("unchecked")]
            if pending:
                self.listeners.wait_known(pending, wait)
                rows = self._rows()
        return rows

    def _rows(self) -> list[dict]:
        best: dict[tuple[str, str], dict] = {}
        for rec in self.read_records():
            up = is_up(rec)
            # Up means a board of ours listens there (finding 3), not only that something accepts a connection: a forged
            # record, or a stale one whose pid and port were reused, is listed as `foreign`, and /<name> does not go there.
            answer = self.check(rec) if up else None
            foreign = up and answer is False
            unchecked = up and answer is None
            up = up and answer is True
            if not up and not os.path.isdir(rec["repo"]):
                continue
            row = {
                "slug": rec["slug"], "name": str(rec.get("name") or rec["slug"]), "repo": rec["repo"],
                "main_repo": rec.get("main_repo"), "port": rec["port"], "host": rec.get("host"),
                "started": str(rec.get("started") or ""), "up": up, "known": True, "foreign": foreign,
                "unchecked": unchecked, "pid": rec.get("pid"),
            }
            key = (row["slug"], row["repo"])
            held = best.get(key)
            if held is None or (row["up"], row["started"]) > (held["up"], held["started"]):
                best[key] = row
        rows = list(best.values())
        listed = {r["slug"] for r in rows}
        boards = self.state / "boards"
        for folder in sorted(boards.iterdir()) if boards.is_dir() else []:
            if folder.is_dir() and folder.name not in listed:
                rows.append({"slug": folder.name, "name": folder.name, "repo": "", "main_repo": None, "port": 0,
                             "host": None, "started": "", "up": False, "known": False})
        return sorted(rows, key=lambda r: (not r["up"], r["slug"], r["repo"]))

    def resolve(self, name: str, wait: float = 0.0) -> tuple[str, list[dict]]:
        """('one', [row]) | ('clash', rows) | ('many', rows) | ('none', []).

        An exact slug first, then an exact display-name slug, then a unique
        prefix. A prefix never wins over an exact match: `/notes` goes to
        `notes`, not to `notes-archive-old`. Several projects under one
        slug share one board folder (serve.py keys its state by slug): that is
        an old defect, shown here as a clash and not hidden."""
        slug_of = self.serve.slug_for
        rows = self.rows(wait)
        key = slug_of(Path(name))
        by_slug: dict[str, list[dict]] = {}
        shown: dict[str, set[str]] = {}
        for r in rows:
            by_slug.setdefault(r["slug"], []).append(r)
            shown.setdefault(slug_of(Path(r["name"])), set()).add(r["slug"])
        if key in by_slug:
            slugs = [key]
        elif key in shown:
            slugs = sorted(shown[key])
        elif key:
            slugs = sorted({s for s in by_slug if s.startswith(key)}
                           | {s for d, ss in shown.items() if d.startswith(key) for s in ss})
        else:
            slugs = []
        if not slugs:
            return "none", []
        if len(slugs) > 1:
            return "many", [by_slug[s][0] for s in slugs]
        group = by_slug[slugs[0]]
        pool = [r for r in group if r["up"]] or group
        if len({r["repo"] for r in pool}) > 1:
            return "clash", pool
        return "one", [sorted(pool, key=lambda r: r["started"], reverse=True)[0]]

    # ── the election ──────────────────────────────────────────────────
    def keep_trying(self, stop: threading.Event, holding: threading.Event | None = None) -> None:
        """Hold the front door's port, for as long as this process lives. A
        bind that fails (the port is held, by a board or by something that is
        not one) is tried again after the retry, quietly: a banner and a
        hook's line say why the address by name is unavailable; a log line
        every few seconds would only be noise in a terminal."""
        try:
            retry = max(0.05, float(os.environ.get(POLL_ENV) or DEFAULT_POLL_S))
        except ValueError:
            retry = DEFAULT_POLL_S
        while not stop.is_set():
            try:
                httpd = self.make_server((FRONT_HOST, self.port))
            except OSError:
                if stop.wait(retry):
                    return
                continue
            if holding is not None:
                holding.set()
            try:
                httpd.serve_forever()
            finally:
                httpd.server_close()
            return

    # ── the handler ───────────────────────────────────────────────────
    def handler(self):
        door, serve = self, self.serve
        names = serve.allowed_hosts()

        class Handler(BaseHTTPRequestHandler):
            server_version = "status-board-front-door"

            def log_message(self, *_):
                pass

            def end_headers(self):  # noqa: N802 - stdlib naming
                # Here, not in `send`: the responses the standard library builds (a 414, a 501) pass through it too.
                for name, value in SECURITY_HEADERS:
                    self.send_header(name, value)
                super().end_headers()

            def send(self, status: int, body: str, kind: str = "text/html; charset=utf-8", extra: dict | None = None):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def refused(self) -> bool:
                from_here = bool(self.client_address) and self.client_address[0] in serve.LOOPBACK_CLIENTS
                refusal = serve.host_refusal(self.headers, from_here, names)
                if refusal is None:
                    return False
                asked = (self.headers.get("Host") or "").strip().lower()
                shown = asked if serve.HOST_HEADER.match(asked) else "a name that is not a host"
                self.send(refusal[0], page("Not served", (
                    "<p>The front door is served only under this machine's own names "
                    f"(127.0.0.1, localhost). It was asked for as <code>{E(shown)}</code>.</p>")))
                return True

            def not_allowed(self):
                self.send(405, page("Not allowed", "<p>The front door only reads: GET and HEAD.</p>"),
                          extra={"Allow": "GET, HEAD"})

            do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_allowed  # noqa: N815

            def do_HEAD(self):  # noqa: N802 - stdlib naming
                self.do_GET()

            def do_GET(self):  # noqa: N802 - stdlib naming
                if self.refused():
                    return
                path = unquote(urlsplit(self.path).path).lstrip("/")
                if path == "":
                    return self.send(200, page("Status boards", "<h1>Status boards</h1>" + render_list(door.rows(), serve.slug_for)))
                if path == "_boards.json":
                    # A fresh challenge, answered with HMAC(secret, "<port>:<challenge>"): see "WHO IS BELIEVED". The
                    # secret never leaves this process, so what a stranger reads here cannot be replayed to a probe,
                    # and the port in it is the one this socket listens on, so a relay's cannot be forwarded. Answered
                    # AT ONCE and with nothing else (the re-check, finding 1): a board that starts waits half a second
                    # for it, and the rows are not computed for it.
                    port = int(self.server.server_address[1])
                    challenge = (parse_qs(urlsplit(self.path).query).get("c") or [""])[0]
                    key = read_key(door.state) if re.fullmatch(r"[0-9a-f]{16,64}", challenge) else None
                    if key:
                        proof = {"service": SERVICE, "v": 1, "front_port": port, "proof": proof_for(key, challenge, port)}
                        return self.send(200, json.dumps(proof), "application/json")
                    rows = [public_row(r, serve.slug_for) for r in door.rows()]  # names, ports, state: never a path or a pid
                    return self.send(200, json.dumps({"service": SERVICE, "v": 1, "front_port": port, "boards": rows}),
                                     "application/json")
                name = path[:-1] if path.endswith("/") else path
                kind, found = ("none", []) if "/" in name else door.resolve(name, wait=CHECK_WAIT_S)
                if kind == "one":
                    row = found[0]
                    if row["up"]:
                        return self.send(302, page("Moved", "<p>Going to the board.</p>"),
                                         extra={"Location": f"http://{FRONT_HOST}:{int(row['port'])}/"})
                    return self.send(503, door.down_page(row))
                if kind == "clash":
                    return self.send(300, door.clash_page(found))
                if kind == "many":
                    return self.send(300, page("Several boards", (
                        f"<h1>Which one?</h1><p><code>{E(name)}</code> starts several boards.</p>" + render_list(found, serve.slug_for))))
                return self.send(404, page("No such board", (
                    f"<h1>No board is called <code>{E(name)}</code></h1>" + render_list(door.rows(), serve.slug_for))))

        return Handler

    def down_page(self, row: dict) -> str:
        slug = E(row["slug"])
        if not row["known"]:
            return page("Not running", (
                f"<p><strong>{slug} is not running,</strong> and no record says where it starts. It starts with the next "
                "Claude Code session in that project.</p>"))
        if row.get("unchecked"):
            return page("Not checked yet", (
                f"<p><strong>{slug} is not checked yet.</strong> Its record says a program listens on port "
                f"{int(row['port'])}; the door has not yet looked at whether it is this board. Reload in a moment.</p>"))
        if row.get("foreign"):
            return page("Not reachable", (
                f"<p><strong>{slug} is not reachable.</strong> Its record names port {int(row['port'])}, and what listens "
                "there is not a board of this machine's: the port is held by something that is not a board.</p>"))
        # No folder and no install path in the page (2.3.1): the project by name, and the command with a placeholder for each
        command = 'python3 "<the plugin\'s status-board folder>/serve.py" --repo "<the project\'s folder>" &'
        return page("Not running", (
            f"<p><strong>{slug} is not running.</strong> It starts with the next Claude Code session in that project "
            f"({E(row['name'])}), or now, from a terminal, with:</p><p><code>{E(command)}</code></p>"
            "<p>Put the project's folder and the plugin's status-board folder where the two names in angle brackets stand; "
            "the front door does not say where they are.</p>"))

    def clash_page(self, rows: list[dict]) -> str:
        slug_of = self.serve.slug_for
        items = "".join(
            f"<li><strong>{E(r['name'])}</strong> &mdash; "
            + (f"worktree of {E(slug_of(Path(r['main_repo'])))}" if r.get("main_repo") else "not a worktree")
            + f", started {E(r['started'])} &mdash; "
            + (f"<a href=\"http://{FRONT_HOST}:{int(r['port'])}/\">{FRONT_HOST}:{int(r['port'])}</a>" if r["up"] else
               f"not running (was on {int(r['port'])})") + "</li>"
            for r in rows
        )
        return page("Two projects, one name", (
            f"<h1>More than one project is called <code>{E(rows[0]['slug'])}</code></h1><ul>{items}</ul>"
            "<p>These projects share one board folder; give one a different folder name. They are told apart here by their "
            "ports, their start times and whether they are a worktree: the front door does not show folders, and a board "
            "that is running names its project on its own page.</p>"))
