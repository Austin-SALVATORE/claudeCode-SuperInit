#!/usr/bin/env python3
"""Status board server — one engine, one board per project.

    ./serve.py --repo /path/to/project [--port N] [--name "Display Name"]

The board has two halves, and only one of them can go stale.

  Machine facts  — branch, upstream SHA, unpushed commits, working tree.
                   Re-derived from git on EVERY request. Cannot be stale,
                   because nothing remembers them.
  Judgement      — headline, decisions, findings, what is running.
                   Authored in the project's narrative.json. CAN be stale,
                   so the board reports its age rather than pretending.

Test counts sit between the two: genuinely measured, but too slow to run per
request. They are cached in narrative.json with the timestamp of the run, and
the board shows how old they are.

Per-project state lives in boards/<slug>/narrative.json. The port is derived
from the repo path, so a given project always lands on the same URL and two
projects never collide.

Binds 127.0.0.1 by default. The page itself holds no data and is open; the
report and every write need a token for this board and this run of it, sent as
the header `X-Board-Token` (release 2.4.0: until then any program that could
reach the port read and wrote). Only the opener makes a token, from the board
key `servers/board.key` (0600, the owner's), for a board that has proved it
holds the key; the board's own page keeps it in its tab's sessionStorage, and
it dies with the run of the server that issued it. There is no cookie: a cookie
goes to every port of the host. The shots are asked for by a signed address the
report gives. A write also needs the board's own `Origin` and only
answers a question the board asks. `--host 0.0.0.0` makes it reachable from a
phone on the same network and requires a token, printed once at startup and
stored in the state directory. The token is not real security: it travels in a
URL over plaintext HTTP. It stops casual access from other devices on a shared
network and makes the exposure deliberate rather than a side effect of a flag.

Read-only against the repo: it runs git in reporting modes and never writes
to the working tree.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


# The board's server with a queue of 128 waiting connections, not the standard 5 (15 Nov 2026, P2-FLAKE.md): with 5,
# a burst of 8 connections at one instant (a script, many tabs) could be reset by the kernel before the server saw
# them (macOS: ECONNRESET, EPIPE, or EINVAL from setsockopt), and the owner's tap would show "NOT recorded". Nothing
# the server had accepted was ever lost. Shadows the imported name, here and not at the places the server is made,
# so that this is one hunk that no other change to `main` has to share.
class ThreadingHTTPServer(ThreadingHTTPServer):  # noqa: F811
    request_queue_size = 128
    # The board's port is the board's alone while it runs: no SO_REUSEPORT, which would let a second listener share it. SO_REUSEADDR (the
    # stdlib default here) is kept so that a restart does not wait out TIME_WAIT; measured on macOS it lets nobody share the address: a
    # second bind to 127.0.0.1:<port> is refused whatever options either socket has.
    allow_reuse_port = False


# Sibling modules in this same directory, not standalone scripts invoked
# from elsewhere — unlike the hooks/ scripts, which duplicate this file's
# helpers rather than import them because they run from a different
# working directory. `serve.py`'s own directory is on `sys.path[0]`
# automatically, so these imports need nothing extra. `orca_crew` reads who is
# working from Orca when the project is run there; `workstreams` shapes the
# workstream list. Importing either starts nothing: hooks/revive-board.py
# imports this file only for `slug_for` and `port_for`.
import orca_crew
import workstreams

# The front door (front_door.py): one fixed port that lists every board and
# redirects /<name> to the board's own port. Imported here for the one number
# `port_for` has to keep clear of; the door itself is started in `__main__`.
# It does not import this file back, it is handed it (`FrontDoor(serve)`).
import front_door

HERE = Path(__file__).resolve().parent
PORT_BASE, PORT_SPAN = 8800, 120
# How much of a root transcript is read to learn whose it is. Session files
# reach tens of megabytes; the `agentName` that identifies a pane teammate sits
# within the first few hundred bytes, so one megabyte is a wide margin.
TRANSCRIPT_HEAD = 1_000_000
# What opens the first prompt of a worker that Orca dispatched. A session whose
# transcript begins with it is a worker's, never the lead's (measured 12 Nov
# 2026, Orca 1.4.257: byte 47,405 of a worker's transcript, in its first
# `"type":"user"` record).
# The project's worktrees are asked of git this often, not on every poll: the
# page polls every 3 s per open tab and the list changes when a worker gets a
# new worktree, which the next refresh after this many seconds picks up.
PATHS_TTL_S = 30.0
ORCA_WORKER_PROMPT = re.compile(
    r"terminal handle is: term_[0-9a-f-]{8,}.{0,200}?Your task ID is: task_[0-9a-f]{6,}",
    re.DOTALL,
)

# Board state lives in the user's home, never beside this file. Installed as a
# plugin, this directory is replaced wholesale on every update — boards written
# here would be destroyed by an upgrade, silently and with no backup.
# STATUS_BOARD_HOME overrides it; index.html still loads from HERE, because that
# is code and ships with the plugin.
STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()


# What a caller is told when the board refuses it, in fixed sentences: the detail of anything that went wrong is in the board's log
# (`Board.log_detail`), never in a body: an exception's text can name a path of this machine.
# The banner words of a browser the board does not know: they depend on where the page keeps its token, so they are chosen by
# TOKEN_STORE_KIND below (the constant is defined with the token's other constants).
KEY_SENTENCES = {
    "sessionStorage": (
        "This board opens from Claude Code: click it in the row above the prompt of this project's session, "
        'or tell Claude "open the board". Each browser tab is opened that way, so no other program can open it as you.'
    ),
    "localStorage": (
        "This board needs your key once after each start: press open in the row of this project's session, "
        'or tell Claude "open the board".'
    ),
}
OPEN_REFUSED_SENTENCE = "This opening has expired; click the board in the row of this project's session again."
SITE_SENTENCE = (
    "This board gives its data only to its own page, or to an address typed in the browser; "
    "not to a page of another site or of another port."
)
HOME_SENTENCE = (
    "The status-board home is not private to this account and cannot be made so, so this board does not start: "
    "a board's files are readable by anyone who can read its folder. Fix the owner or the permissions of the folder named below, "
    "then start the board again."
)
NO_KEY_SENTENCE = (
    "This board can't check who is asking: its key, servers/board.key under the status-board home, is missing or not private. "
    "It shows nothing and records nothing. Make that file readable by its owner only, or delete it to get a new one, "
    "then start the board again."
)
OPTIONS_ONLY_SENTENCE = "This question takes one of its listed options only."
GONE_SENTENCE = "This question is no longer on the board, so the answer was not recorded."
REPORT_FAILED_SENTENCE = "The board could not build its report; its log says why."
WRITE_FAILED_SENTENCE = "The board could not record this; its log says why."
BAD_JSON_SENTENCE = "The body is not valid JSON."
# What the browser holds: a TOKEN for this one board and this run of it, never the key, made by the opener (open_board.py) from the board's
# key, kept by the board's own page in its tab's own storage and sent as the header `X-Board-Token`; it is good for 30 days and renewed
# when a day old. There is no cookie: a cookie belongs to a host, so the browser sends it to every port of 127.0.0.1, to a server of any
# account that the owner opens there (measured in Chromium 151 and WebKit 26.5). Storage belongs to one origin and no browser sends it
# by itself.
#
# The token is bound to the board's RUN, a random id this process makes at its start and keeps in memory only. A program that takes a
# stopped board's port (and is sent the open tab's token, or reads it from the origin's storage) gets the token of a run that has
# ended, and the board's next start refuses it: it can be neither read with nor replayed, and its renewal dies with it. The tab keeps
# it in sessionStorage (this tab only) rather than localStorage (shared by every tab of the origin, so a program's page loaded in a new
# tab while the port is held would read it): TOKEN_STORE_KIND here and TOKEN_STORE_KIND in index.html are the two places, and a test
# says they agree.
TOKEN_VERSION = "v2"
SHOT_VERSION = "v1"  # a signed shot address is not bound to the run: it lives ten minutes, and the pictures' <img> cannot send a header
PROOF_VERSION = "v2"
TOKEN_HEADER = "X-Board-Token"
TOKEN_STORAGE = "board-token"  # the key under which the page keeps the token
TOKEN_STORE_KIND = "sessionStorage"  # where: "sessionStorage" (this tab only) or "localStorage" (every tab of the origin)
KEY_SENTENCE = KEY_SENTENCES[TOKEN_STORE_KIND]
TOKEN_LIFE_S = 30 * 24 * 3600  # valid for 30 days from `issued`
TOKEN_RENEW_S = 24 * 3600  # a valid token this old or older is replaced by a fresh one in the report's response header
TOKEN_SKEW_S = 60  # an `issued` this far in the future is still taken (two clocks)
OPEN_TOKEN_MAX_AGE_S = 120  # `/_open` takes only a token the opener made just now
OPEN_FILE_MAX_AGE_S = 60  # and only for the opener's file that is this young
SHOT_LINK_LIFE_S = 600  # a signed shot address is valid for ten minutes
MAX_TOKEN_CHARS = 200
DECIMAL = re.compile(r"0|[1-9][0-9]*")
SHOT_SIGNATURE = re.compile(r"([1-9][0-9]*)\.([0-9a-f]{64})")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[0-9a-f]{16}")
PAGE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
LAN_DENY_SENTENCE = "Not authorised. Open the address printed by the server, including ?k=, once for each tab."
SHOT_REFUSED_SENTENCE = "This picture's address has expired or is not this board's; the page asks again."


def _digest(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("ascii"), hashlib.sha256).hexdigest()


def make_run() -> str:
    """The id of one run of the server: 16 lowercase hex characters, made at each start, kept in memory, written nowhere. Not a secret."""
    return secrets.token_hex(8)


def make_token(key: bytes, port: int, run: str, now: float | None = None) -> str:
    """`v2.<port>.<run>.<issued>.<hex HMAC-SHA256(key, "browser:v2:<port>:<run>:<issued>")>`; `key` is the raw 32 bytes of servers/board.key."""
    issued = int(time.time() if now is None else now)
    return f"{TOKEN_VERSION}.{port}.{run}.{issued}.{_digest(key, f'browser:{TOKEN_VERSION}:{port}:{run}:{issued}')}"


def token_age(token: str, key: bytes | None, port: int, run: str, now: float | None = None) -> int | None:
    """Seconds since `token` was issued (0 for one from a clock a little ahead), or None when it is not a valid token for this board
    and THIS RUN of it.

    Valid: five fields, `v2` (a `v1` token is refused), the port as a plain decimal and this board's, the run 16 lowercase hex and this
    process's, `issued` a plain decimal, the digest 64 lowercase hex characters equal to the HMAC (compared in constant time), and
    `0 <= now - issued <= 30 days` with 60 seconds allowed for the future."""
    if not key or not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != TOKEN_VERSION:
        return None
    _, claimed_port, claimed_run, issued, digest = parts
    if not (DECIMAL.fullmatch(claimed_port) and RUN_ID.fullmatch(claimed_run) and DECIMAL.fullmatch(issued) and HEX_DIGEST.fullmatch(digest)):
        return None
    if int(claimed_port) != port or not hmac.compare_digest(claimed_run.encode("ascii"), run.encode("ascii")):
        return None
    message = f"browser:{TOKEN_VERSION}:{claimed_port}:{claimed_run}:{issued}"
    if not hmac.compare_digest(digest.encode("ascii"), _digest(key, message).encode("ascii")):
        return None
    age = int(time.time() if now is None else now) - int(issued)
    if age < -TOKEN_SKEW_S or age > TOKEN_LIFE_S:
        return None
    return max(age, 0)


def sign_shot(key: bytes, port: int, name: str, now: float | None = None) -> str:
    """The `s` of a signed shot address `shot/<name>?s=<expires>.<hex HMAC-SHA256(key, "shot:v1:<port>:<name>:<expires>")>`, valid ten minutes."""
    expires = int(time.time() if now is None else now) + SHOT_LINK_LIFE_S
    return f"{expires}.{_digest(key, f'shot:{SHOT_VERSION}:{port}:{name}:{expires}')}"


def shot_signature_ok(value: str, key: bytes | None, port: int, name: str, now: float | None = None) -> bool:
    found = SHOT_SIGNATURE.fullmatch(value or "")
    if not key or not found:
        return False
    expires, digest = found.groups()
    if not hmac.compare_digest(digest.encode("ascii"), _digest(key, f"shot:{SHOT_VERSION}:{port}:{name}:{expires}").encode("ascii")):
        return False
    return int(time.time() if now is None else now) <= int(expires)


def policy_for(script: str) -> str:
    """The page's content security policy for its one inline script (without `frame-ancestors`, which every response gets in
    `end_headers`): no script runs but that one, by its hash, and nothing can be sent anywhere but to the board's own origin."""
    source = "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
    return (
        f"default-src 'none'; script-src {source}; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'"
    )


