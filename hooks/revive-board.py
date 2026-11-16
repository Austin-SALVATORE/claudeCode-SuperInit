#!/usr/bin/env python3
"""Start a project's status board when nothing is answering on its port, and restart a board of its own that cannot prove itself.

A board's state survives a reboot (it lives in ~/.claude/status-board/boards/
<slug>/); the server that serves it does not. After a restart the owner found
out by opening a dead URL, and the fix was someone remembering the exact
command. This starts the board again at session start, so the URL in the
owner's browser works without anyone being asked.

What it does, in order:
  1. The project has a board — boards/<slug>/narrative.json exists — or stop. The
     project is the folder the session was started in, found as every hook finds it.
  2. Probe the board's port (the one serve.py itself derives):
       the connection is refused -> start serve.py, detached, and print the URL (a refused connection is the one error
                                    that says nothing listens);
       the connect itself fails  -> the hook could not ask (no source port, no descriptor, a filter: `unasked`). The port
                                    says nothing then, so the record decides: a recorded pid that is dead (or a zombie)
                                    means the board is not running and it is started; a pid that is alive, or no record,
                                    means one line saying the hook could not check, and nothing is stopped or started;
       something answers         -> ask it for its PROOF (below);
  3. The proof. A board of this release answers `GET /_proof?c=<32 hex>` with an
     HMAC that only the holder of `servers/board.key` can make (the same request the
     opener makes: a connection of 1 second at most, an exchange of 2 seconds, no redirect, 1 kB, nothing secret sent).
       proven (the digest is the key's)          -> do nothing, say nothing;
       the key file is not private               -> stop nothing, say what to do: no proof can be
                                                    checked, and a restart cannot help (a new server
                                                    refuses that key too);
       old, nokey or wrong (no proof route, no usable key, a digest the key does not make; a missing key file
       is asked with a key that cannot match)    -> STATEMENTS by the listener, in an HTTP answer. Only these can
                                                    lead to a stop, and only when the same is said on a second,
                                                    separate connection and the process is THIS BOARD'S OWN
                                                    server (below);
       odd (a redirect, another status, over 1 kB)  -> one line, nothing stopped;
       mute (connected, then no answer in time, or a reset)  -> one line, nothing stopped;
       unasked (the connect failed)              -> as above: the record decides, nothing is stopped.

THE RULE above every other: a process is signalled only when (a) all five facts below were established, and again right
before the signal, AND (b) the process said `old`, `nokey` or `wrong` in an HTTP answer on two separate connections.
Silence, a hang, a reset, a failed connect or an odd answer never lead to a signal: the absence of an answer can be the
hook's own (the machine had no source port to give it), and a healthy board was once stopped for that and not started
again. Anything that cannot be established means report, stop nothing. A hook that stops the wrong process is the
fault this must not have. The five facts, each measured:
  F1  the record `servers/<port>.json` in the status-board home: a regular file of this account, in a
      folder that is this account's and closed to others; `service` is `status-board`, `port` is the
      derived port, `repo` is this project's folder, `pid` an integer above 1, `host` is 127.0.0.1;
  F2  that pid is alive (`kill(pid, 0)`; another account's process answers PermissionError and fails F3);
  F3  it is this account's (macOS: `/bin/ps -o uid=`; Linux: the owner of `/proc/<pid>`);
  F4  it is the process that listens on 127.0.0.1:<port> (macOS: `/usr/sbin/lsof`; Linux: `/proc/net/tcp`
      and the process's descriptors);
  F5  its argv is `python .../status-board/serve.py` with exactly one `--repo` that is this project's
      folder (a relative one resolved against the process's working folder). The argv is read EXACTLY:
      macOS `ps -o command=` joins it with single spaces, so a folder with spaces cannot be told from
      several words; the hook reads the same bytes NUL-separated (`sysctl kern.procargs2`) instead.
      Linux reads `/proc/<pid>/cmdline`. Where the argv cannot be read, F5 cannot be established.
A listener that serves the board page's title is not thereby a board: a program with no record, a live process
that is not serve.py, a serve.py of another project, another account's process are reported and never signalled.
A board started in phone mode (`host` is not 127.0.0.1) is reported and not restarted: a restart would drop `--host`.

How it is stopped and started: one SIGTERM, to the pid, through ONE function (`signal_process`), after the record
and the facts were measured twice. On Linux the signal goes through a pidfd opened before the second measure, so a
pid reused in between cannot receive it. Where there is no pidfd (macOS), the window between the second measure and
`kill` is a few milliseconds: accepted, and stated here. It waits up to 3 seconds for the PID to be gone (the process
no longer exists, or it is a zombie that nothing has collected: it holds nothing and is gone for every purpose), and
for nothing else: the port cannot be asked about, because the hook's own connect may be what fails. A server that
ignores SIGTERM (alive after 3 seconds, not a zombie) is not the server this hook knows, so it is reported and never
killed by force. Once the pid is gone the board is started, whatever the port reads: the start needs no connect
(`serve.py` binds, or exits and says the port is held). Then the proof is asked once more: still not proven is
said once and not tried again; a board that is up but cannot be checked (the hook cannot connect) is said so. At most one
restart per board in ten minutes (the file boards/<slug>/.revive-restarted, made 0600 just before the signal);
the whole check, stop and start run under the lock boards/<slug>/.revive.lock, and a lock that cannot be made
means stop nothing (the plain start of a free port still starts anyway, as before).

serve.py's own `slug_for`, `port_for` and `STATE` are imported, not copied, so
the hook cannot drift from the server: the port is a crc32 of the whole resolved
path, which is worse to get subtly wrong. Importing it defines functions and
constants and touches nothing (checked: a tree fingerprint of the plugin dir, the
home and the state dir is unchanged by it).

Detached means a new session (setsid), stdin from /dev/null and stdout/stderr
in boards/<slug>/serve.log — never the hook's own pipes. A child that inherits
them keeps the session start waiting on a pipe that only closes when the
server dies. A short lock file makes two sessions that start together launch one
server rather than one server and one traceback.

"Answers" for a free port means `GET /` returns 200 with the board page, not `GET /report.json`
as SKILL.md suggests for a person: report() runs `git fetch` in the project, and
a session start should not trigger network I/O to find out if a page is up.

Limits, stated rather than hidden:
  * serve.py does not store the display name (`--name` is display-only), so a
    restarted board uses its default, the folder name.
  * It restarts with serve.py's defaults: loopback only. A board that had been
    exposed with --host needs --host again (the hook never restarts one).
  * It only ever looks at the board's derived port: a board on another port is not its business.
  * A restart takes the front door down with a board that held it, until a board's election takes it again
    (the board just started tries at once, the others within five seconds); a board of an older release that
    takes it serves it on 127.0.0.1 only until its own project's next session restarts it.
  * It runs in every session type; it is idempotent, and a teammate starts after its lead, by which time the
    board is up.

Never blocks the session for long: a connection times out in a second, the proof's exchange in two, the wait for a stopped
server's pid in three and for a new one in a few more (about 14 seconds on the longest path, inside the hook's 25), and
every path exits 0. When it cannot start the board it prints one
line pointing at the log; it never fails silently into "the board is probably up". Run by hand with:
    echo '{"cwd": "/path/to/project"}' | python3 hooks/revive-board.py
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVE_PY = PLUGIN_ROOT / "status-board" / "serve.py"
HOST = "127.0.0.1"
BOARD_PAGE = b"<title>status board</title>"  # index.html, line 6
START_WAIT_S = 3.0  # longest the session start waits for a server to come up
LOCK_STALE_S = 20.0  # a lock older than this belongs to a hook that died
STOP_WAIT_S = 3.0  # longest it waits for a stopped server to be gone and its port free
PROOF_RETRY_S = 0.5  # a 503 is asked once more after this
STAMP_NAME = ".revive-restarted"
STAMP_WINDOW_S = 600.0  # at most one restart per board in this time
TOOL_TIMEOUT_S = 2.0
PS = "/bin/ps"  # base-system tools, by their absolute paths
LSOF = "/usr/sbin/lsof"
SERVE_SUFFIX = "/status-board/serve.py"
REASONS = {"old": "an older version", "nokey": "no usable key", "wrong": "an older key"}
STATEMENTS = ("old", "nokey", "wrong")  # what the listener itself says, in an HTTP answer: the only kinds that can lead to a stop
START_SETTLE_S = 1.0  # a server started again that is still alive after this long is up, whatever the hook can ask of it
kill = os.kill  # tests replace these two: what is asked of a process, and the one function that signals


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read(1_000_000))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_serve():
    """serve.py as a module, without writing bytecode into the plugin dir.

    It imports sibling modules (`orca_crew`, `workstreams`, `front_door`), so its
    own directory goes on sys.path; the `__main__` guard keeps the server from
    starting."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SERVE_PY.parent))
    spec = importlib.util.spec_from_file_location("_project_init_serve", SERVE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_board(cwd: Path):
    """(repo, slug, board_dir) for the board of this session's project, else None.

    The same function the other hooks use (`answer_ledger.find_board`): the project is the folder the session was started in
    (`CLAUDE_PROJECT_DIR`), `cwd` only when that is missing, so that the board this starts again is the board whose answers this
    session is told. Its own walk (the git top level first) took a sub-project's session to the root's board. `answer_ledger` is
    the sibling that serve.py sits beside, on the path since `load_serve`, and reads the same STATUS_BOARD_HOME."""
    import answer_ledger

    board, folder, _ = answer_ledger.find_board(cwd)
    if board is None or not (board / "narrative.json").is_file():
        return None
    return folder, board.name, board


def probe_detail(port: int) -> tuple[str, str]:
    """(kind, detail) of what is on the port, asked in one connection:

      free     the connection was refused: nothing listens (the one error that says so);
      board    `GET /` gave 200 with the board page's title;
      other    an answer that is not the board page: something is there, and it is not the board;
      mute     connected, and then no complete answer in a second, or a reset: something is there and does not answer;
      unasked  the connect itself failed, other than refused (no source port, no descriptor, a filter, a timeout before the connection was made): the hook
               could not ask. `detail` is the operating system's words for it.

    `unasked` and `mute` say nothing about what is on the port, and nothing may be concluded from them."""
    import open_board  # a sibling of serve.py, on the path since `load_serve`

    kind, sock, detail = open_board.connect_kind(port, 1.0)
    if sock is None:
        return kind, detail
    conn = http.client.HTTPConnection(HOST, port, timeout=1.0)
    conn.sock = sock
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        head = resp.read(4096)
    except (http.client.RemoteDisconnected, http.client.IncompleteRead, OSError):
        return "mute", ""
    except (http.client.HTTPException, ValueError, UnicodeError):
        return "other", ""  # an answer, but not the page: something that is not the board
    finally:
        conn.close()
        try:
            sock.close()
        except OSError:
            pass
    return ("board" if resp.status == 200 and BOARD_PAGE in head.lower() else "other"), ""


def probe(port: int) -> str:
    """The kind of `probe_detail`: 'free', 'board', 'other', 'mute' or 'unasked'."""
    return probe_detail(port)[0]


def take_lock(lock: Path, strict: bool = False) -> bool:
    """True if we may go on; False if another hook is doing it. A lock that cannot be made (an unwritable folder) is True for the plain start
    of a free port (start anyway) and False when `strict` (stopping a process: stop nothing)."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
                    return False
                lock.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return not strict
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def address_by_name(slug: str, state: Path | None = None) -> str | None:
    """`http://localhost:8800/<slug>` when the front door answers (front_door.py),
    else None, and the line is the one it always was. Asked only after the board
    itself answers: by then its server has had its first try at holding the
    door's port (serve.py waits for it before it serves), so a board that took
    the door is not reported as having none. `front_door` is the sibling that
    serve.py imports, on the path since `load_serve`."""
    try:
        import front_door

        return front_door.address_for(slug, state=state)
    except Exception:
        return None


def launch(serve, repo: Path, slug: str, port: int, board_dir: Path) -> tuple[bool, str]:
    """Launch serve.py detached; (True, where the board answers) once it does, else (False, the one line to print)."""
    proc, failure = _spawn(serve, repo, slug, port, board_dir)
    if proc is None:
        return False, failure
    log_path = board_dir / "serve.log"
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, (
                f'project-init: status board "{slug}" exited at start '
                f"(code {proc.returncode}); see {log_path}"
            )
        if probe(port) == "board":
            by_name = address_by_name(slug, Path(serve.STATE))
            if by_name:
                return True, f"{by_name} (by port: http://{HOST}:{port})"
            return True, f"http://{HOST}:{port}"
        time.sleep(0.1)
    return False, (
        f'project-init: status board "{slug}" was launched but is not answering '
        f"on port {port} yet; see {log_path}"
    )


def launch_checked(serve, repo: Path, slug: str, port: int, board_dir: Path) -> tuple[str, str, int | None]:
    """Launch serve.py detached and say what became of it, as (outcome, text, pid), without needing the hook to be able to connect:

      up         the board answers `GET /`: `text` is where it answers;
      unchecked  the process is alive after a second and the hook cannot connect to see it answer (`unasked`, or `mute`): it is up as far as can be told;
      exited     the process ended (`text` is its exit code): a bind that failed ends it, and it says why in its log;
      failed     it could not be started, or is alive and not answering yet: `text` is the line to print."""
    proc, failure = _spawn(serve, repo, slug, port, board_dir)
    if proc is None:
        return "failed", failure, None
    log_path = board_dir / "serve.log"
    began = time.monotonic()
    deadline = began + START_WAIT_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            return "exited", str(code), None
        kind = probe(port)
        if kind == "board":
            by_name = address_by_name(slug, Path(serve.STATE))
            return "up", (f"{by_name} (by port: http://{HOST}:{port})" if by_name else f"http://{HOST}:{port}"), proc.pid
        if kind in ("unasked", "mute") and time.monotonic() - began >= START_SETTLE_S:
            return ("exited", str(proc.returncode), None) if proc.poll() is not None else ("unchecked", "", proc.pid)
        time.sleep(0.1)
    code = proc.poll()
    if code is not None:
        return "exited", str(code), None
    return "failed", f'project-init: status board "{slug}" was launched but is not answering on port {port} yet; see {log_path}', proc.pid


def _spawn(serve, repo: Path, slug: str, port: int, board_dir: Path):
    """Start serve.py detached (a session of its own, stdin from /dev/null, stdout and stderr in the board's log); (the process, None) or (None, the line)."""
    log_path = board_dir / "serve.log"
    try:
        log = open(log_path, "ab")
        log.write(
            f"--- {datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"revive-board.py: serve.py --repo {repo} --port {port}\n".encode()
        )
        log.flush()
    except OSError:
        log = None  # unwritable board dir: start it anyway, just unlogged
    env = dict(os.environ, STATUS_BOARD_HOME=str(Path(serve.STATE).resolve()))
    try:
        proc = subprocess.Popen(
            [sys.executable or "python3", str(SERVE_PY), "--repo", str(repo), "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log if log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            cwd=str(board_dir),
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        return None, f'project-init: status board "{slug}" could not be started ({exc}); see {log_path}'
    finally:
        if log is not None:
            log.close()  # the child holds its own descriptor
    return proc, None


def start(serve, repo: Path, slug: str, port: int, board_dir: Path) -> str:
    """Launch serve.py detached; return the one line to print."""
    ok, text = launch(serve, repo, slug, port, board_dir)
    return f'project-init: status board "{slug}" was not running; started it again: {text}' if ok else text


# ── what the owner is told: one line each ────────────────────────────────────────────────────────────────────────────
def home_text(state: Path) -> str:
    """The status-board home as the hook resolved it (STATUS_BOARD_HOME may put it elsewhere), with `~` for the home folder where it is under it."""
    try:
        return "~/" + str(Path(state).resolve().relative_to(Path.home().resolve()))
    except (ValueError, OSError, RuntimeError):
        return str(state)


def line_restarted(slug: str, reason: str, pid: int, address: str) -> str:
    return (f'project-init: status board "{slug}" could not prove it is this project\'s board ({reason}); stopped its server (pid {pid}) '
            f"and started it again: {address}")


def line_not_own(slug: str, port: int) -> str:
    return (f'project-init: status board "{slug}" not started: port {port} is held by a program that is not this board\'s server. '
            "Nothing was stopped; stop that program, then start a session here again.")


def line_cannot_establish(slug: str, port: int, fact: str, state: Path) -> str:
    return (f'project-init: status board "{slug}": port {port} answers like a board, but this could not be confirmed as this project\'s own '
            f"server ({fact}). Nothing was stopped. If it is yours, stop it (its pid is in {home_text(state)}/servers/{port}.json), "
            "then start a session here again.")


def line_key_not_private(slug: str, state: Path) -> str:
    return (f'project-init: status board "{slug}" cannot be checked: {home_text(state)}/servers/board.key must be readable by its owner only. '
            "Run chmod 600 on it, or delete it to get a new one, then start a session here again.")


def line_did_not_stop(slug: str, pid: int) -> str:
    return (f'project-init: status board "{slug}": its server (pid {pid}) did not stop within 3 seconds; nothing else was done. '
            "Stop it yourself, then start a session here again.")


def line_still_not_proven(slug: str, reason: str, log: Path) -> str:
    return f'project-init: status board "{slug}" was started again but still cannot prove itself ({reason}); see {log}.'


def line_rate_limited(slug: str, log: Path) -> str:
    return f'project-init: status board "{slug}" cannot prove itself and was already restarted in the last 10 minutes; see {log}.'


def line_phone_mode(slug: str) -> str:
    return (f'project-init: status board "{slug}" runs in phone mode and cannot prove itself; nothing was stopped. '
            "Stop it and start it again with --host.")


# What each line says is true of what the hook measured and did, never of the machine's intent: "the hook could not connect" is a fact of the hook's
# own connect; it does not say the machine refused, or that the board is down.
def line_not_checked(slug: str, port: int, detail: str) -> str:
    """The hook could not connect to the port (an error other than refused), and the record does not say the board is dead."""
    return f'project-init: status board "{slug}" could not be checked: the hook could not connect to port {port} ({detail}); nothing was stopped or started.'


def line_no_answer(slug: str, port: int, state: Path) -> str:
    """Connected, and no complete answer: the hook cannot tell a hung board from a slow machine, so it stops nothing."""
    return (f'project-init: status board "{slug}": port {port} accepts a connection but gives no answer; nothing was stopped. If the board is hung, '
            f"stop its server (its pid is in {home_text(state)}/servers/{port}.json), then start a session here again.")


def line_odd_answer(slug: str, port: int, state: Path, reason: str) -> str:
    """An answer to the proof that no board of this plugin gives."""
    return (f'project-init: status board "{slug}": its server answers the proof in a way this board never does ({reason}); nothing was stopped. '
            f"If it is yours and wrong, stop it (its pid is in {home_text(state)}/servers/{port}.json), then start a session here again.")


def line_restarted_unchecked(slug: str, reason: str, pid: int, new_pid: int) -> str:
    """Stopped on two statements and started again, and the hook cannot connect to see the new server answer."""
    return (f'project-init: status board "{slug}" could not prove it is this project\'s board ({reason}); stopped its server (pid {pid}) and started it '
            f"again (pid {new_pid}); the hook could not connect to check it yet.")


def line_started_unchecked(slug: str, new_pid: int) -> str:
    """Started because the recorded pid is dead, and the hook cannot connect to see the new server answer."""
    return f'project-init: status board "{slug}" was not running; started it again (pid {new_pid}); the hook could not connect to check it yet.'


def line_exited(slug: str, code: object, log: Path) -> str:
    """A server started again that ended at once: when the port is held by something else, that is what it says in its log."""
    return (f'project-init: status board "{slug}" was started again but exited at once (code {code}); the port may be held by something else; '
            f"see {log}.")


# ── is the process on the port this board's own server? five facts, each measured ──────────────────────────────────────
class Verdict(NamedTuple):
    kind: str  # "own", "not_own" (established: it is not), "cannot" (could not be established) or "phone" (its own, in phone mode)
    fact: str  # "F1" to "F5", or "" for "own"
    detail: str  # the words that go into the report, naming the fact
    pid: int | None
    record: dict | None


def _own(pid: int, record: dict) -> Verdict:
    return Verdict("own", "", "", pid, record)


def _not_own(fact: str, detail: str, record: dict | None = None) -> Verdict:
    return Verdict("not_own", fact, f"{fact}: {detail}", None, record)


def _cannot(fact: str, detail: str) -> Verdict:
    return Verdict("cannot", fact, f"{fact}: {detail}", None, None)


def run_tool(argv: list[str], timeout: float = TOOL_TIMEOUT_S) -> tuple[int, str]:
    """(exit code, standard output) of a base-system tool, by its absolute path. Raises OSError (missing) or subprocess.TimeoutExpired."""
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    return done.returncode, done.stdout


def _tool(argv: list[str]) -> tuple[int, str] | None:
    """`run_tool`, or None where the tool could not be run (missing, too slow): the fact then cannot be established."""
    try:
        return run_tool(argv, TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def listening_inodes_linux(table: str, port: int) -> set[int] | None:
    """The inodes of the sockets listening (state 0A) on 127.0.0.1:<port> in the text of /proc/net/tcp; None where the text is not that table."""
    rows = table.splitlines()
    if not rows or not rows[0].split()[:1] == ["sl"]:
        return None
    found: set[int] = set()
    for row in rows[1:]:
        parts = row.split()
        if len(parts) < 10:
            return None
        address, _, hex_port = parts[1].partition(":")
        try:
            if parts[3] == "0A" and address == "0100007F" and int(hex_port, 16) == port:
                found.add(int(parts[9]))
        except ValueError:
            return None
    return found


def argv_from_cmdline(raw: bytes) -> list[str]:
    """The argv in /proc/<pid>/cmdline: NUL-separated and exact."""
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [os.fsdecode(part) for part in parts]


def argv_macos(pid: int) -> list[str] | None:
    """The exact argv of a process of this account on macOS, from `sysctl kern.procargs2.<pid>` (the bytes `ps` is made from): an int argc, the
    executable's path, NULs, then the arguments NUL-separated. None where it cannot be read."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        data = buffer.raw[: size.value]
        argc = int.from_bytes(data[:4], sys.byteorder)
        rest = data[4:]
        rest = rest[rest.index(b"\0"):].lstrip(b"\0")  # past the executable's path and the padding after it
        parts = rest.split(b"\0")
        if argc < 1 or len(parts) < argc:
            return None
        return [os.fsdecode(part) for part in parts[:argc]]
    except Exception:  # noqa: BLE001 - any doubt is "cannot read it"
        return None


def read_argv(pid: int) -> list[str] | None:
    if sys.platform == "darwin":
        return argv_macos(pid)
    if sys.platform.startswith("linux"):
        try:
            return argv_from_cmdline(Path(f"/proc/{pid}/cmdline").read_bytes())
        except OSError:
            return None
    return None


def read_cwd(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    got = _tool([LSOF, "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if got is None or got[0] != 0:
        return None
    names = [line[1:] for line in got[1].splitlines() if line.startswith("n")]
    return names[0] if len(names) == 1 and names[0].startswith("/") else None


def argv_is_serve_py_of(argv: list[str], repo: Path, cwd: str | None) -> bool | None:
    """True when `argv` is `python .../status-board/serve.py` with exactly one `--repo` that resolves to `repo`; False when it is not; None when a
    relative `--repo` cannot be resolved because the working folder is not known."""
    if len(argv) < 3 or not os.path.basename(argv[0]).lower().startswith("python") or not argv[1].endswith(SERVE_SUFFIX):
        return False
    values: list[str] = []
    index = 2
    while index < len(argv):
        item = argv[index]
        if item == "--repo":
            if index + 1 >= len(argv):
                return False
            values.append(argv[index + 1])
            index += 2
            continue
        if item.startswith("--repo="):
            values.append(item[len("--repo="):])
        index += 1
    if len(values) != 1:
        return False
    value = Path(values[0])
    if not value.is_absolute():
        if cwd is None:
            return None
        value = Path(cwd) / value
    try:
        return value.resolve() == repo.resolve()
    except (OSError, RuntimeError):
        return None


def establish(repo: Path, port: int, state: Path) -> Verdict:
    """Is the process on `port` this project's own recorded loopback serve.py? Every fact measured; the first that is not true decides: established as
    not so is `not_own`, not established is `cannot`. Never an exception."""
    try:
        return _establish(repo, port, state)
    except Exception as exc:  # noqa: BLE001 - whatever goes wrong, nothing is stopped
        return _cannot("F1", f"the check itself failed ({type(exc).__name__})")


def _read_record(repo: Path, port: int, state: Path) -> tuple[dict | None, Verdict | None]:
    """F1, the record: (the record, None) when it is a regular file of this account in a private folder, naming this project's board on this port with a
    pid; else (None, the verdict that says why it cannot be used)."""
    uid = os.getuid()
    servers = state / "servers"
    try:
        folder = os.lstat(servers)
    except FileNotFoundError:
        return None, _not_own("F1", "there is no folder of records")
    except OSError:
        return None, _cannot("F1", "the folder of the records could not be read")
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != uid or folder.st_mode & 0o077:
        return None, _cannot("F1", "the servers folder is not this account's, or is open to others")
    path = servers / f"{port}.json"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, _not_own("F1", "there is no record for this port")
    except OSError:
        return None, _cannot("F1", "the record could not be read")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        return None, _not_own("F1", "the record is not a regular file of this account's")
    try:
        record = json.loads(path.read_text()[:65536])
    except (OSError, ValueError):
        return None, _cannot("F1", "the record could not be read")
    if not isinstance(record, dict):
        return None, _cannot("F1", "the record is not what a server writes")
    pid = record.get("pid")
    if record.get("service") != "status-board" or record.get("port") != port or isinstance(record.get("port"), bool):
        return None, _not_own("F1", "the record is not this port's board", record)
    if not isinstance(record.get("repo"), str):
        return None, _not_own("F1", "the record names no project", record)
    try:
        same = Path(record["repo"]).resolve() == repo.resolve()
    except (OSError, RuntimeError):
        return None, _cannot("F1", "the record's project could not be resolved")
    if not same:
        return None, _not_own("F1", "the record is another project's", record)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None, _not_own("F1", "the record's pid is not a process", record)
    return record, None


def _establish(repo: Path, port: int, state: Path) -> Verdict:
    uid = os.getuid()
    record, refused = _read_record(repo, port, state)
    if record is None:
        return refused
    pid = record["pid"]
    if record.get("host") != HOST:
        return Verdict("phone", "F1", "F1: the board listens on something other than 127.0.0.1", pid, record)
    # F2, alive
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return _not_own("F2", "the recorded pid is not alive", record)
    except PermissionError:
        return _not_own("F3", "the recorded pid is another account's", record)
    except OSError:
        return _cannot("F2", "the recorded pid could not be asked")
    # F3, this account's
    if sys.platform.startswith("linux"):
        try:
            owner = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            return _cannot("F3", "the owner of the process could not be read")
    else:
        got = _tool([PS, "-o", "uid=", "-p", str(pid)])
        if got is None or got[0] != 0 or not got[1].strip().isdigit():
            return _cannot("F3", "ps could not say whose the process is")
        owner = int(got[1].strip())
    if owner != uid:
        return _not_own("F3", "the process is another account's", record)
    # F4, it is the process that listens on the port
    listens = _listens(pid, port)
    if listens is None:
        return _cannot("F4", "it could not be told which process listens on the port")
    if not listens:
        return _not_own("F4", "the recorded pid is not the process that listens on the port", record)
    # F5, it is serve.py of this project
    argv = read_argv(pid)
    if argv is None:
        return _cannot("F5", "the process's command line could not be read")
    verdict = argv_is_serve_py_of(argv, repo, None)
    if verdict is None:
        verdict = argv_is_serve_py_of(argv, repo, read_cwd(pid))
        if verdict is None:
            return _cannot("F5", "the process's working folder could not be read")
    if not verdict:
        return _not_own("F5", "the process is not this project's serve.py", record)
    return _own(pid, record)


def _listens(pid: int, port: int) -> bool | None:
    """True when `pid` is the process listening on 127.0.0.1:<port>; False when it is established that it is not; None when that cannot be told."""
    if sys.platform.startswith("linux"):
        try:
            inodes = listening_inodes_linux(Path("/proc/net/tcp").read_text(), port)
            if inodes is None:
                return None
            held = {os.readlink(f"/proc/{pid}/fd/{name}") for name in os.listdir(f"/proc/{pid}/fd")}
        except OSError:
            return None
        return any(f"socket:[{inode}]" in held for inode in inodes)
    got = _tool([LSOF, "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"])
    if got is None:
        return None
    code, out = got
    names = [line[1:] for line in out.splitlines() if line.startswith("n")]
    if code == 0 and f"{HOST}:{port}" in names:
        return True
    if code in (0, 1) and f"{HOST}:{port}" not in names:
        return False  # lsof ran and found no such listener (exit 1 with no output is its "no match")
    return None


def line_for_verdict(slug: str, port: int, state: Path, verdict: Verdict) -> str:
    if verdict.kind == "phone":
        return line_phone_mode(slug)
    if verdict.kind == "not_own":
        return line_not_own(slug, port)
    return line_cannot_establish(slug, port, verdict.detail, state)


# ── stopping and starting ──────────────────────────────────────────────────────────────────────────────────────────
def signal_process(pid: int, handle: int | None = None) -> None:
    """The ONE function that signals a process: SIGTERM, once. `handle` is a pidfd (Linux), so a pid reused since cannot receive it."""
    if handle is not None:
        signal.pidfd_send_signal(handle, signal.SIGTERM)  # type: ignore[attr-defined]
        return
    kill(pid, signal.SIGTERM)


def _open_handle(pid: int) -> int | None:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return opener(pid)
    except OSError:
        return None


def _is_zombie(pid: int) -> bool:
    """True when the process has ended and nothing has collected it yet: it is still in the process table (`kill(pid, 0)` still succeeds) and holds
    nothing. Linux reads the state from /proc/<pid>/stat; elsewhere one `ps -o stat=`. Where that cannot be read, it is not called a zombie."""
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return text[text.rfind(")") + 1:].split()[:1] == ["Z"]
    got = _tool([PS, "-o", "stat=", "-p", str(pid)])
    return got is not None and got[0] == 0 and got[1].strip().startswith("Z")


def _pid_gone(pid: int) -> bool:
    """The process no longer exists, or is a zombie: gone for every purpose. A pid that cannot be asked (another account's) is not gone."""
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return _is_zombie(pid)


def recorded_pid(repo: Path, port: int, state: Path) -> int | None:
    """The pid in this port's record, when the record is this project's board's own; None when there is no usable record."""
    record, _refused = _read_record(repo, port, state)
    return None if record is None else record["pid"]


def stamp_age(stamp: Path) -> float | None:
    try:
        return time.time() - stamp.stat().st_mtime
    except OSError:
        return None


def write_stamp(stamp: Path) -> bool:
    """The restart's stamp, 0600, made (or touched) just before the signal."""
    try:
        fd = os.open(stamp, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.utime(fd)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def proof_kind(state: Path, port: int) -> tuple[str, str]:
    """(kind, reason) of the proof of the board on `port`: 'proven', 'key_not_private' (the key file is there and cannot be used), or one of
    the kinds of `open_board.proof_outcome_detail` (old, nokey, wrong, odd, mute, unasked, free) with its reason in words: for a statement
    (old, nokey, wrong) what it means, for `odd` and `unasked` what was seen (`status 302`, the system's words for the failed connect). A 503 is
    asked once more after half a second: a server that has no key reads the file again, so a key that has just been closed heals it.

    A key file that is not there is not a reason to stop asking: the board is asked with a key that cannot match, so that a board of an
    earlier release (no proof route: `old`), or one that holds a key of its own (`wrong`) says so itself, and a connect that fails or a hang
    says nothing."""
    import front_door
    import open_board

    key_path = state / "servers" / "board.key"
    if not os.path.lexists(key_path):
        key = os.urandom(32)
    else:
        key = front_door.read_key(state, "board.key")
        if key is None or len(key) != 32:
            return "key_not_private", ""
    kind, _run, detail = open_board.proof_outcome_detail(key, port)
    if kind == "nokey":
        time.sleep(PROOF_RETRY_S)
        kind, _run, detail = open_board.proof_outcome_detail(key, port)
    if kind == "proven":
        return "proven", ""
    return kind, REASONS.get(kind) or detail


def restart_own(serve, repo: Path, slug: str, port: int, board_dir: Path, state: Path, reason: str) -> str:
    """The check, the stop and the start of the board on `port`, under the caller's lock; returns the one line to print. Signals the process only when
    all five facts are established, and again right before the signal."""
    verdict = establish(repo, port, state)
    if verdict.kind != "own":
        return line_for_verdict(slug, port, state, verdict)
    stamp = board_dir / STAMP_NAME
    age = stamp_age(stamp)
    if age is not None and age < STAMP_WINDOW_S:
        return line_rate_limited(slug, board_dir / "serve.log")
    pid = verdict.pid
    handle = _open_handle(pid)  # opened BEFORE the second measure
    try:
        again = establish(repo, port, state)
        if again.kind != "own" or again.pid != pid:
            return line_for_verdict(slug, port, state, again if again.kind != "own" else _cannot("F1", "the record changed while it was checked"))
        if not write_stamp(stamp):
            return line_cannot_establish(slug, port, "its folder cannot be written, so a second restart could not be prevented", state)
        signal_process(pid, handle)
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
    # The wait is for the PID, and for nothing else: the port cannot be asked about, because the hook's own connect may be what fails (it once read
    # a connect error as "the port is held", never saw it free, and left the board down). A zombie is gone.
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if _pid_gone(pid):
            break
        time.sleep(0.05)
    else:
        return line_did_not_stop(slug, pid)
    log = board_dir / "serve.log"
    outcome, text, new_pid = launch_checked(serve, repo, slug, port, board_dir)  # the start needs no connect: serve.py binds, or exits and says why
    if outcome == "exited":
        return line_exited(slug, text, log)
    if outcome == "failed":
        return text
    if outcome == "unchecked" or new_pid is None:
        return line_restarted_unchecked(slug, reason, pid, new_pid or 0)
    kind, why = proof_kind(state, port)
    if kind == "proven":
        return line_restarted(slug, reason, pid, text)
    if kind in ("unasked", "mute"):
        return line_restarted_unchecked(slug, reason, pid, new_pid)
    return line_still_not_proven(slug, why or REASONS["nokey"], log)


def main() -> int:
    payload = read_payload()
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd = Path(raw).resolve()
    if not cwd.is_dir():
        return 0
    try:
        serve = load_serve()
    except Exception:
        return 0
    found = find_board(cwd)
    if found is None:
        return 0
    repo, slug, board_dir = found
    problem = getattr(serve, "front_port_problem", lambda: None)()
    if problem:  # serve.py would refuse to start (it cannot hold the door's port): say why, start nothing
        print(f'project-init: status board "{slug}" not started: {problem}')
        return 0
    port = serve.port_for(repo)
    state = Path(serve.STATE).resolve()

    lock = board_dir / ".revive.lock"
    first, detail = probe_detail(port)
    if first == "free":
        plain_start(serve, repo, slug, port, board_dir, lock)
        return 0
    if first == "unasked":  # the hook's own failure to ask: the port says nothing, so the record decides
        said = when_unasked(serve, repo, slug, port, board_dir, state, detail, lock, locked=False)
        if said:
            print(said)
        return 0

    kind, reason = proof_kind(state, port)
    if kind == "proven":
        return 0
    if kind not in STATEMENTS:  # nothing the listener said that this hook acts on
        said = said_without_statement(serve, repo, slug, port, board_dir, state, kind, reason, lock, locked=False)
        if said:
            print(said)
        return 0
    # A statement, on a first connection. A process is stopped only on the same statement on a second, separate connection, under the lock.
    if not take_lock(lock, strict=True):
        if not lock.exists():  # held by another hook: silent. Not made at all: stop nothing, and say why
            print(line_cannot_establish(slug, port, "its folder could not be locked", state))
        return 0
    try:
        second, detail = probe_detail(port)  # the board went away, or another hook has stopped it, while we waited
        if second == "free":
            print(start(serve, repo, slug, port, board_dir))
            return 0
        if second == "unasked":
            said = when_unasked(serve, repo, slug, port, board_dir, state, detail, lock, locked=True)
            if said:
                print(said)
            return 0
        kind, reason = proof_kind(state, port)  # the second connection: another hook may have restarted it, or the key may have been closed
        if kind == "proven":
            return 0
        if kind in STATEMENTS:
            print(restart_own(serve, repo, slug, port, board_dir, state, reason))
            return 0
        said = said_without_statement(serve, repo, slug, port, board_dir, state, kind, reason, lock, locked=True)
        if said:
            print(said)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def plain_start(serve, repo: Path, slug: str, port: int, board_dir: Path, lock: Path) -> None:
    """The port was refused: nothing listens. Start the board under the lock (the plain start, as before)."""
    if not take_lock(lock):
        return
    try:
        if probe(port) == "free":  # another hook may have won while we waited
            print(start(serve, repo, slug, port, board_dir))
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def said_without_statement(serve, repo: Path, slug: str, port: int, board_dir: Path, state: Path, kind: str, reason: str, lock: Path, locked: bool) -> str | None:
    """The line for a proof that is not a statement the hook acts on: the key file is not private, an answer no board gives, no answer, the hook's own
    failure to connect, or a refused connection (the board went away). Never a stop."""
    if kind == "key_not_private":
        return line_key_not_private(slug, state)
    if kind == "odd":
        return line_odd_answer(slug, port, state, reason)
    if kind == "mute":
        return line_no_answer(slug, port, state)
    if kind == "unasked":
        return when_unasked(serve, repo, slug, port, board_dir, state, reason, lock, locked)
    if kind == "free":
        if locked:
            return start(serve, repo, slug, port, board_dir)
        plain_start(serve, repo, slug, port, board_dir, lock)
        return None
    return line_cannot_establish(slug, port, f"the proof gave {kind}", state)


def when_unasked(serve, repo: Path, slug: str, port: int, board_dir: Path, state: Path, detail: str, lock: Path, locked: bool) -> str | None:
    """The hook could not connect to the port (an error other than refused), so the port says nothing, and the record decides:

      the recorded pid is dead (gone, or a zombie)  -> the board is not running: start it. A bind that fails ends the new process, which says why in
                                                       its log, and that is said truthfully;
      the recorded pid is alive, or there is no record  -> say the hook could not check, and do nothing.

    Never a signal and never "held by a program that is not this board's server": that is said only after a statement by a listener."""
    pid = recorded_pid(repo, port, state)
    if pid is None or not _pid_gone(pid):
        return line_not_checked(slug, port, detail)
    if not locked and not take_lock(lock):
        return None  # another hook is starting it
    try:
        pid = recorded_pid(repo, port, state)  # another hook may have started it since: its record names a live process
        if pid is not None and not _pid_gone(pid):
            return None
        outcome, text, new_pid = launch_checked(serve, repo, slug, port, board_dir)
    finally:
        if not locked:
            try:
                lock.unlink()
            except OSError:
                pass
    if outcome == "up":
        return f'project-init: status board "{slug}" was not running; started it again: {text}'
    if outcome == "unchecked":
        return line_started_unchecked(slug, new_pid or 0)
    if outcome == "exited":
        return line_exited(slug, text, board_dir / "serve.log")
    return text


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