class BoardError(ValueError):
    """A refusal whose sentence this server writes itself: it is safe to send to the caller as it is."""


class OptionsOnly(BoardError):
    """An answer in the owner's own words to a decision that says `"other": false`: only one of its listed options is taken."""


class QuestionGone(BoardError):
    """An answer to a question the board does not ask and has not shown in the last 30 minutes."""


def slug_for(repo: Path) -> str:
    return repo.name.lower().replace(" ", "-")


def port_for(repo: Path) -> int:
    # crc32, not hash(): hash() is salted per process and would move the port
    # on every restart. The whole point is that the URL is stable.
    port = PORT_BASE + zlib.crc32(str(repo).encode()) % PORT_SPAN
    # The base port is the front door's (14 Nov 2026, the owner's choice,
    # http://localhost:8800): a board on it would hold the address that lists
    # all the others. One path in 120 hashes there; it gets the first port past
    # the span instead, and its address by name does not change. Always 8800,
    # whatever STATUS_BOARD_FRONT_PORT says (the review, finding 4): a process
    # with the variable set must give a project the same port as one without,
    # or a hook and the server it revives would disagree. Nothing else is
    # skipped, so a door port configured inside the span would be taken by a
    # board: `front_port_problem` refuses such a configuration at start (the
    # re-check of 14 Nov 2026, finding 4), and the answer here never depends
    # on the environment.
    if port == PORT_BASE:
        port = PORT_BASE + PORT_SPAN
    return port


def front_port_problem(port: int | None = None) -> str | None:
    """One line saying why this front door port cannot be used, or None. Boards take their own ports from
    PORT_BASE+1 to PORT_BASE+PORT_SPAN (the last is where the path that hashed to the base one goes); a door there
    would collide with a board, and `port_for` would have to dodge it differently in every process."""
    port = front_door.FRONT_PORT if port is None else port
    low, high = PORT_BASE + 1, PORT_BASE + PORT_SPAN
    if low <= port <= high:
        return (f"{front_door.FRONT_ENV}={port} is inside {low} to {high}, where the boards take their own ports; "
                f"the front door needs a port outside it (the default is {PORT_BASE}). Unset it or choose another.")
    return None


class Board:
    def __init__(self, repo: Path, name: str | None):
        self.repo = repo
        self.slug = slug_for(repo)
        self.name = name or repo.name
        self.dir = STATE / "boards" / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)
        # Which project this board folder belongs to (14 Nov 2026). The folder
        # is named after the project's folder, so two projects with one folder
        # name share it, an old defect that stays as it is. Written once, only
        # if missing; the plugin's hooks stay silent in any other project (the
        # review of the answer waiter, finding 1). Nothing is moved or refused:
        # a second project is told in one line and starts as before.
        # Two starts at once end with one owner: the first to link its record
        # into place wins and the other reads it (the review of the keeper,
        # finding 1). And a record that names a folder that no longer exists
        # (the project was moved) is taken over by the project that starts now,
        # and the line says so.
        repo_file = self.dir / "repo.json"

        def recorded_owner() -> str | None:
            try:
                found = json.loads(repo_file.read_text()).get("repo")
            except (OSError, ValueError, AttributeError):
                return None
            return found if isinstance(found, str) else None

        def write_record(replace: bool) -> bool:
            # The record is written whole beside the file, then put in place: a reader never sees half of it.
            # To create, `link` is the atomic step, and it fails if the file exists: the first writer wins.
            tmp = self.dir / f"repo.json.{os.getpid()}.tmp"
            try:
                tmp.write_text(json.dumps({"repo": str(repo)}))
                if replace:
                    tmp.replace(repo_file)
                else:
                    try:
                        os.link(tmp, repo_file)
                    except FileExistsError:
                        return False
                    except OSError:  # a filesystem without hard links: create it exclusively and write at once
                        with open(repo_file, "x") as fh:
                            fh.write(json.dumps({"repo": str(repo)}))
                return True
            except OSError:
                return False
            finally:
                tmp.unlink(missing_ok=True)

        owner = recorded_owner()
        if owner is None and not repo_file.exists():
            if not write_record(replace=False):
                owner = recorded_owner()  # lost the race: the winner's record
        elif isinstance(owner, str) and Path(owner) != repo and not Path(owner).exists():
            if write_record(replace=True):
                print(f"This board belonged to {owner}, which no longer exists; it now belongs to {repo}.")
                owner = None
        if isinstance(owner, str) and Path(owner) != repo:
            print(
                f"This board folder already belongs to {owner}; two projects named {self.slug} share it. "
                "Rename one folder to give each its own board."
            )
        self.narrative = self.dir / "narrative.json"
        # Two files, two authors. The owner writes backlog.json from the page;
        # Claude writes narrative.json. Keeping them apart means a narrative
        # rewrite can never clobber something the owner just typed.
        self.backlog = self.dir / "backlog.json"
        # Answers are recorded by the page the instant the owner taps, so a
        # question leaves "Needs you" whether or not Claude remembers to move
        # it. Twice in one session an answered question stayed up and the
        # owner answered it again; discipline did not fix that, this does.
        self.answers = self.dir / "answers.json"
        # Who is working, when the project is run under Orca. It reads in the
        # background and is asked for its last snapshot, so a missing or slow
        # `orca` never delays a request (see orca_crew.py).
        self.orca = orca_crew.Probe()
        # Requests are served in parallel (ThreadingHTTPServer), and every
        # file the server writes is read, changed and written back. Without
        # a lock two taps a few milliseconds apart (the Orca pane and a
        # phone, say) both read the same answers.json and the second write
        # drops the first answer; worse, both write the same `.tmp` file, and
        # an interleaved write leaves answers.json unparseable, after which
        # the next tap starts again from an empty file. One lock, held for
        # the whole read-change-write, here and for the backlog and the
        # section ages.
        self._write_lock = threading.Lock()
        # The questions this server sent in a report, with the time, for `asked` (see there). Kept in `shown.json` of the board's folder
        # (0600), so that a restart does not lose it: a tab shows Q, the board restarts, the agent withdraws Q meanwhile, and the owner's
        # tap is still taken. It matters for the phone: its tab keeps its LAN token across a restart (that token is not bound to a run),
        # so it can still answer a question withdrawn while the board was down; this file is what remembers that it was shown. (The
        # token of this machine's own tab dies with the run, so there a restart ends the tab's taps before a question is looked at.)
        # Read back with `load_shown`, which trusts a file only if it is the owner's, private, a regular file, small and well formed,
        # and never for more than SHOWN_TTL_S from now.
        self._shown: dict[str, tuple[float, frozenset | None]] = {}  # question -> (when sent, its listed labels when it takes no free text)
        self._shown_saved = 0.0  # when shown.json was last written
        self._shown_save_failed = False
        self._shown_lock = threading.Lock()
        self.load_shown()  # what the last run of this board had shown, if its file is trustworthy
        self._paths: tuple[float, frozenset[str]] | None = None
        if not self.narrative.exists():
            self.narrative.write_text(
                json.dumps(
                    {
                        "writtenAt": datetime.now(timezone.utc).astimezone().isoformat(),
                        "headline": f"{self.name} — nothing reported yet",
                        "decisions": [],
                        "inFlight": [],
                        "findings": [],
                        "queued": [],
                        "open": [],
                    },
                    indent=2,
                )
                + "\n"
            )

    # ── git, read-only ────────────────────────────────────────────────
    def git(self, *args: str, default: str = "") -> str:
        try:
            out = subprocess.run(
                ("git", *args),
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return default
        return out.stdout.strip() if out.returncode == 0 else default

    def upstream(self) -> str | None:
        """The branch this one is tracking, however the project names it."""
        for probe in (
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
            ("rev-parse", "--abbrev-ref", "origin/HEAD"),
        ):
            ref = self.git(*probe)
            if ref and ref != "@{u}":
                return ref
        return None

    def unpushed(self, up: str | None) -> list[dict]:
        if not up:
            return []
        rows = []
        for line in self.git("log", "--format=%h\x1f%s", f"{up}..HEAD").splitlines():
            sha, _, subject = line.partition("\x1f")
            if sha:
                rows.append({"sha": sha, "what": subject})
        return rows

    # ── backlog: the one part of the board the owner writes ───────────
    KINDS = ("bug", "idea", "task", "question")

    def load_backlog(self) -> list[dict]:
        if not self.backlog.exists():
            return []
        try:
            rows = json.loads(self.backlog.read_text())
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def save_backlog(self, rows: list[dict]) -> None:
        tmp = self.backlog.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.backlog)  # atomic: a crash mid-write cannot truncate

    def add_item(self, payload: dict) -> dict:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise BoardError("An item needs a title.")
        with self._write_lock:
            rows = self.load_backlog()
            item = {
                "id": f"you-{len(rows) + 1}-{int(datetime.now().timestamp())}",
                "title": title[:300],
                "kind": payload.get("kind") if payload.get("kind") in self.KINDS else "task",
                "notes": str(payload.get("notes", "")).strip()[:2000],
                "createdAt": datetime.now(timezone.utc).astimezone().isoformat(),
                "state": "needs-triage",
                "source": "you",
            }
            self.save_backlog([*rows, item])
        return item

    def set_state(self, item_id: str, state: str) -> dict | None:
        # The one value the page sends is "done" (its tick). Any other string used to be stored as the caller sent it, of any
        # length; the state of an item is not a place to put text.
        if state != "done":
            raise BoardError("Only the value done can be recorded for a backlog item.")
        with self._write_lock:
            rows = self.load_backlog()
            for index, row in enumerate(rows):
                if row.get("id") == item_id:
                    changed = {**row, "state": state}
                    self.save_backlog([*rows[:index], changed, *rows[index + 1 :]])
                    return changed
        return None

    def backlog_view(self, narrative: dict) -> list[dict]:
        """Owner-written items first, then whatever Claude parked, both aged.

        Two Claude-authored channels feed this one list: `open` (an item
        that already has enough shape to name who it's waiting on) and
        `queued` (known future work an agent named but deliberately did not
        do — the skill's routing rule for a deliverable's "not in this
        commit" section). They were always the same fact under two names:
        something the owner needs to see without asking, not in flight, not
        yet owned — and the field mapping already agreed before anything
        used it (`row.get("who") or row.get("note")` accepts `queued`'s
        shape unmodified). Found and fixed together, 3 Aug 2026: `queued`
        was documented in the skill and rendered nowhere — every entry an
        agent wrote there reached `report.json` and stopped. Folding it in
        here, rather than giving it a second list next to this one, is the
        fix for the whole shape of the defect, not just this instance of
        it: one rail, so the owner checks one place.
        """
        out = []
        for row in self.load_backlog():
            if row.get("state") == "done":
                continue
            out.append({**row, "ageDays": age_days(row.get("createdAt"))})
        for key in ("open", "queued"):
            for row in narrative.get(key, []):
                out.append(
                    {
                        "title": row.get("what", ""),
                        "code": row.get("code"),
                        "notes": row.get("who") or row.get("note"),
                        "state": row.get("state", "unowned"),
                        "kind": row.get("kind", "task"),
                        "source": "claude",
                        "ageDays": age_days(row.get("since")),
                    }
                )
        return out

    # ── pipeline: commit-backed steps answer to git, not to me ────────
    def resolve_pipeline(self, narrative: dict, upstream: str | None) -> dict | None:
        """A step carrying a `sha` derives its own status.

        Hand-maintained step statuses go stale the moment something ships and
        nobody notices — it happened three times in one session. Anything
        backed by a commit is now measured: on the upstream means shipped,
        local-only means held. Steps with no `sha` keep what they were told.
        """
        pipe = narrative.get("pipeline")
        if not pipe:
            return None
        steps = []
        for step in pipe.get("steps", []):
            sha = step.get("sha")
            if sha:
                exists = self.git("cat-file", "-e", f"{sha}^{{commit}}", default="?") != "?"
                if not exists:
                    status = step.get("status", "todo")
                elif upstream and self.git(
                    "merge-base", "--is-ancestor", sha, upstream, default="?"
                ) != "?":
                    status = "shipped"
                else:
                    status = "held"
                steps.append({**step, "status": status, "derived": True})
            else:
                steps.append(step)
        return {**pipe, "steps": steps}

    # ── section ages: when did a STATE section last actually change ───
    #
    # `writtenAt` answers "when was the file last saved" — it bumps on
    # every write regardless of which part changed, so a `pipeline` that
    # has been silently frozen for days behind an otherwise-fresh
    # `written 2 min ago` stamp reads as current. That is not hypothetical:
    # it is the failure this exists for (3 Aug 2026, a progress rail two
    # milestones behind while findings kept landing; separately, 1 Aug
    # 2026, `inFlight` said "building Phase 0" while `pipeline` still read
    # `todo` — both hand-maintained, both individually plausible, and nine
    # months from now a fresh set of eyes would have no way to tell the
    # difference between "just written" and "frozen for a week" without
    # this).
    #
    # Deliberately NOT a field the narrative's author writes. A hand-typed
    # `pipelineUpdatedAt` would be exactly the second source of truth that
    # already burned this project once — see `resolve_pipeline`'s own
    # docstring, same lesson, one layer up: a claim that can be typed can
    # be typed wrong, or left stale, with nothing to catch it. Derived
    # instead as a content hash, compared against the previous poll, and
    # persisted in a file this server owns exclusively — `narrative.json`'s
    # author never reads or writes it, same posture as `answers.json`
    # belonging to the page rather than to Claude.
    #
    # Scope, deliberately narrow: only `pipeline` and `inFlight` for now —
    # the two sections actually caught going stale, and the two with an
    # unambiguous reading of "unchanged is a defect" (both are the lead's
    # own claim about current work, nothing else drives them). `decisions`
    # shrinks when the OWNER answers a question, not when the lead edits —
    # so "unchanged" there could mean "nobody has asked anything new",
    # which is not obviously a defect the way a frozen pipeline is. Worth
    # revisiting with its own reasoning, not folded in by default.
    #
    # `workstreams` joined on 12 Nov 2026 for a different reason: the list is
    # a copy of the project's register, and the page dates it ("list updated
    # 26 min ago") from this hash. Unlike the other two, an unchanged list is
    # normal (a register can sit for days), so the page shows this age without
    # the amber. What it can prove is drift from the register file itself:
    # see workstreams.source_check.
    STATE_SECTIONS = ("pipeline", "inFlight", "workstreams")

    def section_ages(self, narrative: dict) -> dict[str, str]:
        """ISO timestamp each tracked section's content last differed from
        the previous poll. A brand-new board (no prior record) reports
        every section as changed just now, not as ancient — there is no
        history before the first measurement, and treating silence as
        staleness before anything has actually been observed would be a
        false alarm on every board's first request.

        Known, stated limitation rather than a silently-assumed one: this
        cannot see staleness that predates its own deployment. A `pipeline`
        that had already sat frozen for two days before this shipped reads
        as freshly-changed on the first request after the upgrade, because
        the only history available is what this method itself recorded.
        """
        with self._write_lock:
            return self._section_ages(narrative)

    def _section_ages(self, narrative: dict) -> dict[str, str]:
        path = self.dir / "section-ages.json"
        try:
            stored: dict = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        now = datetime.now(timezone.utc).astimezone().isoformat()
        dirty = False
        out = {}
        for key in self.STATE_SECTIONS:
            digest = hashlib.sha256(
                json.dumps(narrative.get(key), sort_keys=True, ensure_ascii=False).encode(
                    "utf-8"
                )
            ).hexdigest()
            prev = stored.get(key)
            if not isinstance(prev, dict) or prev.get("hash") != digest:
                stored[key] = {"hash": digest, "changedAt": now}
                dirty = True
            out[key] = stored[key]["changedAt"]
        if dirty:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(stored, indent=2))
            tmp.replace(path)  # atomic, same reasoning as save_backlog/record_answer
        return out

    # ── answers: recorded by the page, not by Claude ──────────────────
    def load_answers(self) -> dict:
        if not self.answers.exists():
            return {}
        try:
            rows = json.loads(self.answers.read_text())
            return rows if isinstance(rows, dict) else {}
        except (OSError, ValueError):
            return {}

    # The owner's answer is the one thing on this board that exists nowhere
    # else — a stale headline gets rewritten, a wrong count gets re-measured,
    # but a ruling typed here has no other copy. 20,000 characters is not a
    # rendering limit or a real constraint of anything: it is a backstop
    # against a catastrophic accidental paste (a whole log file, a whole
    # document) silently bloating answers.json forever, sized generously past
    # anything a considered written ruling would ever reach — several
    # paragraphs, in any of the languages this board is answered in, is a few
    # thousand characters at most. `len()` on a Python str counts Unicode
    # code points, not bytes, so this cap is the same size in English or in
    # CJK text; it does not shrink for scripts that use more bytes per
    # character. Found because the real limit used to be 500: an owner's
    # multi-paragraph ruling was cut mid-sentence with no mark that anything
    # was missing, and it went unnoticed because every other stored answer
    # happened to be under 120 characters. If this cap is ever hit for real,
    # it must be visible, never silent — see `truncated`/`originalLength`
    # below, both carried through to the board by `split_decisions`.
    ANSWER_MAX_CHARS = 20_000
    # The question, by contrast, is always the agent's own short title —
    # never the owner's free text — so the original 500-character cap stays;
    # it was never the field that lost anything.
    QUESTION_MAX_CHARS = 500
    # `do_POST` rejects the raw HTTP body before this class ever sees it, on
    # a byte count against `Content-Length` — and that used to be an
    # independent literal, 8192, chosen once for a payload that was always a
    # short choice or a short backlog title. Raising ANSWER_MAX_CHARS above
    # without touching that number left it as the *real* limit: every answer
    # the JSON body of which crossed 8192 bytes was rejected with a 400
    # before `record_answer` ran, silently as far as the owner could tell
    # (the client did not check the response — fixed separately, in
    # index.html). Measured live against the real endpoint: 8,160 ASCII
    # characters fit, 8,170 did not; in Chinese, 3 bytes/character, the
    # usable ceiling was roughly 2,700 characters, a third of the English
    # one — for the owner this board is actually for, this was the tighter
    # cap by far, not the character one below it.
    #
    # Derived from ANSWER_MAX_CHARS rather than stated as a second number, so
    # the two cannot drift apart silently again: worst-case UTF-8 is 4 bytes
    # per character (surrogate-pair / astral-plane code points — well above
    # ordinary CJK, which is 3), so the answer field alone can cost up to
    # ANSWER_MAX_CHARS * 4 bytes on the wire. The remaining margin covers the
    # `question` field at its own worst case (QUESTION_MAX_CHARS * 4) plus
    # the small JSON envelope (`{"q":"","answer":""}` and escaping) and the
    # unrelated `/backlog` route's payload, which shares this same gate and
    # is always far smaller.
    MAX_BODY_BYTES = ANSWER_MAX_CHARS * 4 + QUESTION_MAX_CHARS * 4 + 4096

    # The board takes only answers to the questions it asks. A question is "asked" while it is in the narrative's `decisions`
    # (answered or not: a second tap from a tab that still shows it is taken, as before), or when this server sent it in a report
    # in the last SHOWN_TTL_S seconds: the agent can rewrite or withdraw a question while the owner's tab still shows it, and the
    # owner's words must not be lost to that race. Anything else is not the owner answering the board; it is a caller inventing a
    # question, and what is recorded here is told to the session as the owner's ruling.
    SHOWN_TTL_S = 30 * 60
    SHOWN_MAX = 500

    def _question_key(self, question) -> str | None:
        """The form a question is compared in: the text it is recorded under, a number printed as text (as `split_decisions` does)."""
        if isinstance(question, (int, float)) and not isinstance(question, bool):
            question = str(question)
        if not isinstance(question, str) or not question.strip():
            return None
        return question.strip()[: self.QUESTION_MAX_CHARS]

    @staticmethod
    def closed_labels(decision) -> frozenset | None:
        """The listed option labels of a decision that says `"other": false` (the literal false, nothing else), else None: a decision
        takes free text by default. An option is a text or an object with a `label`; labels are compared as every answer is, trimmed."""
        if not isinstance(decision, dict) or decision.get("other") is not False:
            return None
        listed = decision.get("options")
        labels = set()
        for option in listed if isinstance(listed, list) else []:
            label = option.get("label") if isinstance(option, dict) else option
            if isinstance(label, (str, int, float)) and not isinstance(label, bool):
                labels.add(str(label).strip())
        return frozenset(labels)

    SHOWN_FILE = "shown.json"
    SHOWN_FILE_MAX_BYTES = 262144
    SHOWN_SAVE_EVERY_S = 60  # a poll that shows nothing new rewrites the file at most this often: a restart loses at most this much recency

    def note_shown(self, questions) -> None:
        """Remember that these questions were sent in a report just now, with the rule of each: `questions` are decisions (objects with
        a `q` and perhaps `"other": false`) or just the question texts. Written to `shown.json` when a question or a rule is new, and
        otherwise at most every SHOWN_SAVE_EVERY_S seconds."""
        now = time.time()
        with self._shown_lock:
            changed = False
            for question in questions:
                key = self._question_key(question.get("q") if isinstance(question, dict) else question)
                if key is not None:
                    rule = self.closed_labels(question)
                    previous = self._shown.pop(key, None)
                    changed = changed or previous is None or previous[1] != rule or now - previous[0] >= self.SHOWN_TTL_S
                    self._shown[key] = (now, rule)  # re-inserted at the end: the dictionary stays in order of time
            fresh = [(k, v) for k, v in self._shown.items() if now - v[0] < self.SHOWN_TTL_S]
            self._shown = dict(fresh[-self.SHOWN_MAX :])
            if changed or (self._shown and now - self._shown_saved >= self.SHOWN_SAVE_EVERY_S):
                self.save_shown(now)

    def save_shown(self, now: float) -> None:
        """`shown.json`: `{question: [time, labels or null]}`, written whole to a temporary file created 0600 and put in place. A failure
        costs the restart's memory and nothing else: it is logged once and never stops a report."""
        rows = {k: [t, sorted(v) if v is not None else None] for k, (t, v) in self._shown.items()}
        tmp = self.dir / (self.SHOWN_FILE + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(rows, ensure_ascii=False))
            os.replace(tmp, self.dir / self.SHOWN_FILE)
            self._shown_saved = now
        except OSError as exc:
            if not self._shown_save_failed:
                self._shown_save_failed = True
                self.log_detail("write shown.json", exc)

    def load_shown(self) -> None:
        """Read `shown.json` back at start. Ignored (and never an error) when it is not a regular file of this account, is readable by
        the group or others, is bigger than SHOWN_FILE_MAX_BYTES, is not JSON, or is not an object; an entry that is not
        `[time, labels or null]` is skipped; a time in the future is taken as now, and an entry older than SHOWN_TTL_S is dropped, so
        that nothing in the file is ever trusted for more than SHOWN_TTL_S from now."""
        path = self.dir / self.SHOWN_FILE
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > self.SHOWN_FILE_MAX_BYTES:
                return
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                rows = json.loads(handle.read(self.SHOWN_FILE_MAX_BYTES + 1))
        except (OSError, ValueError):
            return
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(rows, dict):
            return
        now = time.time()
        loaded = {}
        for question, entry in rows.items():
            key = self._question_key(question)
            if key is None or not isinstance(entry, list) or len(entry) != 2:
                continue
            when, labels = entry
            if isinstance(when, bool) or not isinstance(when, (int, float)) or when != when:
                continue
            if labels is not None and not (isinstance(labels, list) and all(isinstance(x, str) for x in labels)):
                continue
            when = min(float(when), now)
            if now - when < self.SHOWN_TTL_S:
                loaded[key] = (when, frozenset(labels) if labels is not None else None)
        ordered = sorted(loaded.items(), key=lambda item: item[1][0])
        with self._shown_lock:
            self._shown = dict(ordered[-self.SHOWN_MAX :])

    def question_rule(self, question) -> tuple[bool, frozenset | None]:
        """(is it asked, the listed labels when it takes no free text). The narrative's decision wins while it is there (answered or
        not); a question withdrawn within SHOWN_TTL_S keeps the rule it was sent with."""
        key = self._question_key(question)
        if key is None:
            return False, None
        try:
            narrative = json.loads(self.narrative.read_text())
        except (OSError, ValueError):
            narrative = {}
        listed = narrative.get("decisions") if isinstance(narrative, dict) else None
        for raw in listed if isinstance(listed, list) else []:
            if isinstance(raw, dict) and self._question_key(raw.get("q")) == key:
                return True, self.closed_labels(raw)
        with self._shown_lock:
            seen = self._shown.get(key)
        if seen is not None and time.time() - seen[0] < self.SHOWN_TTL_S:
            return True, seen[1]
        return False, None

    def asked(self, question) -> bool:
        return self.question_rule(question)[0]

    def log_detail(self, what: str, exc: BaseException) -> None:
        """What went wrong, for whoever reads the board's log (`boards/<slug>/serve.log`, which `revive-board.py` also opens): the
        type and the text of the exception, which can name a path. Nothing of it goes into a response body."""
        line = f"{datetime.now(timezone.utc).astimezone().isoformat()} {what}: {type(exc).__name__}: {exc}"
        line = line.replace("\r", " ").replace("\n", " ")[:2000]
        try:
            fd = os.open(self.dir / "serve.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # a log that cannot be written must not turn a refusal into a crash

    def log_note(self, text: str) -> None:
        """One line of plain text in the board's log (`serve.log`): for something the owner should know that is not an error of a request."""
        line = f"{datetime.now(timezone.utc).astimezone().isoformat()} {text}".replace("\r", " ").replace("\n", " ")[:2000]
        try:
            fd = os.open(self.dir / "serve.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def record_answer(self, question: str, answer: str, source: str = "unmarked") -> dict:
        """Record the owner's answer. `source` is "network" when the request came from another machine and was judged by the phone's
        token (which is not a proof of the owner, whatever marks the request carries), "browser" when it came from this machine with
        the browsers' same-page mark (`Sec-Fetch-Site: same-origin`) and "unmarked" otherwise; it is stored as `from`, and the session
        confirms a network or unmarked answer with the owner before it acts on anything hard to undo."""
        if question is None or answer is None:
            raise BoardError("An answer needs both a question and a choice.")
        question, answer = str(question).strip(), str(answer).strip()
        if not question or not answer:
            raise BoardError("An answer needs both a question and a choice.")
        is_asked, closed = self.question_rule(question)
        if not is_asked:
            raise QuestionGone(GONE_SENTENCE)
        if closed is not None and answer not in closed:
            raise OptionsOnly(OPTIONS_ONLY_SENTENCE)  # "other": false: the owner's words are not taken for this question
        truncated = len(answer) > self.ANSWER_MAX_CHARS
        entry = {
            "answer": answer[: self.ANSWER_MAX_CHARS],
            "at": datetime.now(timezone.utc).astimezone().isoformat(),
            "from": source if source in ("browser", "network") else "unmarked",
        }
        if truncated:
            entry["truncated"] = True
            entry["originalLength"] = len(answer)
            entry["truncatedAt"] = self.ANSWER_MAX_CHARS
        # Read, change and write back as one step (see _write_lock): the
        # file's format is unchanged, only two writers can no longer cross.
        with self._write_lock:
            rows = {**self.load_answers(), question[: self.QUESTION_MAX_CHARS]: entry}
            tmp = self.answers.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(self.answers)
        return entry

    def split_decisions(self, narrative: dict) -> tuple[list, list, int]:
        """Anything the owner has answered leaves 'Needs you' immediately.

        The note distinguishes *they told me* from *I acted on it* — those are
        different states and collapsing them is what made the board lie.
        """
        recorded = self.load_answers()
        pending, answered, unreadable = [], [], 0
        listed = narrative.get("decisions")
        for raw in listed if isinstance(listed, list) else []:
            # The question is the key an answer is recorded under, so it has
            # to be text. A number is printed as text. An entry that is not
            # an object, or has no question, cannot be answered: it is
            # counted, and the page says how many it could not show.
            q = raw.get("q") if isinstance(raw, dict) else None
            if isinstance(q, (int, float)) and not isinstance(q, bool):
                q = str(q)
            if not isinstance(q, str) or not q.strip():
                unreadable += 1
                continue
            d = raw if q is raw["q"] else {**raw, "q": q}
            hit = recorded.get(d["q"])
            if hit:
                entry = {
                    "q": d["q"],
                    "chose": hit["answer"],
                    "note": "Recorded from the board. Not yet folded into my report.",
                    "when": hit["at"][11:16],
                    # The whole timestamp as well: the page orders answered
                    # questions newest first, and "23:37" alone has no date.
                    "at": hit["at"],
                }
                # Where the answer came from (2.4.0): carried through as it is stored; an answer recorded before has no mark.
                if hit.get("from") in ("browser", "unmarked", "network"):
                    entry["from"] = hit["from"]
                # Carried through, never dropped here: a truncated answer is
                # still truncated on the board, not just in the file on disk.
                if hit.get("truncated"):
                    entry["truncated"] = True
                    entry["originalLength"] = hit["originalLength"]
                    entry["truncatedAt"] = hit["truncatedAt"]
                answered.append(entry)
            else:
                pending.append(d)
        earlier = narrative.get("answered")
        return pending, answered + (earlier if isinstance(earlier, list) else []), unreadable

    # ── who is working: Orca's record when there is one ───────────────
    def project_paths(self) -> frozenset[str]:
        """The repo and its git worktrees: every folder that is this project.

        An Orca worker often works in a worktree of the repo, not in the
        checkout the board was started for. `git worktree list` is what makes
        both the same project, measured rather than inferred from a folder
        name.
        """
        cached = self._paths
        if cached is not None and time.monotonic() - cached[0] < PATHS_TTL_S:
            return cached[1]
        paths = {os.path.realpath(self.repo)}
        for line in self.git("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                paths.add(os.path.realpath(line[len("worktree ") :].strip()))
        found = frozenset(paths)
        self._paths = (time.monotonic(), found)
        return found

    def crew(self, within_hours: float = 12.0) -> tuple[list[dict], dict]:
        """Who is working, and what the page may say about where that came from.

        Run under Orca, the rows are Orca's: the coordinator a Run names,
        its workers, and any other session in the project. Otherwise they are
        derived from transcripts as before (`_transcript_team`).

        The one thing that changes for a project Orca knows: the lead is no
        longer guessed. "Newest session in this folder" was the stand-in when
        no team config named the lead; under Orca that folder also holds the
        transcripts of workers started in the same checkout, so the guess
        attributes a worker's activity to the lead. If no Run names a
        coordinator here, the page shows none and says so.
        """
        paths = self.project_paths()
        view = self.orca.view(paths)
        known = view.snapshot is not None and orca_crew.knows_project(view.snapshot, paths)
        # While Orca's first answer is on its way, nothing is guessed either:
        # a guessed row that vanishes a second later is still a wrong row.
        lead, explicit, others = self._transcript_team(
            within_hours, guess_lead=not known and view.state != "checking"
        )
        if not known:
            return ([lead] if lead else []) + others, {"state": view.state, "known": False}
        rows = orca_crew.crew_rows(
            view.snapshot, paths, os.path.realpath(self.repo), time.time(), within_hours
        )
        named = any(r["kind"] == "coordinator" for r in rows)
        if lead and explicit and not named:
            rows = [lead] + rows  # a team config still names its lead outright
        meta = {
            "state": "ok",
            "known": True,
            "ageSec": round(max(0.0, time.time() - view.snapshot.taken)),
        }
        if not named and not (lead and explicit):
            meta["note"] = (
                "No coordinator row: no Orca Run is coordinated from a terminal "
                "in this project, so the board does not guess one."
            )
        return rows + others, meta

    # ── teammates, measured rather than reported ──────────────────────
    def _transcript_team(
        self, within_hours: float, guess_lead: bool
    ) -> tuple[dict | None, bool, list[dict]]:
        """Who has been working, and when they last moved: (lead row, whether a
        team config named it, everyone else).

        Derived from agent transcript mtimes, not from anyone's self-report —
        an agent that acts touches its transcript, so this stays true even
        when a teammate goes idle without telling anyone. Only metadata,
        mtimes and the first megabyte of root transcripts are read; no .jsonl
        body is ever opened whole (they run to tens of megabytes).

        `guess_lead` is False for a project Orca knows (see `crew`): the lead
        then comes from a team config or not at all.
        """
        mangled = "-" + str(self.repo).strip("/").replace("/", "-")
        root = Path.home() / ".claude" / "projects" / mangled
        newest: dict[str, dict] = {}
        for meta_path in root.glob("*/subagents/*.meta.json"):
            transcript = meta_path.with_suffix("").with_suffix(".jsonl")
            if not transcript.exists():
                continue
            age = (datetime.now().timestamp() - transcript.stat().st_mtime) / 60
            if age > within_hours * 60:
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            who = meta.get("name") or meta.get("agentType") or "agent"
            row = {
                "id": who,
                "who": who,
                "kind": "teammate"
                if meta.get("taskKind") == "in_process_teammate"
                else "subagent",
                "model": (meta.get("model") or "inherited").replace("claude-", ""),
                "lastMin": round(age, 1),
            }
            if who not in newest or age < newest[who]["lastMin"]:
                newest[who] = row

        # The lead, whose transcript is NOT under subagents/ — it sits at the
        # project root, one per session. Without this the board watches every
        # participant except the one writing it, which is precisely backwards:
        # every staleness signal here (git commits, teammate activity, elapsed
        # minutes) measures something *other* than the person responsible for
        # the narrative going stale. Now the owner can read "lead moved 0.2 min
        # ago" beside "written 25 min ago" and see the gap directly.
        # (Owner's observation, 1 Aug 2026.)
        # Which transcript is the lead's matters: a project directory holds one
        # .jsonl per session, so "the newest file here" can be a different
        # session entirely and would attribute someone else's activity to this
        # board. The team config names the lead session explicitly; fall back to
        # newest only when no team exists.
        teams = []
        for cfg in sorted((Path.home() / ".claude" / "teams").glob("*/config.json")):
            try:
                team = json.loads(cfg.read_text())
            except (OSError, ValueError):
                continue
            if any(
                Path(m.get("cwd", "")) == self.repo for m in team.get("members", [])
            ):
                teams.append(team)

        # Pane teammates — dev, qa, architect in their own cmux panes — leave no
        # meta.json, so the glob above sees only in-process spawns and the board
        # showed a lead working alone while three teammates were mid-batch. A
        # pane teammate is a member of the team config, and its transcript is
        # an ordinary session file at the project root, indistinguishable from
        # the lead's by name. What tells them apart is content: a teammate's
        # transcript carries `"agentName":"<name>"` in its first lines (byte
        # ~450 for dev, qa and architect, measured 21 Oct 2026); the lead's
        # carries none. So each recent root transcript has its first megabyte
        # read — only that, see TRANSCRIPT_HEAD — and is matched to a member by
        # name. Newest transcript per name wins, as for subagents above.
        panes = [
            m
            for team in teams
            for m in team.get("members", [])
            if m.get("agentType") != "team-lead"
            and m.get("backendType") != "in-process"
            and m.get("name")
        ]
        pane_names = {m["name"] for m in panes}
        # Per recent root transcript: its age, the agent names its head
        # declares, and whether it opens with an Orca worker's dispatch.
        recent: dict[Path, tuple[float, frozenset[str], bool]] = {}
        for t in root.glob("*.jsonl"):
            if not t.is_file():
                continue
            age = (datetime.now().timestamp() - t.stat().st_mtime) / 60
            if age > within_hours * 60:
                continue
            recent[t] = (age, *self._head_marks(t))
        claimed: set[Path] = set()
        for m in panes:
            who = m["name"]
            hits = [(age, t) for t, (age, names, _) in recent.items() if who in names]
            if not hits:
                continue
            age, t = min(hits, key=lambda h: h[0])
            claimed.add(t)
            row = {
                "id": who,
                "who": who,
                "kind": "teammate",
                "model": (m.get("model") or "inherited").replace("claude-", ""),
                "lastMin": round(age, 1),
            }
            if who not in newest or age < newest[who]["lastMin"]:
                newest[who] = row

        # A team config names the lead only if it is a team. With agent teams
        # switched on, every Claude Code session writes a config of its own,
        # `session-<id>`, whose one member is itself as `team-lead`; an Orca
        # worker started in this checkout writes one too. Taking the first of
        # those that has a transcript made the board show a worker's activity
        # as the lead's (12 Nov 2026: `session-030cd4b9`, the `designer`
        # worker, sorted ahead of the coordinator's `session-70e0375d`). A
        # one-member config says "this session leads itself", which every
        # session can say; it decides nothing, so only a config with a
        # teammate in it is believed.
        lead_file, explicit = None, False
        for team in teams:
            if not any(m.get("agentType") != "team-lead" for m in team.get("members", [])):
                continue
            candidate = root / f"{team.get('leadSessionId')}.jsonl"
            if candidate.exists():
                lead_file, explicit = candidate, True
                break
        if lead_file is None and guess_lead:
            # The configured lead session can be gone — a team config outlives
            # the session that wrote it (21 Oct 2026: e41c6c61 named, no such
            # transcript). "Newest root transcript" then stands in, but not one
            # that is a teammate's: before pane teammates were recognised, the
            # newest file here was routinely the dev's, and the board reported
            # its activity as the lead's. Nor one that is an Orca worker's,
            # for the same reason: when the `orca` CLI cannot be asked, a
            # worker started in this checkout must still not pass for the lead.
            #
            # There used to be a second stand-in after this one: the newest
            # root transcript of any age and any owner. It could only do two
            # things: pick a file too old to show (no row either way), or pick
            # one this list had just excluded as a teammate's. It is gone.
            others = [
                t
                for t, (_, names, worker) in recent.items()
                if t not in claimed and not worker and not (names & pane_names)
            ]
            lead_file = max(others, key=lambda t: t.stat().st_mtime, default=None)

        rest = sorted(newest.values(), key=lambda r: r["lastMin"])
        if lead_file is not None:
            try:
                age = (datetime.now().timestamp() - lead_file.stat().st_mtime) / 60
            except OSError:
                age = None
            if age is not None and age <= within_hours * 60:
                rows_lead = {
                    "id": "team-lead",
                    "who": "team-lead",
                    "kind": "lead",
                    "model": (self._lead_model(lead_file) or "inherited").replace(
                        "claude-", ""
                    ),
                    "lastMin": round(age, 1),
                }
                return rows_lead, explicit, rest
        return None, False, rest

    # Keyed by path: (bytes the head covered, agentNames found in it, whether
    # it opens with an Orca dispatch). Once a file is past TRANSCRIPT_HEAD its
    # head can no longer change, so that entry is final; a shorter file is
    # re-read whenever it has grown.
    _head_marks_cache: dict[str, tuple[int, frozenset[str], bool]] = {}

    def _head_marks(self, path: Path) -> tuple[frozenset[str], bool]:
        """What a root transcript's head says about whose session it is: the
        `"agentName"` values it declares (a pane teammate), and whether its
        first prompt is an Orca dispatch (a worker).

        Reads at most TRANSCRIPT_HEAD bytes, for the same reason _lead_model
        reads only a tail: this runs on every request against files that reach
        tens of megabytes.
        """
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return frozenset(), False
        hit = self._head_marks_cache.get(key)
        if hit is not None and (hit[0] >= TRANSCRIPT_HEAD or hit[0] == size):
            return hit[1], hit[2]
        try:
            with path.open("rb") as fh:
                blob = fh.read(TRANSCRIPT_HEAD).decode("utf-8", "ignore")
        except OSError:
            return frozenset(), False
        names = frozenset(re.findall(r'"agentName":"([^"]{1,64})"', blob))
        worker = _opens_with_dispatch(blob)
        self._head_marks_cache[key] = (min(size, TRANSCRIPT_HEAD), names, worker)
        return names, worker

    _lead_model_cache: dict[str, str] = {}

    def _lead_model(self, path: Path) -> str:
        """The lead's model, read from the tail of its own transcript.

        These files reach tens of megabytes, and this runs on every request, so
        only the last 64 KB is read rather than scanning the whole thing.

        Only `claude-*` values are accepted: a lead transcript also records the
        `model` argument of every spawn it makes, so a bare "sonnet" from a
        teammate dispatch sits in the same file and would otherwise be reported
        as the lead's own model.
        """
        key = str(path)
        if key in self._lead_model_cache:
            return self._lead_model_cache[key]
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 65536))
                blob = fh.read().decode("utf-8", "ignore")
        except OSError:
            return ""
        found = re.findall(r'"model"\s*:\s*"(claude-[^"]{1,40})"', blob)
        model = found[-1] if found else ""
        if model:
            self._lead_model_cache[key] = model
        return model

    def tree_state(self) -> tuple[str, int]:
        porcelain = self.git("status", "--porcelain", default="\x00")
        if porcelain == "\x00":
            return "unknown", 0
        if not porcelain:
            return "clean", 0
        n = len(porcelain.splitlines())
        return f"{n} file{'s' if n != 1 else ''} dirty", n

    # ── report ────────────────────────────────────────────────────────
    def report(self) -> dict:
        narrative = json.loads(self.narrative.read_text())
        up = self.upstream()
        if up:
            remote, _, branch = up.partition("/")
            self.git("fetch", "--quiet", remote, branch)  # keeps `upstream` honest

        held = self.unpushed(up)
        tree, dirty = self.tree_state()
        checks = narrative.get("checks", {})

        state = {
            "branch": self.git("rev-parse", "--abbrev-ref", "HEAD", default="?"),
            "local": self.git("rev-parse", "--short", "HEAD", default="?"),
            "upstream": self.git("rev-parse", "--short", up, default="no remote")
            if up
            else "no remote",
            "unpushed": f"{len(held)} commit{'s' if len(held) != 1 else ''}"
            if held
            else "none",
            "tree": tree,
        }
        # There is no `lock` row: the tree lock was retired on 15 Nov 2026 (2.2.0).
        # Under Orca the rule is one writing worker per worktree, and the page
        # prints whatever rows `state` has, so nothing there changed. A lock
        # file that an older version left in a board's folder is read by
        # nothing, and is left where it is.
        for label in ("tests", "typecheck", "lint", "build"):
            if label in checks:
                state[label] = checks[label]

        # Staleness the board can prove, rather than trusting whoever writes it.
        #
        # The narrative is judgement and can rot; git is re-derived every
        # request and cannot. When a commit lands *after* the last narrative
        # rewrite, the words on this page describe a repository that has since
        # moved — and the page was already displaying the new SHA beside them.
        # It had both halves and never compared them. (1 Aug 2026: a pipeline
        # read "Phase 0 — todo" next to `local: e2db646`, the commit that
        # finished Phase 0. The owner spotted it; nothing on the page did.)
        #
        # Reported as a fact, not a warning: how many commits landed since, and
        # the newest subject. Whether that matters is judgement, and judgement
        # is the half that can be wrong.
        stale = None
        head_iso = self.git("log", "-1", "--format=%cI")
        written = str(narrative.get("writtenAt") or "")
        if head_iso and written:
            try:
                if datetime.fromisoformat(head_iso) > datetime.fromisoformat(written):
                    since = self.git(
                        "log", "--format=%h\x1f%s", f'--since={written}'
                    ).splitlines()
                    stale = {
                        "commitsSince": len([r for r in since if r.strip()]),
                        "newest": (since[0].split("\x1f", 1)[-1] if since else ""),
                        "at": head_iso,
                    }
            except ValueError:
                stale = None

        pending, answered, unreadable = self.split_decisions(narrative)

        # The workstream list is the agent's; who is on each workstream is
        # not. A worker is linked to a workstream by the id in its Task title,
        # read from Orca, so the list can never claim a worker that is not
        # there, nor miss one that is.
        crew, orca = self.crew()
        streams = workstreams.normalise(narrative.get("workstreams"))
        if streams is not None:
            crew = workstreams.link(crew, workstreams.ids_of(streams))
            drift = workstreams.source_check(self.repo, streams["source"], self.narrative)
            if drift is not None:
                streams = {**streams, "sourceCheck": drift}

        out = dict(narrative)
        out.pop("checks", None)
        out.update(
            {
                "project": self.name,
                "decisions": pending,
                "unreadableDecisions": unreadable,
                "answered": answered,
                "pipeline": self.resolve_pipeline(narrative, up),
                "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
                "state": state,
                "held": held,
                "team": crew,
                "orca": orca,
                "workstreams": streams,
                "backlog": self.backlog_view(narrative),
                "freshness": {
                    "narrativeAgeMin": age_minutes(narrative.get("writtenAt")),
                    "checksAgeMin": age_minutes(checks.get("at")),
                    "dirtyFiles": dirty,
                    "commitsSinceNarrative": stale,
                    "sections": self.section_ages(narrative),
                },
            }
        )
        return out


def age_minutes(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    # Clamped: a hand-stamped writtenAt is often a few minutes ahead of the
    # clock, and "written -5 min ago" reads as a bug rather than as rounding.
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 60)


def age_days(stamp: str | None) -> float | None:
    mins = age_minutes(stamp)
    return None if mins is None else round(mins / 1440, 2)


def _opens_with_dispatch(head: str) -> bool:
    """True when a transcript's first prompt is an Orca worker's dispatch.

    Only the records before the first assistant turn are looked at. Later in
    a session the same words can appear in anybody's transcript (a
    coordinator that prints a worker's preamble, a search result); as the
    first prompt they mean one thing.
    """
    for line in head.split("\n"):
        if '"type":"assistant"' in line:
            return False
        if '"type":"user"' in line and ORCA_WORKER_PROMPT.search(line):
            return True
    return False


# ── who may read, and who may write ───────────────────────────────────
#
# On loopback there is no token: the board trusts "this machine". But a
# request from this machine is not the same thing as the owner. Any web page
# open in a browser here can ask the browser to talk to 127.0.0.1, and two
# things stood open until 13 Nov 2026:
#
#   * Writes. A page could send `{"q": …, "answer": …}` to /answer as
#     text/plain, a "simple" request the browser sends without asking the
#     server first, and the board recorded it as the owner's ruling. (Review:
#     a POST carrying `Origin: https://example.invalid` got 201.)
#   * Reads. A page can point a name it controls at 127.0.0.1; the browser
#     then treats the board as that page's own site and hands it everything
#     it asks for. (Re-check: `GET /report.json` with `Host: evil.example`
#     returned 200: every decision and answer, repo paths, commit subjects,
#     task titles, and the shots.)
#
# One check covers both: a request from this machine must be addressed to it
# by one of its own names (`host_refusal`). Writes carry three more
# (`write_refusal`). The page's own requests pass all of them unchanged; a plain
# client such as curl passes them too and is then stopped at the next gate, the
# token (`key_ok`): it has none, and no token is ever handed to a program.
LOOPBACK_CLIENTS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
# The names under which this machine reaches itself. A board opened in a
# browser on this machine is addressed by one of these and by nothing else.
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
# For an owner who reads the board under another name (a name in /etc/hosts, a
# tunnel, a reverse proxy): STATUS_BOARD_HOSTS=name1,name2 in the server's
# environment, read once at start-up. Explicit on purpose. A foreign name is
# exactly what an attacker's request looks like, so the board does not guess
# which ones are the owner's, not even this machine's own hostname.
HOSTS_ENV = "STATUS_BOARD_HOSTS"
# A bare host and an optional port, as a browser writes the Host header. A
# user part (`evil.example@127.0.0.1`) or anything else is not a host.
HOST_HEADER = re.compile(r"^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?$")


def _hostname(host_header: str) -> str:
    """`127.0.0.1:8839` -> 127.0.0.1, `[::1]:8839` -> ::1, not a host -> ""."""
    match = HOST_HEADER.match(host_header.strip().lower())
    return match.group(1).strip("[]") if match else ""


def allowed_hosts(env=os.environ) -> frozenset[str]:
    """The loopback names, plus whatever STATUS_BOARD_HOSTS adds."""
    listed = re.split(r"[,\s]+", (env.get(HOSTS_ENV) or "").strip())
    return frozenset({*LOOPBACK_NAMES, *filter(None, map(_hostname, listed))})


def host_refusal(headers, from_loopback: bool, names: frozenset[str]) -> tuple[int, str] | None:
    """Why a request from this machine is not served, as (status, reason), or
    None when it is.

    The Host header is what the browser was told to connect to. Under a name
    that is not this machine's, the request comes from a page that pointed
    its own name here, and whatever is answered goes to that page. Requests
    from other machines are not judged here: they have had to carry the token.
    """
    if not from_loopback:
        return None
    asked = (headers.get("Host") or "").strip().lower()
    if _hostname(asked) in names:
        return None
    shown = asked if HOST_HEADER.match(asked) else "a name that is not a host"
    return 403, (
        "This board is served only under this machine's own names "
        f"(127.0.0.1, localhost). It was asked for as {shown}. To read it "
        f"under another name, start it with {HOSTS_ENV}=<that name>."
    )


def _origin_of(url: str) -> str:
    """scheme://host[:port] of a URL, lower-cased; "" when it has none."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def write_refusal(headers, from_loopback: bool, names: frozenset[str]) -> tuple[int, str] | None:
    """Why a POST may not write, as (status, reason), or None when it may.

    1. The body must be declared `application/json`. A page on another site
       cannot send that without the browser first asking this server, which
       does not answer such a question; the request is then never sent.
    2. A browser that says where the request comes from (`Sec-Fetch-Site`)
       must say `same-origin`.
    3. `Origin` must be there and be this board's own origin: the scheme and
       the `Host` the request itself was addressed to. Until 2.4.0 a request
       without one was taken (and a `Referer` stood in for it), which let any
       program that could reach the port write; a browser sends `Origin` on
       every POST (the Fetch standard), so the board's own page, and the
       phone's, still pass. No `Referer` fallback any more.
    4. The check every request gets, `host_refusal`: without it a page can
       point a name of its own at 127.0.0.1 and then pass check 3, because it
       is asking "itself".

    The caller's key is checked before this (`Handler.key_ok`): a write needs
    the board key as well as these.
    """
    kind = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if kind != "application/json":
        return 415, "The board records only requests sent as application/json."
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site != "same-origin":
        return 403, "The board records only requests made by its own page."
    host = (headers.get("Host") or "").strip().lower()
    own = f"http://{host}"
    origin = headers.get("Origin")
    if origin is None or _origin_of(origin) != own:
        return 403, "The board records only requests made by its own page."
    return host_refusal(headers, from_loopback, names)


def make_handler(board: Board, token: str | None = None, board_key: bytes | None = None, port: int = 0, run: str | None = None, reread_key=None):
    """The handler of one board's server.

    `token` is the phone's LAN token: set when the board is bound beyond loopback. `board_key` is the secret of the owner's account
    (the raw 32 bytes of `servers/board.key`), or None when it could not be made or read: then nothing is read and nothing is written
    through this server, because failing open would restore exactly what the key closes. `port` is this board's, which every token and
    every signed shot address is bound to: one made for another board is worth nothing here. `run` is the id of this start (made here
    when not given: one per call, as `main` calls this once per process): every token is bound to it, so a token dies with the process
    that issued it. `reread_key` is a function that reads the key file again: it is asked (at most once a second) only while this
    server has no key, so that a key the owner closes (a file readable by others made 0600) heals a running board with no restart;
    a server that has a key keeps it, whatever the file becomes."""
    run = run or make_run()
    last_reread = [0.0]

    def refresh_key() -> None:
        nonlocal board_key
        if board_key is not None or reread_key is None or time.time() - last_reread[0] < 1.0:
            return
        last_reread[0] = time.time()
        try:
            board_key = reread_key() or None
        except Exception:  # noqa: BLE001 - a key that cannot be read is no key: the board stays closed
            board_key = None

    names = allowed_hosts()
    # The page's one inline script, hashed once at start: the policy lets that script run and no other (so an injected one does not).
    try:
        page_script = PAGE_SCRIPT.search((HERE / "index.html").read_text(encoding="utf-8")).group(1)
        page_policy = policy_for(page_script)
    except (OSError, AttributeError, UnicodeDecodeError):
        page_policy = None  # no page to protect: only the frame rule goes out

    class Handler(SimpleHTTPRequestHandler):
        RUN = run  # tests read it; it is not a secret

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def handle_one_request(self):
            refresh_key()
            super().handle_one_request()

        def header_token(self) -> str:
            return (self.headers.get(TOKEN_HEADER) or "").strip()

        @staticmethod
        def same_secret(supplied: str, expected: str | None) -> bool:
            # compare_digest on bytes: the timing leak is not a realistic threat here, but writing `==` where a secret is compared is
            # the kind of thing that gets copied into somewhere it matters.
            return bool(expected) and bool(supplied) and hmac.compare_digest(supplied.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))

        def key_ok(self) -> bool:
            """May this caller read the board's report or write to it?

            From this machine: only with a valid token for THIS board in the `X-Board-Token` header, which the board's own page sends
            and which the opener makes from the board key (only the owner's account can read that: a program of another account, or
            one kept away from the owner's files, cannot). Cookies are ignored and nothing reads a token from an address. From another
            machine: the phone's LAN token, in the same header, and nothing else."""
            self._token_age = None
            if self.from_this_machine():
                self._token_age = token_age(self.header_token(), board_key, port, run)
                return self._token_age is not None
            return bool(token) and self.same_secret(self.header_token(), token)

        def refuse_site(self) -> bool:
            """True, with the refusal sent, when the browser says this read comes from another site or another port.

            A browser tells where a request comes from (`Sec-Fetch-Site`): `same-origin` for the board's own page, `none` for an
            address typed or a bookmark, and `same-site` or `cross-site` for a request made by a page of somewhere else. A page
            served from another PORT of 127.0.0.1 (a dev server open in the same browser) is the same SITE as the board. The report
            and the shots are therefore given only to the board's own page or an address typed in the browser; a request with no such
            header (a program, an older browser) is judged by what it carries. Nothing of the content is sent."""
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if not site or site in ("same-origin", "none"):
                return False
            self.refuse(403, SITE_SENTENCE, "site", as_text=not self.path.split("?")[0].rstrip("/") in ("/report.json", "/report"))
            return True

        def refuse_key(self, as_text: bool = False) -> None:
            if not self.from_this_machine():
                return self.refuse(403, LAN_DENY_SENTENCE, "key", as_text)
            return self.refuse(401, KEY_SENTENCE if board_key else NO_KEY_SENTENCE, "key", as_text)

        def refuse(self, status: int, sentence: str, code: str, as_text: bool = False) -> None:
            if as_text:
                body = sentence.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return None
            body = json.dumps({"ok": False, "code": code, "error": sentence}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.path.startswith("/report"):
                self.send_header("Cache-Control", "no-store")  # as the report itself (end_headers adds its own for every other path)
            self.end_headers()
            self.wfile.write(body)
            return None

        # No board is ever shown inside a frame of another page.
        #
        # Until 14 Nov 2026 no response said so, and a board has one control
        # that is expensive to press: the pill that records an answer. A page
        # the owner visited could hold a board in an iframe at opacity 0.05,
        # place the pill under the pointer, and take one click as the owner's
        # ruling. The POST then comes from the board's own page, so the checks
        # on writes (write_refusal) all pass: they cannot tell a frame from a
        # tab. (Reviewer, 14 Nov 2026: a real click on the top page recorded
        # the recommended answer; answers.json grew by one. Every release up to
        # 2.0.1 had it.) The board is never meant to be framed, not even by
        # itself, so every response forbids it, in both forms: X-Frame-Options
        # for browsers that predate CSP, `frame-ancestors` for the rest.
        #
        # One place, end_headers, because every response ends there: the page,
        # the report, the shots, the redirects and listings of the static
        # handler, and send_error's refusals alike. A response that names a
        # policy of its own (a shot: SHOT_POLICY) must not get a second
        # Content-Security-Policy header, which a browser enforces as another
        # policy beside the first rather than as part of it; send_header holds
        # such a policy back and end_headers sends one header that has both.
        # Nothing is added for scripts or styles: that is a change of its own.
        FRAME_POLICY = "frame-ancestors 'none'"

        def send_header(self, keyword: str, value: str) -> None:
            if keyword.lower() == "content-security-policy":
                self._held_policies = [*getattr(self, "_held_policies", []), value]
                return None
            return super().send_header(keyword, value)

        def end_headers(self):  # noqa: N802 - stdlib naming
            # The page never re-fetches itself: the 3s poll refreshes report.json
            # only. So without this, an open tab keeps running whatever HTML it
            # loaded, and a fix to the page cannot reach it — the browser has a
            # Last-Modified and no Cache-Control, so it caches heuristically and
            # never revalidates. That shipped a confirmation dialog to a tab that
            # went on silently recording unconfirmed answers with the old code
            # (1 Aug 2026). The board is localhost and tiny; caching it buys
            # nothing and costs correctness.
            if not self.path.startswith("/report"):
                self.send_header("Cache-Control", "no-store, must-revalidate")
            # No Set-Cookie, ever (2.4.0): a cookie goes to every port of the host. The credential is a token that the board's own page
            # keeps in its own storage and sends as a header (`TOKEN_HEADER`). And nothing here is ever sent on as a Referer.
            super().send_header("Referrer-Policy", "no-referrer")
            held = ", ".join(getattr(self, "_held_policies", []))
            self._held_policies = []
            super().send_header("X-Frame-Options", "DENY")
            super().send_header(
                "Content-Security-Policy",
                f"{held}; {self.FRAME_POLICY}" if held else self.FRAME_POLICY,
            )
            super().end_headers()

        def from_this_machine(self) -> bool:
            return bool(self.client_address) and self.client_address[0] in LOOPBACK_CLIENTS

        def refused_by_name(self) -> bool:
            """True, with the refusal already sent, when a request from this
            machine is addressed to a name that is not this machine's."""
            refusal = host_refusal(self.headers, self.from_this_machine(), names)
            if refusal is None:
                return False
            body = refusal[1].encode()
            self.send_response(refusal[0])
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True

        # The page, and nothing else of this folder: the static handler used to serve every file under status-board/ (the plugin's
        # own code: serve.py, the hooks' helpers), which is no one's business but is now no one's to read either. The page loads
        # only `report.json` and `shot/<name>` (see index.html), both routed below.
        PAGE_ROUTES = ("", "/index.html")

        def do_HEAD(self):  # noqa: N802 - stdlib naming
            if self.refused_by_name():
                return None
            route = self.path.split("?")[0].rstrip("/")
            if route in self.PAGE_ROUTES:
                if page_policy:
                    self.send_header("Content-Security-Policy", page_policy)  # held, and sent with the response's own headers
                return super().do_HEAD()
            return self.send_error(404, "File not found")

        def do_GET(self):  # noqa: N802 - stdlib naming
            # Every read, not only report.json: the page, the report and the
            # shots all answer to the same names (see host_refusal).
            if self.refused_by_name():
                return None
            route = self.path.split("?")[0].rstrip("/")
            if route in ("/report.json", "/report"):
                if self.refuse_site():
                    return None
                if not self.key_ok():
                    return self.refuse_key()
                return self.send_report()
            if route.startswith("/shot/"):
                if self.refuse_site():
                    return None
                return self.send_shot(route[len("/shot/") :])
            if route == "/_proof":
                return self.send_proof()
            if route in self.PAGE_ROUTES:
                return self.send_page()
            return self.send_error(404, "File not found")

        PROOF_CHALLENGE = re.compile(r"[0-9a-f]{32}")

        def send_proof(self) -> None:
            """`GET /_proof?c=<32 lowercase hex>`: the board proves it holds the key. The opener asks before it writes a
            form that carries a token, so that it never hands a token to a program that merely holds this board's port (a board that is gone,
            its pid alive, another program on the port: the token would work on the real board for 30 days when it returned).

            The answer is `{"run": <this process's run>, "proof": <HMAC-SHA256(key, "proof:v2:<port>:<run>:<c>")>}`, the proof 64 lowercase hex
            characters; the opener makes its token for that run, and no other (a token dies with the run). It needs no token and gives
            nothing away (the run is not a secret): the message has a prefix of its own (`proof:`, where a token's is `browser:` and a shot's `shot:`), `c` is hex and
            holds no colon, so the proof of one `c` is never a token or a shot signature; and a listener without the key cannot make it.
            Only for this machine (another machine: 404), under the Host rule applied before. A page of another origin that asks cannot
            read the answer (no CORS header is ever sent), and if it could, it would learn the HMAC of a challenge it chose, which is
            worth nothing. A board with no key it trusts answers 503."""
            if not self.from_this_machine():
                return self.send_error(404, "File not found")
            if not board_key:
                return self.refuse(503, NO_KEY_SENTENCE, "key")
            given = parse_qs(urlsplit(self.path).query).get("c", [])
            if len(given) != 1 or not self.PROOF_CHALLENGE.fullmatch(given[0]):
                return self.send_json(400, {"ok": False, "error": "The challenge is 32 lowercase hexadecimal characters."})
            return self.send_json(200, {"run": run, "proof": _digest(board_key, f"proof:{PROOF_VERSION}:{port}:{run}:{given[0]}")})

        def send_page(self):
            """The page itself holds no data, so it is served to anyone who can reach the port: revive-board.py, bookmarks, the front
            door's redirect and the short address in the row land on it, and a phone's navigation (which cannot carry a header)
            does too. It runs under the hash policy of `policy_for`. The one exception is the phone's printed address
            `/?k=<the LAN token>`: from another machine, with the right token, the answer is the storing page, which keeps the token
            in that origin's storage and replaces itself with `/`. On this machine `?k=` means nothing."""
            if not self.from_this_machine() and token:
                try:
                    given = parse_qs(urlsplit(self.path).query).get("k", [])
                except ValueError:
                    given = []
                if len(given) == 1 and self.same_secret(given[0], token):
                    return self.send_storing_page(token)
            if page_policy:
                self.send_header("Content-Security-Policy", page_policy)
            return super().do_GET()

        def send_storing_page(self, stored: str) -> None:
            """A page of the board's own origin whose one script keeps `stored` as this origin's token and replaces itself with `/`.
            It is for the opener's fresh token (`POST /_open`) and for the phone's LAN token (`GET /?k=`). The token is in this
            response's body, never in an address (the history keeps `/_open`, which holds nothing, and then `/`): `no-store`, no
            Referer, a policy with the hash of this page's own script."""
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,%d}" % MAX_TOKEN_CHARS, stored):
                return self.refuse(500, WRITE_FAILED_SENTENCE, "error", as_text=True)
            script = f"try{{{TOKEN_STORE_KIND}.setItem({json.dumps(TOKEN_STORAGE)},{json.dumps(stored)})}}catch(e){{}}location.replace('/')"
            body = (
                '<!doctype html><meta charset="utf-8"><title>status board</title>'
                f"<p>Opening the board.</p><script>{script}</script>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", policy_for(script))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - stdlib naming
            """The only write path in the server, and it touches one file.

            Until 2.4.0 loopback had no auth ("fine for a single local reader"),
            and any program that could reach the port recorded an answer, even
            to a question the board never asked, which a session was then told
            was the owner's ruling. A write now needs, in this order: the token
            (`key_ok`: this board's token, in the `X-Board-Token` header, from this
            machine; the phone's LAN token, from another), which proves the
            owner's account; the board's own page (`write_refusal`: `Origin`,
            `Sec-Fetch-Site`, `Host`); and then the write itself, which takes
            only an answer to a question the board asks (`record_answer`) or a
            backlog item the page can make. The body is capped and the payload
            is whitelisted field by field rather than merged wholesale. A write
            path left open because only the read path was guarded is the classic
            version of this mistake.
            """
            route = self.path.split("?")[0].rstrip("/")
            if route == "/_open":
                return self.open_board()  # the one write that needs no token: it is how the browser gets one
            if not self.key_ok():
                return self.refuse_key()
            if route not in ("/backlog", "/answer"):
                return self.send_error(404)
            refusal = write_refusal(self.headers, self.from_this_machine(), names)
            if refusal is not None:
                return self.send_json(refusal[0], {"ok": False, "error": refusal[1]})
            if not self.from_this_machine():
                source = "network"  # judged by the phone's token: it is a proof that someone had the token, and nothing more
            else:
                source = "browser" if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "same-origin" else "unmarked"
            try:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    length = 0
                # board.MAX_BODY_BYTES, not a literal here: this gate exists
                # to stop something pathological, not to be the board's real
                # content limit — that limit is board.ANSWER_MAX_CHARS, and
                # this number is derived from it precisely so it cannot
                # become the binding one again by accident.
                if length <= 0 or length > board.MAX_BODY_BYTES:
                    raise BoardError(
                        f"Body must be between 1 byte and {board.MAX_BODY_BYTES} bytes."
                    )
                try:
                    payload = json.loads(self.rfile.read(length))
                except ValueError as exc:  # not JSON, or not UTF-8: the decoder's own text is not sent on
                    raise BoardError(BAD_JSON_SENTENCE) from exc
                if not isinstance(payload, dict):
                    raise BoardError("Expected a JSON object.")
                if route == "/answer":
                    item = board.record_answer(payload.get("q"), payload.get("answer"), source)
                elif payload.get("id"):
                    item = board.set_state(payload["id"], payload.get("state", "done"))
                    if item is None:
                        raise BoardError("No backlog item with that id.")
                else:
                    item = board.add_item(payload)
                code, answer = 201, {"ok": True, "item": item}
            except (QuestionGone, OptionsOnly) as exc:
                code, answer = 409, {"ok": False, "error": str(exc)}
            except BoardError as exc:  # a sentence this server wrote itself
                code, answer = 400, {"ok": False, "error": str(exc)}
            except Exception as exc:  # anything else: the detail to the log, a fixed sentence to the caller
                board.log_detail(f"POST {route}", exc)
                code, answer = 400, {"ok": False, "error": WRITE_FAILED_SENTENCE}
            self.send_json(code, answer)

        OPEN_MAX_BODY = 4096
        OPEN_FILE = re.compile(r"[0-9a-f]{16}")

        def refuse_open(self, code: int, sentence: str) -> None:
            body = sentence.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def open_board(self) -> None:
            """`POST /_open`: the way a browser is let in (2.4.0), and the only way. `open_board.py` makes a token for this board from
            the key (the token, never the key) and a form that posts it here from a local file, as the fields `t` and `n`.

            Only from this machine (another machine does not get it: 404), under the Host rule, as `application/x-www-form-urlencoded`
            (else 415), with `Origin: null` (a local file) or this board's own (any other: 403). Then:
            `t` must be a valid token for this port and this run that was issued at most 120 seconds ago (checked FIRST: it needs the key,
            and the nonce is readable on a command line), and then the opener's file `servers/open-<n>.html` must exist, be the owner's, be
            0600 and be at most 60 seconds old, and it is removed, so that an opening works once.
            A refusal there answers 401 with OPEN_REFUSED_SENTENCE and sets nothing. Otherwise the answer is the
            storing page (`send_storing_page`): `no-store`, its own script hash, and nothing in an address."""
            if not self.from_this_machine():
                return self.send_error(404)
            refusal = host_refusal(self.headers, True, names)
            if refusal is not None:
                return self.refuse_open(refusal[0], refusal[1])
            kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if kind != "application/x-www-form-urlencoded":
                return self.refuse_open(415, "This takes only the opener's form.")
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in ("null", f"http://{(self.headers.get('Host') or '').strip().lower()}"):
                return self.refuse_open(403, "This takes only the opener's form, from a local file or from this board's own page.")
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length < 0 or length > self.OPEN_MAX_BODY:
                return self.refuse_open(400, "This takes only the opener's form.")
            try:
                fields = parse_qs(self.rfile.read(length).decode("ascii"), keep_blank_values=True, max_num_fields=8)
            except (ValueError, UnicodeDecodeError):
                return self.refuse_open(400, "This takes only the opener's form.")
            if not board_key:
                return self.refuse_open(401, NO_KEY_SENTENCE)
            nonces, tokens = fields.get("n", []), fields.get("t", [])
            if len(nonces) != 1 or len(tokens) != 1 or not self.OPEN_FILE.fullmatch(nonces[0]):
                return self.refuse_open(401, OPEN_REFUSED_SENTENCE)
            # The token first: it needs the key, and the nonce is on command lines anyone who can run `ps` reads, so a post with that
            # nonce and a worthless token must not use up the owner's opening.
            age = token_age(tokens[0], board_key, port, run)
            if age is None or age > OPEN_TOKEN_MAX_AGE_S:
                return self.refuse_open(401, OPEN_REFUSED_SENTENCE)
            if not self.take_opener_file(STATE / "servers" / f"open-{nonces[0]}.html"):
                return self.refuse_open(401, OPEN_REFUSED_SENTENCE)
            return self.send_storing_page(tokens[0])

        @staticmethod
        def take_opener_file(path: Path) -> bool:
            """True when `path` is the opener's file as the opener leaves it (a regular file, the owner's, 0600, at most 60 seconds
            old by its modification time, with 60 seconds allowed for a clock a little ahead), and it has been removed by this
            call: of two requests with the same nonce, one gets it and the other finds it gone."""
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    return False
                age = time.time() - info.st_mtime
                if age > OPEN_FILE_MAX_AGE_S or age < -TOKEN_SKEW_S:
                    return False
                os.unlink(path)
            except OSError:
                return False
            return True

        def send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Images the board owns, and the only ones it will ever serve.
        #
        # A finding that is fundamentally visual cannot survive as prose. The
        # board learned this the expensive way: a wrapping-text defect was
        # described in three careful paragraphs and read as minor, then a
        # single screenshot made it obvious in two seconds. Before this
        # endpoint the best the board could do was print a filesystem path and
        # ask the reader to go and open it — which is the "fall back to prose"
        # failure its own rules warn about, wearing a filename.
        #
        # Deliberately NOT a general file server. Two reasons, both learned
        # from how this would go wrong:
        #
        #   * Path traversal. Serving arbitrary paths from a query string,
        #     even on loopback, is the classic hole. Here there is no path to
        #     traverse: the name is a single filename, matched against a strict
        #     pattern, resolved inside one directory the board owns, and the
        #     resolved result is re-checked to be inside it — belt and braces,
        #     because a symlink placed in shots/ would otherwise escape.
        #   * Lifetime. Shots taken in a session scratchpad vanish when the
        #     session does, so a board referencing them would rot into broken
        #     images. Living beside narrative.json, they last exactly as long
        #     as the words that cite them.
        SHOT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
        # A shot is a picture, and it is served as nothing more. An SVG is
        # also a document: opened in a tab of its own, which is what a click
        # on any shot does, it ran its scripts as the board's own page. A
        # hostile SVG copied into shots/ could then read the board and record
        # an answer that passed every check on writes, because it was the
        # same origin (re-check of 13 Nov 2026: `SVG-RECHECK injected` landed
        # in answers.json). `sandbox` gives such a document an origin of its
        # own and runs no script in it; the rest lets through only what a
        # still picture needs: its own styles, and images and fonts embedded
        # in it. Sent with every shot, not only .svg: one rule, no list of
        # types to keep right.
        SHOT_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
        SHOT_TYPES = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }

        def shot_links(self) -> dict[str, str]:
            """The pictures of `shots/` as signed addresses, `shot/<name>?s=<expires>.<digest>`, valid ten minutes: an `<img>` and a
            link opened in a tab cannot send a header, so the address is what proves the page asked for it."""
            links: dict[str, str] = {}
            if not board_key:
                return links
            try:
                with os.scandir(board.dir / "shots") as entries:
                    for entry in sorted(entries, key=lambda e: e.name)[:500]:
                        name = entry.name
                        if (self.SHOT_NAME.match(name) and not name.startswith(".") and Path(name).suffix.lower() in self.SHOT_TYPES
                                and entry.is_file()):
                            links[name] = f"shot/{name}?s={sign_shot(board_key, port, name)}"
            except OSError:
                pass
            return links

        def send_shot(self, name: str):
            """A shot is given for a signed address only (`?s=<expires>.<digest>`, bound to this port and this name, valid ten minutes),
            from this machine and from the network alike; nothing else opens one, not even a good token."""
            name = unquote(name)
            root = (board.dir / "shots").resolve()
            suffix = Path(name).suffix.lower()
            if (
                not self.SHOT_NAME.match(name)
                or name.startswith(".")
                or suffix not in self.SHOT_TYPES
            ):
                return self.send_error(404, "no such shot")
            given = parse_qs(urlsplit(self.path).query).get("s", [])
            if len(given) != 1 or not shot_signature_ok(given[0], board_key, port, name):
                return self.refuse(401, SHOT_REFUSED_SENTENCE, "key", as_text=True)
            try:
                path = (root / name).resolve()
                # Re-check after resolution: the pattern above already forbids
                # slashes and dots-only names, but a symlink inside shots/ is
                # a path the pattern cannot see.
                path.relative_to(root)
                body = path.read_bytes()
            except (OSError, ValueError):
                return self.send_error(404, "no such shot")
            self.send_response(200)
            self.send_header("Content-Type", self.SHOT_TYPES[suffix])
            self.send_header("Content-Length", str(len(body)))
            # Images are immutable once written; a changed shot gets a new
            # name, so the narrative citing it changes in the same edit.
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", self.SHOT_POLICY)
            self.end_headers()
            self.wfile.write(body)

        def send_report(self):
            try:
                report = board.report()
                report["shotLinks"] = self.shot_links()  # the page draws its pictures and their links from these
                body = json.dumps(report, ensure_ascii=False).encode()
                code = 200
                # The questions the page is about to show: an answer to one of them is taken for 30 minutes more, even if the
                # agent withdraws it meanwhile (`Board.asked`).
                shown = report.get("decisions")
                board.note_shown([d for d in shown if isinstance(d, dict)] if isinstance(shown, list) else [])
            except Exception as exc:  # say that something broke; never go blank. What, goes to the log: a path can be in it
                board.log_detail("GET /report.json", exc)
                body = json.dumps(
                    {
                        "project": board.name,
                        "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
                        "headline": "The board cannot read its own report",
                        "error": REPORT_FAILED_SENTENCE,
                        "state": {},
                    }
                ).encode()
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # A valid token a day old or more is replaced by a fresh one, which the page stores: that makes "30 days from the last
            # visit". Only for this board's own kind of token, from this machine (the phone's LAN token is not renewed).
            if code == 200 and board_key and getattr(self, "_token_age", None) is not None and self._token_age >= TOKEN_RENEW_S:
                self.send_header(TOKEN_HEADER, make_token(board_key, port, run))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # a chatty board is noise in the terminal it was started from

    return Handler


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class HomeNotPrivate(OSError):
    """The status-board home, or something in it, is not this account's or cannot be made private; the text names where."""


def make_home_private(state: Path, max_entries: int = 50_000, notes: list | None = None) -> None:
    """Force the status-board home to this account: `STATE`, `boards/`, `servers/` and every board's folder (and the folders in
    them) to 0700, and every file in them to the owner's bits only (0600 for the data files). Raises HomeNotPrivate, naming the
    first thing that is another account's or cannot be changed, and changes nothing outside the home. One exception: a FILE of this
    account that cannot be changed (a file with the user-immutable flag, say) inside a folder that has been closed is out of other
    accounts' reach already, so it does not stop the board: its path (relative to the home) is appended to `notes` and the walk goes on.
    A folder that cannot be closed, and anything that belongs to another account, still raise.

    Until 2.4.0 these were made with the umask's modes (boards/<slug>/ 0755, serve.log 0644, a narrative 0644): private only
    because `~/.claude` above them is 0700, which is the one thing nobody here checks. The board key and the phone's token are made private on their own; this is for everything else a board holds, which
    a program of another account could otherwise read where the home is not under a private folder. Symbolic links are not
    followed below the home (their target is not the home's), and a link at the top of it (a home kept elsewhere) is followed once."""
    uid = os.getuid()
    seen = 0

    def force(path: Path, folder: bool) -> None:
        try:
            info = os.stat(path) if path == state else os.lstat(path)
        except FileNotFoundError:
            return  # gone since it was listed: another board of this home removed its temporary file
        if stat.S_ISLNK(info.st_mode):
            return  # a link inside the home: its target is not ours to change
        if info.st_uid != uid:
            raise HomeNotPrivate(f"{path} belongs to another account")
        wanted = 0o700 if folder else stat.S_IMODE(info.st_mode) & 0o700
        if stat.S_IMODE(info.st_mode) != wanted:
            try:
                os.chmod(path, wanted)
            except FileNotFoundError:
                return
            except OSError as exc:
                if not folder and notes is not None:
                    notes.append(str(Path(path).relative_to(state)))
                    return
                raise HomeNotPrivate(f"{path} cannot be made private ({exc.strerror})") from exc

    def walk(folder: Path, depth: int) -> None:
        nonlocal seen
        force(folder, True)
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise HomeNotPrivate(f"{folder} cannot be read ({exc.strerror})") from exc
        for entry in entries:
            seen += 1
            if seen > max_entries:
                return  # a home this large is not one a board made; what was done stays done
            path = Path(entry.path)
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth < 6:
                    walk(path, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                if entry.name.endswith(".key"):
                    continue  # a key readable by others is refused when it is read, never trusted by being closed afterwards
                force(path, False)

    try:
        Path(state).mkdir(mode=0o700, parents=True, exist_ok=True)
        for sub in ("boards", "servers"):
            (Path(state) / sub).mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise HomeNotPrivate(f"{state} cannot be made ({exc.strerror})") from exc
    # The home's own folder, and then only what boards write: `boards/<slug>/` for every board, and `servers/` (the records and the
    # keys). Not the rest of the home: the owner keeps other things there (previews, notes), which are not a board's to change.
    force(Path(state), True)
    walk(Path(state) / "servers", 0)
    boards = Path(state) / "boards"
    force(boards, True)
    try:
        folders = sorted(p for p in boards.iterdir() if not p.is_symlink() and p.is_dir())
    except OSError as exc:
        raise HomeNotPrivate(f"{boards} cannot be read ({exc.strerror})") from exc
    for folder in folders:
        walk(folder, 0)


def lan_token(state: Path) -> str:
    """The phone's token (`STATE/lan-token`): read and kept when there is one (the phone needs nothing new), made otherwise.

    Made closed: written whole to a temporary file created 0600 with O_EXCL and linked into place, so that the token is never
    readable by others, not even for an instant, and two boards started together end with one token. An old file readable by
    others (earlier versions created it with the umask's mode and chmodded after) is closed now."""
    import secrets

    path = Path(state) / "lan-token"
    try:
        existing = path.read_text().strip()
    except (OSError, ValueError):
        existing = ""
    if existing:
        try:
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)
        except OSError:
            pass
        return existing
    Path(state).mkdir(mode=0o700, parents=True, exist_ok=True)
    made = secrets.token_urlsafe(16)
    tmp = Path(state) / f".lan-token.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(made)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        return path.read_text().strip() or made
    except OSError:
        return made


def lan_ip() -> str:
    """Best-effort LAN address, for printing a URL a phone can actually use.

    Opens a UDP socket to a routable address; nothing is sent, but the kernel
    picks the interface it would route through, which is the address other
    devices on the network can reach.
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 1))  # TEST-NET-1: routable, never answers
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "<this-machine-ip>"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Per-project status board server.")
    ap.add_argument("--repo", default=".", help="path to the project repo")
    ap.add_argument("--port", type=int, help="override the derived port")
    ap.add_argument("--name", help="display name (defaults to the directory name)")
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "interface to bind. Defaults to loopback. Use 0.0.0.0 to reach the "
            "board from a phone on the same network — this requires a token, "
            "printed at startup, and the board is still plaintext HTTP."
        ),
    )
    args = ap.parse_args()

    # A door port inside the boards' own range is refused before anything is bound or written (the re-check, finding 4).
    problem = front_port_problem()
    if problem:
        print(problem)
        sys.exit(2)

    # Everything this process makes is the owner's alone (0700 / 0600), and what is already in the home is made so before anything
    # is read or served: see `make_home_private`. A home that cannot be made private is not served, in a fixed sentence.
    os.umask(0o077)
    home_notes: list = []
    try:
        make_home_private(STATE, notes=home_notes)
    except HomeNotPrivate as exc:
        print(HOME_SENTENCE)
        print(f"  {exc}")
        sys.exit(2)

    repo = Path(args.repo).expanduser().resolve()
    board = Board(repo, args.name)
    port = args.port or port_for(repo)
    if home_notes:
        # one line, in the log this process writes to (and in the board's own log): the files stay as they are, inside closed folders
        note = (f"! {len(home_notes)} file(s) of this account in the status-board home cannot be made private ({home_notes[0]}"
                f"{' and others' if len(home_notes) > 1 else ''}): they stay as they are, inside folders closed to other accounts.")
        print(note)
        board.log_note(note[2:])
    # Ask Orca once now, in the background, so its answer is usually in hand
    # before the first page load. Costs nothing when there is no `orca`.
    board.orca.view(board.project_paths())

    # Binding beyond loopback exposes repo paths, branch names, commit subjects
    # and every decision on the board to anything on the network, and the board
    # has no login. A token is not real security — it is plaintext over HTTP —
    # but it stops casual access from every other device on a shared network,
    # and it makes the exposure a deliberate act rather than a side effect of a
    # flag. Loopback stays tokenless: on a single-user machine it would be
    # friction with nothing on the other side of it.
    token = None
    if args.host not in LOOPBACK:
        token = lan_token(STATE)

    # The secret of the owner's account (the raw 32 bytes of servers/board.key): the tokens the opener makes and the signed shot
    # addresses this server makes are HMACs of it, and only a request that carries one reads the report or writes (make_handler).
    # One key per home. It is made, or read and checked (the owner's, 0600, in a 0700 folder), by the front door's functions. When
    # that fails the board says so on its page and refuses reads and writes until it is fixed. The key is never printed or sent.
    board_key = None
    try:
        board_key = front_door.make_key(STATE, name="board.key") or None
    except Exception as exc:  # noqa: BLE001 - say why, once, in the log this process writes to
        print(f"! the board key could not be made or read ({type(exc).__name__}): this board shows nothing and records nothing. Make servers/board.key under the status-board home readable by its owner only, or delete it to get a new one, then start the board again.")
    if board_key is None:
        print("! no usable board key: this board shows nothing and records nothing. Make servers/board.key under the status-board home readable by its owner only, or delete it to get a new one, then start the board again.")

    handler = make_handler(board, token, board_key, port, reread_key=lambda: front_door.read_key(STATE, name="board.key"))
    shown = args.host if args.host not in LOOPBACK else "127.0.0.1"
    # Line-buffer stdout: this process is usually backgrounded into a log, and
    # Python block-buffers to a pipe, so the startup banner — which is the only
    # place the URL and the LAN token appear — would sit unflushed until the
    # buffer filled. A token you cannot read is a token you cannot use.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # Bound before anything is said or written about it: a board whose port is
    # taken used to print its banner and then fail, and with the record below it
    # would also have claimed an address it never held.
    httpd = ThreadingHTTPServer((args.host, port), handler)

    # The front door (front_door.py). This server says where it is (a record the
    # door reads on every request) and, if it takes part, tries to hold the
    # door's port for as long as it lives. 0.5 s for the first try, so that the
    # banner can say whether the address by name works without guessing.
    door = front_door.FrontDoor(sys.modules[__name__])
    door.write_record(board, port, args.host)
    door_state = None
    if front_door.election_enabled(STATE):
        holding = threading.Event()
        threading.Thread(target=door.keep_trying, args=(threading.Event(), holding), daemon=True).start()
        holding.wait(0.5)
        door_state = "door" if holding.is_set() else front_door.probe_state(state=STATE)
    name_lines = front_door.banner_lines(board.name, board.slug, port, door_state, shown) if door_state else None
    if door_state == "door" and door.v6_state == "other":
        # Another program holds [::1] of the door's port: a browser given `localhost` may try that address first and reach that program,
        # which can show a false list and send the owner to a false board (it gets no token). Say so once, here and in the board's log,
        # and give the short address in its 127.0.0.1 form while that lasts.
        warning = (f"! another program holds [::1]:{door.port}, so the address by name may not reach this board's door: "
                   f"use http://127.0.0.1:{door.port}")
        print(warning)
        board.log_note(warning[2:])
        name_lines = [row.replace(f"http://localhost:{door.port}", f"http://127.0.0.1:{door.port}") for row in name_lines] if name_lines else name_lines
    if token:
        ip = lan_ip() if args.host == "0.0.0.0" else args.host
        print(f"{board.name} board → http://{ip}:{port}/?k={token}")
        print("  ! bound beyond loopback. Anything on this network can reach it.")
        print("  ! plaintext HTTP, and the token is in the URL. Do not use on")
        print("    a network you do not control. Each browser tab keeps the token for itself:")
        print("    open this address once for each tab. An answer it records is marked as coming")
        print("    from the network: the token proves that someone held it, not that it was you.")
        print(f"  token stored at: {STATE / 'lan-token'}")
        if name_lines:  # on this machine only: the front door is loopback
            print("  on this machine: " + name_lines[0].split(" → ", 1)[1] + "  " + name_lines[1].strip())
    elif name_lines:
        print("\n".join(name_lines))
    else:
        print(f"{board.name} board → http://{shown}:{port}")
    print(f"  narrative: {board.narrative}", flush=True)
    httpd.serve_forever()
