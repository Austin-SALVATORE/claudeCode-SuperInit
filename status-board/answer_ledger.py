#!/usr/bin/env python3
"""The one place that decides which of the owner's board answers are fresh.

Two hooks carry a tap to the agent: `hooks/answer-waiter.py` (fast: it waits in
the background and wakes the session) and `hooks/board-answers.py` (slow: it
runs when the owner sends a message). Each used to keep its own idea of what
was already told, and an answer both carried reached the agent twice. Now both
call `claim()` below, which reads, compares and writes one record, under one
lock: an answer is told once, whichever path got to it first.

THE RECORD  (boards/<slug>/.surfaced-to-agent.json, as it always was)
  {question: at}   the `at` that was last told to a session keeping this board.
An answer is fresh when its `at` differs from the record's, so answering the
same question again is fresh again, and a record that has not got the question
yet makes it fresh. Written through a `.tmp` and `os.replace`, and only while
holding `.surfaced.lock`.

NEVER EMPTY, NEVER REPLAYED
  * `load_answers` gives None for an answers.json that cannot be read, and the
    caller skips the round. It is never read as "no answers": nothing is lost
    by that, because the record is never pruned, but a half-read file must not
    pass for a finished one.
  * A record that is missing or unreadable is seeded, not trusted: every answer
    recorded before this session began is written as already told. Without
    that, a deleted or torn record replayed the owner's whole history to the
    agent as fresh rulings, which is what the old hook did (14 Nov 2026). Those
    answers are covered by the skill's "read the file before you ask".

NEVER MARKED AND LOST  (spikes S3 and S5, 14 Nov 2026)
  `/restart` re-execs Claude Code in the same process. A hook that was started
  before it keeps running, but the image that would have read its output is
  gone: its exit 2 goes to nobody. Such a waiter must claim nothing, and if it
  claims in the instant before it finds out, the marker is put back when the
  message cannot be written. `claim()` takes `still_mine`, asked again inside
  the lock, and `deliver`, called inside the lock after the marker is written.

Standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()
MARKER = ".surfaced-to-agent.json"
LOCK = ".surfaced.lock"
LOCK_WAIT_S = 5.0  # the lock is held for milliseconds; a holder that hangs must not hang everyone
ANCHOR_LEVELS = 6  # how far up the process tree to look for `claude`
PS_TIMEOUT_S = 2.0

Fresh = list  # [(question, entry)], oldest first


def slug_for(repo: Path) -> str:
    # Must match serve.py's slug_for exactly, or this reads a board that does
    # not exist and silently reports nothing forever.
    return repo.name.lower().replace(" ", "-")


def board_dir(cwd: Path) -> Path:
    return STATE / "boards" / slug_for(cwd)


def _git_out(path: Path, *args: str) -> Optional[str]:
    try:
        done = subprocess.run(["git", "-C", str(path), "rev-parse", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def _project_root(cwd: Path) -> Path:
    top = _git_out(cwd, "--show-toplevel")
    return Path(top).resolve() if top else cwd.resolve()


def _common_dir(path: Path) -> Optional[Path]:
    common = _git_out(path, "--path-format=absolute", "--git-common-dir")
    return Path(common).resolve() if common else None


def board_ok(board: Path, cwd: Path) -> bool:
    """Does the board in this folder belong to the project that `cwd` is in?

    The board is found by the project folder's name (serve.py's slug), so two
    projects whose folders have the same name share one board folder: an old
    defect. Until 14 Nov 2026 it was harmless, because the only reader was the
    prompt hook, which runs when a prompt is submitted. The waiter runs in every
    session all the time, and a session of the other project would have been
    told this board's answers as its own owner's rulings (the review, finding
    1). `serve.py` therefore writes `repo.json` once, naming the project that
    started the board (F1b), and every hook is silent in any other project.

    No `repo.json` yet (a board started before this): today's behaviour. A
    linked worktree of the recorded project is the same project (the same git
    common dir), and a project whose folder is the recorded path is it.

    A recorded path that no longer exists is nobody's (the review of 14 Nov
    2026, finding 1): the project was moved, renamed under the same name or
    restored elsewhere, and refusing it meant that it was told nothing, ever,
    with nothing to say so. The project that asks is accepted, and `board_for`
    makes the record say so."""
    recorded = _recorded_repo(board)
    if recorded is None:
        return True
    if not Path(recorded).exists():
        return True
    try:
        here, there = cwd.resolve(), Path(recorded).resolve()
    except OSError:
        return True
    if here == there:
        return True
    root = _project_root(cwd)
    if root == there:
        return True
    mine, theirs = _common_dir(root), _common_dir(there)
    return mine is not None and mine == theirs


def _recorded_repo(board: Path) -> Optional[str]:
    """The project folder repo.json names, or None when there is no usable record."""
    try:
        recorded = json.loads((board / "repo.json").read_text(encoding="utf-8")).get("repo")
    except (OSError, ValueError, AttributeError):
        return None
    return recorded if isinstance(recorded, str) and recorded else None


def adopt_board(board: Path, cwd: Path) -> bool:
    """repo.json names a folder that is gone: write this project's root in its place. Decided again inside the lock,
    so that two projects that ask at once end with one owner. serve.py does the same when it starts (and says so)."""
    recorded = _recorded_repo(board)
    if recorded is None or Path(recorded).exists():
        return False
    lock = _open_lock(board)
    if lock is None:
        return False
    with lock:
        recorded = _recorded_repo(board)
        if recorded is None or Path(recorded).exists():
            return False
        return _write_atomic(board / "repo.json", json.dumps({"repo": str(_project_root(cwd))}))


def _same_folder(a: object, b: object) -> bool:
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return False


def _homes() -> set:
    """The home folders: the HOME the hook runs with and the account's own. A home is never a project."""
    out = set()
    for raw in (os.environ.get("HOME"), _account_home()):
        if raw:
            try:
                out.add(Path(raw).resolve())
            except OSError:
                pass
    return out


def _account_home() -> Optional[str]:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return None


def start_folder() -> Optional[Path]:
    """The folder this session was started in, or None when the hook cannot say.

    Claude Code gives every hook `CLAUDE_PROJECT_DIR`, "the project root where the session started": measured on 15 Nov
    2026 (2.1.325) in SessionStart (startup, resume, clear, compact), UserPromptSubmit, Stop, PreToolUse and PostToolUse,
    in a subagent's, in `async` hooks, in `claude -p` and after /restart; it does not follow a `cd` and it stays when the
    session enters a worktree, where the payload's `cwd` moves. The project of a session is this folder; `cwd` is
    used only when this is missing (KEEPER-BUILD.md section 14, rule 0). Unusable (not absolute, not a folder): None."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    if not raw or not os.path.isabs(raw):
        return None
    try:
        folder = Path(raw).resolve()
    except OSError:
        return None
    return folder if folder.is_dir() else None


def _wall(folder: Path, homes: set) -> bool:
    return folder in homes or folder == Path(folder.anchor)


def _scope(here: Path) -> tuple:
    """(folders, lenient): where a session whose project folder is here may find its project, nearest first, and the one
    folder whose board may be accepted by the older, laxer rules. KEEPER-BUILD.md sections 13 and 14 are the tables of
    every place it can be; this is their code.

    The boundary is the nearest folder at or above `here` with a `.git` entry (a folder, or a file for a submodule or a
    linked worktree), found from the filesystem and not from git, so that a repository git cannot read is a boundary
    all the same (the third round of the walk, 15 Nov 2026). The folders are `here` up to the boundary inclusive, or,
    outside any repository, up to but excluding the home folder and `/`. **A home folder and `/` are walls**: never a
    project, and a repository at a wall is not a boundary, so that a project under a home that is a dotfiles repository
    is outside any repository (the reviewer's note on 3af87b7a: it hid the project's old board). A folder inside the
    home's boards/ (a board's own folder) has no folders at all. Nothing above the boundary is ever in the list.
    `lenient` is the boundary, or `here` outside a repository."""
    try:
        here = here.resolve()
    except OSError:
        pass
    boards_root = (STATE / "boards").resolve()
    homes = _homes()
    if here == boards_root or boards_root in here.parents or _wall(here, homes):
        return [], None
    folders: list = []
    boundary = None
    for folder in (here, *here.parents):
        if _wall(folder, homes):
            break
        folders.append(folder)
        if (folder / ".git").exists():
            boundary = folder
            break
    return folders, (boundary if boundary is not None else here)


def _names(board: Path, folder: Path) -> bool:
    """Does this board's repo.json name that very folder (an existing path, the same folder)?"""
    recorded = _recorded_repo(board)
    return bool(recorded) and Path(recorded).exists() and _same_folder(recorded, folder)


def _in_doubt(board: Path) -> bool:
    """A board that cannot be told apart from this project's: no repo.json, or one that names a folder that is gone."""
    recorded = _recorded_repo(board)
    return recorded is None or not Path(recorded).exists()


def _find(here: Path, from_start: bool) -> tuple:
    """(board, folder, refused) from one folder: the walk of KEEPER-BUILD.md sections 14 and 15.
      1. the folder's own board, whose repo.json names it (`from_start` only: a `cwd` may have followed a `cd`);
      2. the folder's own board when it has no repo.json, **only when the folder is the boundary (the top of a
         repository) or is outside any repository** (`from_start` only). Below the boundary a board of that name with
         no repo.json cannot be told from another project's: it stops the walk (section 15, the fifth round);
      3. **the nearest folder that a board names wins**, walking up to the boundary; a folder below the boundary whose
         board is in doubt (`_in_doubt`: no repo.json, or one that names a folder that is gone) stops the walk;
      4. the boundary (or the folder outside a repository) is tried with `board_ok`: a board with no repo.json yet, a
         linked worktree of the recorded project, a recorded path that is gone (adopted by `board_for`). A board found
         there that belongs to another project is `refused` (and the folder says whose project was refused), for the
         one line a refused project is told (`elsewhere_note`). When in doubt, no board."""
    folders, lenient = _scope(here)
    if from_start and folders:
        own = board_dir(folders[0])
        if own.is_dir() and (_names(own, folders[0]) or (_recorded_repo(own) is None and folders[0] == lenient)):
            return own, folders[0], None
    reach = list(folders)
    for index, folder in enumerate(folders):
        board = board_dir(folder)
        if not board.is_dir():
            continue
        if _names(board, folder):
            return board, folder, None
        if folder != lenient and _in_doubt(board):
            reach = folders[:index]  # nothing at or beyond this folder is looked at
            break
    refused = None
    if lenient is not None and lenient in reach:
        board = board_dir(lenient)
        if board.is_dir():
            if board_ok(board, lenient):
                return board, lenient, None
            refused = board
    return None, (lenient if refused is not None else None), refused


def find_board(cwd: Path) -> tuple:
    """(board, folder, refused): the board of the session's project, with the folder that is the project.

    **The project is the folder the session was started in** (`start_folder`, rule 0 of KEEPER-BUILD.md section 14),
    not where its shell is now: `cwd` follows a `cd`, and a project cannot be guessed from it. **If that gives no board
    and no refusal, the same walk is done from `cwd`** (rule 7 of section 15): a session started outside its project (in
    the folder that holds it, in the home folder) and then `cd`'d into it is the project's. A refusal from the start
    folder stands. `cwd` alone is used when the variable is missing. See `_find` for the walk."""
    start = start_folder()
    if start is not None:
        found = _find(start, True)
        if found[0] is not None or found[2] is not None:
            return found
    return _find(cwd, False)


def board_for(cwd: Path) -> Optional[Path]:
    """The board of the project `cwd` is in, found from the project's root (`find_board`), or None when the folder
    name's board belongs to another project (see `board_ok`). A board whose recorded project is gone becomes this
    project's."""
    board, folder, _ = find_board(cwd)
    if board is None:
        return None
    adopt_board(board, folder)
    return board


def refused_board(cwd: Path) -> Optional[Path]:
    """The board folder this project's own name has, when it belongs to another project that still exists."""
    return find_board(cwd)[2]


def board_belongs(board: Path, cwd: Path) -> bool:
    """Does `board` (found by its path, not by a name) belong to the session's project? It is the board `find_board` finds:
    one rule, so that the keeper and the waiter cannot disagree."""
    found = find_board(cwd)[0]
    try:
        return found is not None and found.resolve() == board.resolve()
    except OSError:
        return False


def load_answers(board: Path) -> Optional[dict]:
    """{question: entry}, {} on a board nobody has tapped yet, or None when the
    file exists and cannot be read right now (the caller skips the round).

    A missing file is a new board, as serve.py's `load_answers` reads it: the
    file appears with the first tap. The caller has already checked that the
    board's folder exists."""
    try:
        text = (board / "answers.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


MARKER_BAK = MARKER + ".bak"


def _read_marker_file(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_marker(board: Path) -> Optional[dict]:
    """The record, or None when it and its last good copy are both missing or
    unreadable (then it is seeded). A missing or torn record falls back to the
    copy `write_marker` keeps beside it: without one, a lost marker told the
    whole history again as fresh rulings (the review, finding 3)."""
    data = _read_marker_file(board / MARKER)
    return data if data is not None else _read_marker_file(board / MARKER_BAK)


def _write_atomic(path: Path, text: str) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def write_marker(board: Path, marker: dict) -> bool:
    text = json.dumps(marker, indent=2, ensure_ascii=False)
    if not _write_atomic(board / MARKER, text):
        return False
    if not _write_atomic(board / MARKER_BAK, text):
        # The last good copy could not be brought up to date (a full disk): an older one must not stay, or a marker
        # lost later would be read from it and everything told since would be told again (the review of 14 Nov 2026,
        # finding 5). With no copy at all, a lost marker is seeded, and says so (`take_seed_note`).
        try:
            (board / MARKER_BAK).unlink()
        except OSError:
            pass
    return True


def at_epoch(at: object) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(at)).timestamp()
    except ValueError:
        return None


def session_start(payload: dict) -> float:
    """When this session began: its transcript's birth time, else now. Only
    `claim` reads it, and only when it has to seed a missing record."""
    raw = payload.get("transcript_path")
    if isinstance(raw, str) and raw:
        try:
            born = getattr(Path(raw).expanduser().stat(), "st_birthtime", None)
            if born:
                return float(born)
        except OSError:
            pass
    return time.time()


SEEDED_AT, SEEDED_COUNT, SEEDED_NOTED = "__seeded_at__", "__seeded_count__", "__seeded_noted__"


def seed(answers: dict, started: float = 0.0) -> dict:
    """The record when neither it nor its copy can be read: every answer present
    counts as told, whatever the session's age (F3, 14 Nov 2026).

    The earlier rule counted as told only the answers older than the session,
    which told a long-lived or resumed session every answer since its
    transcript was born, again, as fresh rulings. The cost of this rule: an
    answer recorded between the loss and the seeding is counted as told, never
    as new. That is the right side to err on, and it is said, once, to the
    session that keeps the board (`take_seed_note`). `started` is no longer
    read; it stays so that callers need not change."""
    out = {q: e.get("at") for q, e in answers.items() if isinstance(e, dict)}
    if out:
        out[SEEDED_COUNT] = len(out)
        out[SEEDED_AT] = datetime.now().astimezone().isoformat(timespec="seconds")
    return out


def esc(text: object) -> str:
    """The answers file is the owner's, and anything that can reach the board's
    port can put text in it (the review, finding 8): nothing in a question or an
    answer may close the block or open another. The agent reads `&lt;` as `<`."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


UNMARKED_LINE = (
    "  This answer was not marked as sent by a browser. Confirm it with the owner in one sentence, quoting it, before you act on "
    "anything that is hard to undo."
)


NETWORK_LINE = (
    "  This answer came from the network (a board started with --host), not from the owner's own page on this machine. Confirm it with the owner "
    "in one sentence, quoting it, before you act on anything that is hard to undo."
)


def format_block(fresh: Fresh) -> str:
    """The text both hooks give the agent, moved here from board-answers.py so
    that the two cannot drift apart."""
    lines = [
        "<board-answers>",
        f"The owner answered {len(fresh)} question(s) on the status board. "
        "These are recorded rulings, not suggestions: the recommended option "
        "records on a single tap, and any other option required an explicit "
        "confirmation in a modal. Act on them, move them to `answered` in "
        "narrative.json, and do not ask again.",
    ]
    for question, entry in fresh:
        lines.append(f"\n  Q: {esc(question)}")
        lines.append(f"  A: {esc(entry.get('answer'))}   [{esc(entry.get('at'))}]")
        # The board's server marks each answer (2.4.0); one with no mark at all is older, and gets no line. An answer judged by the network token of a
        # board started with --host is marked `network`: that token is not a proof of the owner, so it gets the same rule as an unmarked one.
        if entry.get("from") == "unmarked":
            lines.append(UNMARKED_LINE)
        elif entry.get("from") == "network":
            lines.append(NETWORK_LINE)
    lines.append("</board-answers>")
    return "\n".join(lines) + "\n"


def peek_fresh(board: Path) -> bool:
    """Cheap and unlocked: might there be something to claim? Over-approximates
    on purpose (a missing record says yes), so a quiet board costs two reads."""
    answers = load_answers(board)
    if answers is None:
        return False
    marker = read_marker(board)
    if marker is None:
        return True
    return any(isinstance(a, dict) and marker.get(q) != a.get("at") for q, a in answers.items())


def _open_lock(board: Path):
    try:
        fh = os.fdopen(os.open(board / LOCK, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    except OSError:
        return None
    end = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > end:
                fh.close()
                return None
            time.sleep(0.01)


def claim(
    board: Path,
    started: float,
    still_mine: Callable[[], bool],
    deliver: Optional[Callable[[str], None]] = None,
) -> Fresh:
    """Take the answers nobody has told yet, mark them told, and return them.

    Under the lock: read the answers (unreadable: skip, return []), read the
    record (missing or unreadable: seed it), compare, ask `still_mine()` (a
    waiter whose session is gone or restarted must not claim), write the record
    (a failed write: return [], because a waiter that woke the session on an
    answer it could not mark would wake it again after every turn), then hand
    the block to `deliver` if one is given. If `deliver` raises, the record is
    put back and nothing is returned: the answer was not told."""
    lock = _open_lock(board)
    if lock is None:
        return []
    with lock:
        answers = load_answers(board)
        if answers is None:
            return []
        marker = read_marker(board)
        if marker is None:
            marker = seed(answers, started)
            if not write_marker(board, marker):
                return []
        elif _read_marker_file(board / MARKER) is None:
            write_marker(board, marker)  # the marker was missing or torn: put the last good copy back
        fresh = sorted(
            ((q, a) for q, a in answers.items() if isinstance(a, dict) and marker.get(q) != a.get("at")),
            key=lambda qa: qa[1].get("at") or "",
        )
        if not fresh or not still_mine():
            return []
        if not write_marker(board, {**marker, **{q: a.get("at") for q, a in fresh}}):
            return []
        if deliver is not None:
            try:
                deliver(format_block(fresh))
            except (OSError, ValueError):
                write_marker(board, marker)
                return []
        return fresh


# ── who is the session this waiter belongs to ─────────────────────────
def _ps(pid: int, fields: str) -> Optional[str]:
    try:
        done = subprocess.run(
            ["ps", "-o", fields, "-p", str(pid)], capture_output=True, text=True, timeout=PS_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def find_anchor(env: Optional[dict] = None) -> Optional[int]:
    """The pid of the `claude` process this hook belongs to: the nearest
    ancestor whose command is named claude. Observed 14 Nov 2026 (Claude Code
    2.1.325): the hook's parent is claude itself, so the first step finds it,
    and the walk covers a shell in between. ANSWER_WAITER_ANCHOR_PID overrides
    it, for tests."""
    env = os.environ if env is None else env
    override = env.get("ANSWER_WAITER_ANCHOR_PID")
    if override:
        try:
            return int(override)
        except ValueError:
            return None
    pid = os.getppid()
    for _ in range(ANCHOR_LEVELS):
        info = _ps(pid, "ppid=,comm=")
        if info is None:
            return None
        parts = info.split(None, 1)
        if len(parts) < 2:
            return None
        ppid, comm = int(parts[0]), parts[1]
        if os.path.basename(comm) == "claude":
            return pid
        if ppid <= 1:
            return None
        pid = ppid
    return None


def anchor_command(pid: int) -> Optional[str]:
    """The anchor's command line. A /restart rewrites it, so a change means
    the session was re-exec'd. A second /restart may add to it again (it grew
    by the carried flags each time on 14 Nov 2026), but nothing guarantees
    that, so this is only the cheap second guard; the first is the hang-up of
    the waiter's own output, which the waiter polls."""
    out = _ps(pid, "command=")
    return " ".join(out.split()) if out else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── the waiters' registrations, and the note when none runs ───────────
def _key(anchor: Optional[int], session_id: object) -> str:
    return str(anchor) if anchor else "noanchor-" + re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))[:60]


def read_registration(board: Path, anchor: Optional[int], session_id: object = "") -> Optional[dict]:
    try:
        data = json.loads((board / "waiters" / f"{_key(anchor, session_id)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def register(board: Path, anchor: int, info: dict) -> bool:
    """Say that this process is the waiter of this claude. A newer waiter of
    the same claude overwrites it, and the older one finds out within a poll.
    Clears the 'reported' flag, so a later failure is reported again.

    Also removes the files of every anchor that is not alive any more (F7, the
    review, finding 7): one was written for every claude that ever ran in the
    project and none was ever removed, and a reused pid inherited an old
    `ended` record, which hides a waiter that crashed from `waiter_note`."""
    folder = board / "waiters"
    try:
        folder.mkdir(exist_ok=True)
        for path in folder.iterdir():
            stem = path.name.split(".", 1)[0]
            if stem.isdigit() and int(stem) != anchor and not pid_alive(int(stem)):
                path.unlink(missing_ok=True)
        tmp = folder / f"{anchor}.json.tmp"
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, folder / f"{anchor}.json")
        (folder / f"{anchor}.reported").unlink(missing_ok=True)
        return True
    except OSError:
        return False


def mark_ended(board: Path, anchor: int, pid: int, reason: str) -> None:
    """The waiter says that it ended on its own and why (woke the session,
    replaced, deadline, deaf after a restart). Only if the registration is still
    its own: a newer waiter's must not be overwritten."""
    reg = read_registration(board, anchor)
    if not reg or reg.get("pid") != pid:
        return
    folder = board / "waiters"
    try:
        tmp = folder / f"{anchor}.json.tmp"
        tmp.write_text(json.dumps({**reg, "ended": datetime.now().astimezone().isoformat(timespec="seconds"),
                                   "reason": reason}), encoding="utf-8")
        os.replace(tmp, folder / f"{anchor}.json")
    except OSError:
        pass


def waiter_note(board: Path, anchor: Optional[int], session_id: object) -> Optional[str]:
    """The block telling the agent that answers reach it only with the owner's
    next message, or None when a waiter runs or this has been said already.
    Caller marks it with `mark_reported` once it has printed it.

    A waiter that ended on its own (`ended` in its registration) is not a
    missing one. The end-to-end run of 14 Nov 2026 found it: a waiter wakes the
    session by exiting 2, and the wake itself fires UserPromptSubmit, so this
    check ran in the instant after the waiter had gone and before the next had
    started, and told the agent, falsely, that no waiter was running. Only a
    waiter that is gone without having said so (killed, crashed) or never
    registered is a failure of the fast path."""
    reg = read_registration(board, anchor, session_id)
    if reg and isinstance(reg.get("pid"), int) and pid_alive(reg["pid"]):
        return None
    if reg and reg.get("ended"):
        return None
    if (board / "waiters" / f"{_key(anchor, session_id)}.reported").exists():
        return None
    since = reg.get("started") if reg else None
    return (
        "<board-watcher>\n"
        "Answers on this board are reaching you only when the owner sends a "
        f"message: no answer waiter has run in this session since {since or 'it started'}. "
        "Tell the owner in one line. Do not start a watcher yourself (no Monitor, "
        "no background command): the plugin starts its own, and a background task "
        "you start would make /restart refuse.\n"
        "</board-watcher>\n"
    )


def mark_reported(board: Path, anchor: Optional[int], session_id: object) -> None:
    try:
        folder = board / "waiters"
        folder.mkdir(exist_ok=True)
        (folder / f"{_key(anchor, session_id)}.reported").write_text("1")
    except OSError:
        pass


# ── the keeper: only the session that last wrote the board is told ────
# The owner's ruling, 14 Nov 2026: "Only the session that last wrote the board's
# content is told, within seconds. Other sessions in the folder stay silent. If
# you start a new session and it takes over the board, it becomes the one that
# is told. One ruling, one agent acting on it." Before it, any session in the
# folder claimed whatever it polled first (the review: the lead was told 3 of 10).
#
# keeper.json, in the board's folder:
#   {"v": 1, "narrative_sig": [ino, mtime_ns, size], "writers": [{"session_id",
#    "anchor", "via", "agent_id", "at", "t"}, ...]}   newest writer first, at most five.
# Written only by `record_writer`, under the same lock as the marker.
#
# WHEN A SESSION IS RECORDED (the review of 14 Nov 2026, findings 2 and 3; KEEPER-ATTRIBUTION.md)
# Only for a change that one of its own tool calls is seen making, and that nothing
# else could have made. When in doubt nobody is recorded and the keeper stays who it
# was. A Write or Edit to the file is recorded at once (the tool's own input names
# the file). A Bash command that names the file is bracketed: its PreToolUse stores
# the file's signature, its PostToolUse compares, and the session is recorded only
# if the file changed during that call and no other session's call that also named
# the file overlapped it (`open_bracket`, `close_bracket`). Anything else (a script
# that does not name the file, the owner's editor, serve.py, a worker, a headless
# run) records nobody. There is no sweep at the end of a turn any more: it recorded
# whichever session's turn happened to span a change, a session that had written
# nothing.
KEEPER = "keeper.json"
MAX_WRITERS = 5
ROLES_NOT_TOLD = ("worker", "teammate", "headless")
BRACKETS = "brackets"
BRACKET_CLOSED_KEEP_S = 120.0  # a closed bracket that changed the file still counts for an overlapping call this long
BRACKET_OPEN_MAX_S = 24 * 3600.0  # an open one (no PostToolUse: the turn was interrupted) is dropped after a day


def narrative_sig(board: Path) -> Optional[list]:
    try:
        st = (board / "narrative.json").stat()
    except OSError:
        return None
    return [st.st_ino, st.st_mtime_ns, st.st_size]


def read_keeper(board: Path) -> dict:
    data = _read_marker_file(board / KEEPER)
    if not data or not isinstance(data.get("writers"), list):
        return {"v": 1, "writers": []}
    return data


def record_writer(board: Path, session_id: str, anchor: Optional[int], via: str, agent_id: Optional[str] = None,
                  since: Optional[float] = None) -> bool:
    """This session wrote the board just now: it is the keeper. A session is in
    the list once, at its newest; the list keeps the last five. With `since`
    (a bracket's start), nothing is recorded if another writer was recorded
    after it: that write happened during this session's call and is not its."""
    lock = _open_lock(board)
    if lock is None:
        return False
    with lock:
        keeper = read_keeper(board)
        if since is not None and keeper["writers"] and (keeper["writers"][0].get("t") or 0.0) >= since:
            return False
        entry = {"session_id": session_id, "anchor": anchor, "via": via, "agent_id": agent_id,
                 "at": datetime.now().astimezone().isoformat(timespec="seconds"), "t": time.time()}
        writers = [entry] + [w for w in keeper["writers"] if w.get("session_id") != session_id]
        text = json.dumps({"v": 1, "narrative_sig": narrative_sig(board), "writers": writers[:MAX_WRITERS]}, indent=2)
        return _write_atomic(board / KEEPER, text)


# ── brackets: did THIS call change the file? ──────────────────────────
# boards/<slug>/brackets/<tool_use_id>.json: {session_id, agent_id, pre, t0, post?, t1?}, written under the marker's
# lock. Spike of 14 Nov 2026 (Claude Code 2.1.325): a Bash call's PreToolUse and PostToolUse carry the same
# tool_use_id, Pre first; a subagent's carry the lead's session_id and the same pair.
def _bracket_file(board: Path, tool_use_id: str) -> Path:
    return board / BRACKETS / (re.sub(r"[^A-Za-z0-9_-]", "", tool_use_id)[:100] + ".json")


def _brackets(board: Path) -> dict:
    out = {}
    try:
        paths = list((board / BRACKETS).glob("*.json"))
    except OSError:
        return out
    for path in paths:
        data = _read_marker_file(path)
        if data is not None and isinstance(data.get("t0"), (int, float)):
            out[path] = data
    return out


def _last_hook(board: Path, session_id: object) -> float:
    """When this session last ran a hook of ours that registers it (the waiter's start: SessionStart, a prompt, a
    Stop), from its registrations; 0.0 when none is known."""
    last = 0.0
    try:
        paths = list((board / "waiters").glob("*.json"))
    except OSError:
        return last
    for path in paths:
        reg = _read_marker_file(path)
        if reg and reg.get("session_id") == session_id:
            last = max(last, at_epoch(reg.get("started")) or 0.0)
    return last


def _prune_brackets(board: Path, now: float) -> None:
    """Open ones older than a day, or whose session has run a later hook (the re-check of 15 Nov 2026, finding 2: a
    call whose PostToolUse never ran was interrupted; the session cannot have a call open and start its next turn);
    closed ones a couple of minutes after they closed, unless a call that began before they closed is still open (it
    overlaps them: they still count against it)."""
    entries = _brackets(board)
    moved_on = {path for path, b in entries.items() if b.get("t1") is None and _last_hook(board, b.get("session_id")) > b["t0"]}
    live_open = [b["t0"] for path, b in entries.items()
                 if b.get("t1") is None and path not in moved_on and now - b["t0"] <= BRACKET_OPEN_MAX_S]
    for path, data in entries.items():
        if data.get("t1") is None:
            stale = path in moved_on or now - data["t0"] > BRACKET_OPEN_MAX_S
        else:
            stale = now - data["t1"] > BRACKET_CLOSED_KEEP_S and not any(t0 <= data["t1"] for t0 in live_open)
        if stale:
            try:
                path.unlink()
            except OSError:
                pass


def open_bracket(board: Path, tool_use_id: str, session_id: str, agent_id: Optional[str] = None) -> bool:
    """PreToolUse of a Bash call that names narrative.json: remember the file's signature as it is now."""
    lock = _open_lock(board)
    if lock is None:
        return False
    with lock:
        now = time.time()
        _prune_brackets(board, now)
        try:
            (board / BRACKETS).mkdir(exist_ok=True)
        except OSError:
            return False
        data = {"session_id": session_id, "agent_id": agent_id, "pre": narrative_sig(board), "t0": now}
        return _write_atomic(_bracket_file(board, tool_use_id), json.dumps(data))


def close_bracket(board: Path, tool_use_id: str) -> str:
    """PostToolUse of that call. What happened to the file during it, as one of:
      "unchanged"      the signature is what it was: a read.
      "explained"      another session's Write or Edit was recorded during the call and left the file as it is now.
      "contested"      another SESSION's call that named the file started from the same state and is still open, or
                       changed the file: nothing says which of the two did it. (A session's own parallel calls
                       do not contest each other: whichever changed it, the session did.)
      "changed-alone"  it changed during this call and nothing else could have: this session did it.
      "unknown"        no bracket (Pre did not run or was pruned): nothing is claimed."""
    lock = _open_lock(board)
    if lock is None:
        return "unknown"
    with lock:
        now = time.time()
        path = _bracket_file(board, tool_use_id)
        mine = _read_marker_file(path)
        if mine is None or not isinstance(mine.get("t0"), (int, float)):
            return "unknown"
        post = narrative_sig(board)
        mine = {**mine, "post": post, "t1": now}
        _write_atomic(path, json.dumps(mine))
        if post == mine.get("pre"):
            verdict = "unchanged"
        else:
            keeper = read_keeper(board)
            newest = keeper["writers"][0] if keeper["writers"] else {}
            if keeper.get("narrative_sig") == post and (newest.get("t") or 0.0) >= mine["t0"]:
                verdict = "explained"
            elif any(other.get("pre") == mine.get("pre") and (other.get("t1") is None or other.get("post") != other.get("pre"))
                     and other.get("session_id") != mine.get("session_id")  # a session's own calls (two Bash calls in one
                     for other_path, other in _brackets(board).items()      # message) cannot be someone else's change
                     if other_path != path):
                verdict = "contested"
            else:
                verdict = "changed-alone"
        _prune_brackets(board, now)
        return verdict


def bracket_start(board: Path, tool_use_id: str) -> Optional[float]:
    """When this call's bracket opened (the caller passes it to `record_writer` as `since`)."""
    data = _read_marker_file(_bracket_file(board, tool_use_id)) or {}
    started = data.get("t0")
    return float(started) if isinstance(started, (int, float)) else None


def mark_unattributed(board: Path, verdict: str) -> None:
    """A change that no session could be given (contested, or explained by another's write): said in keeper.json,
    with the writers as they were. Nothing reads it; it is there for whoever looks at why nobody was recorded."""
    lock = _open_lock(board)
    if lock is None:
        return
    with lock:
        keeper = read_keeper(board)
        keeper = {**keeper, "unattributed": {"verdict": verdict, "sig": narrative_sig(board),
                                             "at": datetime.now().astimezone().isoformat(timespec="seconds")}}
        keeper.setdefault("narrative_sig", None)
        _write_atomic(board / KEEPER, json.dumps(keeper, indent=2))


def live_sessions(board: Path) -> list:
    """The sessions that are open: a registration whose claude is alive and
    whose role is an ordinary session. Workers, teammates and headless runs are
    never in it. `started` is when that session last ran one of the hooks."""
    out = []
    try:
        paths = list((board / "waiters").glob("*.json"))
    except OSError:
        return out
    for path in paths:
        if not path.stem.isdigit():
            continue
        anchor = int(path.stem)
        reg = _read_marker_file(path)
        if not reg or not pid_alive(anchor) or reg.get("role", "session") in ROLES_NOT_TOLD:
            continue
        out.append({"session_id": reg.get("session_id"), "anchor": anchor,
                    "started": at_epoch(reg.get("started")) or 0.0, "orca_role": reg.get("orca_role")})
    return sorted(out, key=lambda e: e["started"], reverse=True)


def keeper_for(board: Path, me: Optional[dict] = None) -> Optional[dict]:
    """The one session that is told, from files alone (so every session, asking
    inside the same lock, gets the same answer). In this order:
      1. the last writer, if its session is open (its own claude first; a
         second window of the same session only when that one closes);
      2. an earlier writer that is open;
      3. the Orca coordinator of the project (the registration says so);
      4. the session most recently in use.
    `me` is the asking session, counted as open and as in use right now: the
    prompt hook passes it, because a session the owner types into is the most
    recently active at that moment. Returns the open session's entry, with
    `why` (writer, coordinator, recent) and, for a writer, `writer_at`."""
    live = live_sessions(board)
    if me:
        for entry in live:
            if entry["session_id"] == me["session_id"] and entry["anchor"] == me["anchor"]:
                entry["started"] = me["started"]
                break
        else:
            live.append(dict(me))
        live.sort(key=lambda e: e["started"], reverse=True)
    for writer in read_keeper(board)["writers"]:
        same = [e for e in live if e["session_id"] == writer.get("session_id")]
        if same:
            chosen = next((e for e in same if e["anchor"] == writer.get("anchor")), same[0])
            return {**chosen, "why": "writer", "writer_at": writer.get("at")}
    for entry in live:
        if entry.get("orca_role") == "coordinator":
            return {**entry, "why": "coordinator"}
    return {**live[0], "why": "recent"} if live else None


def answered_since(board: Path, since: float) -> bool:
    """Did an answer land on this board after `since`, told or not? For the
    one line a session that is not the keeper gets: the keeper's waiter claims
    an answer within seconds, so by the owner's next prompt in another session
    nothing is waiting any more, and the line would never come."""
    answers = load_answers(board) or {}
    return any(isinstance(e, dict) and (at_epoch(e.get("at")) or 0.0) > since for e in answers.values())


def is_keeper(board: Path, session_id: str, anchor: Optional[int], me: Optional[dict] = None) -> bool:
    keeper = keeper_for(board, me)
    return bool(keeper and keeper["session_id"] == session_id and keeper["anchor"] == anchor)


def _who(keeper: dict) -> str:
    sid = str(keeper.get("session_id"))[:8]
    if keeper["why"] == "writer":
        when = at_epoch(keeper.get("writer_at"))
        stamp = datetime.fromtimestamp(when).strftime("%H:%M") if when else "earlier"
        return f"session {sid}, last write {stamp}"
    if keeper["why"] == "coordinator":
        return f"the Orca coordinator of this project, session {sid}"
    return f"the most recently active session here, {sid}"


def keeper_note(board: Path, anchor: Optional[int], session_id: object, keeper: Optional[dict]) -> Optional[str]:
    """One line to a session that is not the keeper, once per keeper, and only
    when an answer is waiting that it is not given. Said on the owner's own
    prompt, never by the waiter: a session that is not told must not be woken."""
    if not keeper:
        return None
    noted = board / "waiters" / f"{_key(anchor, session_id)}.keeper-noted"
    try:
        if noted.read_text(encoding="utf-8") == str(keeper["session_id"]):
            return None
    except OSError:
        pass
    return (
        "<board-keeper>\n"
        f"The owner's answers on this board go to another session: the one that last wrote narrative.json ({_who(keeper)}), "
        "not this one. None is shown here, and you need not do anything about them. If the owner asks this session to keep "
        "the board, write narrative.json with the Write or Edit tool; from then on its answers come here.\n"
        "</board-keeper>\n"
    )


def mark_keeper_noted(board: Path, anchor: Optional[int], session_id: object, keeper: dict) -> None:
    try:
        folder = board / "waiters"
        folder.mkdir(exist_ok=True)
        (folder / f"{_key(anchor, session_id)}.keeper-noted").write_text(str(keeper["session_id"]), encoding="utf-8")
    except OSError:
        pass


# ── an answer that waits unheard in an idle keeper (the review, finding 4) ─────
# The ruling stays strict: an answer is never given to another session because the keeper is idle. What changes is
# that the owner is told. A session that is open but has not run a hook for 21 days has no waiter, and a window he
# forgot has none either; the answer then waits until he types in that very session. Every other session of the
# folder says so, once per answer, and without the answer.
UNHEARD_MIN_AGE_S = 10.0  # an answer younger than this is between one waiter and the next, not unheard
KEEPER_IDLE_S = 600.0  # a keeper with no waiter that has done nothing visible for this long is idle (not in the middle of a turn)


def waiter_running(board: Path, anchor: Optional[int]) -> bool:
    reg = read_registration(board, anchor)
    return bool(reg and isinstance(reg.get("pid"), int) and pid_alive(reg["pid"]) and not reg.get("ended"))


def last_activity(board: Path, keeper: dict) -> float:
    """When the keeper's session last did anything we can see: its last hook (the waiter's start), the waiter's end
    when that woke it (a turn began), a write to its transcript, and its brackets. 0.0 when nothing is known."""
    reg = read_registration(board, keeper.get("anchor")) or {}
    seen = [at_epoch(reg.get("started")) or 0.0]
    if str(reg.get("reason", "")).startswith("woke"):
        seen.append(at_epoch(reg.get("ended")) or 0.0)
    transcript = reg.get("transcript")
    if isinstance(transcript, str) and transcript:
        try:
            seen.append(Path(transcript).expanduser().stat().st_mtime)
        except OSError:
            pass
    sid = keeper.get("session_id")
    for data in _brackets(board).values():
        if data.get("session_id") == sid:
            seen.extend(x for x in (data.get("t0"), data.get("t1")) if isinstance(x, (int, float)))
    return max(seen)


def _keeper_deaf(board: Path, keeper: dict) -> bool:
    """No answer waiter runs in the keeper (whatever made it the keeper: it wrote the board, it is the coordinator,
    or it is the session most recently in use) and it has been idle for KEEPER_IDLE_S: a session in the middle of a
    long turn has no waiter either, and will be told when the turn ends (the re-check, finding 3)."""
    if waiter_running(board, keeper.get("anchor")):
        return False
    return time.time() - last_activity(board, keeper) > KEEPER_IDLE_S


def unheard_answers(board: Path, keeper: Optional[dict]) -> list:
    """[(question, at)] of the answers nobody has been told, older than ten seconds, while the keeper has no waiter."""
    if not keeper or not _keeper_deaf(board, keeper):
        return []
    answers, marker = load_answers(board), read_marker(board)
    if not answers or marker is None:  # no record yet: the seeding, not this, deals with what is there
        return []
    now = time.time()
    waiting = [(q, a.get("at")) for q, a in answers.items()
               if isinstance(a, dict) and marker.get(q) != a.get("at") and now - (at_epoch(a.get("at")) or now) > UNHEARD_MIN_AGE_S]
    return sorted(waiting, key=lambda qa: at_epoch(qa[1]) or 0.0)


def unheard_note(board: Path, anchor: Optional[int], session_id: object, keeper: Optional[dict]) -> Optional[tuple]:
    """None when the case does not apply (the caller gives the plain keeper line). Otherwise (text, [at, ...]):
    the block for the answers not yet named to this session, or ("", []) when all were. The block never holds the
    answer, only the question."""
    waiting = unheard_answers(board, keeper)
    if not waiting:
        return None
    try:
        named = set(json.loads((board / "waiters" / f"{_key(anchor, session_id)}.unheard-noted").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        named = set()
    new = [(q, at) for q, at in waiting if at not in named]
    if not new:
        return "", []
    first = at_epoch(new[0][1])
    stamp = datetime.fromtimestamp(first).strftime("%H:%M") if first else "earlier"
    questions = ", ".join(f'"{esc(q)}"' for q, _ in new)
    if len(new) == 1:
        what = f"An answer from the owner is waiting unheard in the session that keeps this board ({_who(keeper)}). It was recorded at {stamp} for the question {questions}"
    else:
        what = (f"{len(new)} answers from the owner are waiting unheard in the session that keeps this board ({_who(keeper)}). "
                f"The oldest was recorded at {stamp}, for the questions {questions}")
    text = (
        "<board-keeper>\n"
        f"{what}, and that session has not been told, because no answer waiter is running in it (it has been idle a long "
        "time, or its window was left open and forgotten). Tell the owner so, in one line. Do not act on the answer here: "
        "it belongs to that session. He can type in that session to have it acted on, or ask this session to take the "
        "board over by writing narrative.json.\n"
        "</board-keeper>\n"
    )
    return text, [at for _, at in new]


def mark_unheard_noted(board: Path, anchor: Optional[int], session_id: object, ats: list) -> None:
    path = board / "waiters" / f"{_key(anchor, session_id)}.unheard-noted"
    try:
        path.parent.mkdir(exist_ok=True)
        try:
            before = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = []
        path.write_text(json.dumps(sorted(set(before if isinstance(before, list) else []) | set(ats))), encoding="utf-8")
    except OSError:
        pass


# ── a project that is refused its board's name (the review, finding 1) ─
def elsewhere_note(cwd: Path, anchor: Optional[int], session_id: object, board: Optional[Path] = None) -> Optional[str]:
    """Said once per session in a project whose folder name's board belongs to another project that still exists:
    nothing from that board reaches this session, and the way out. A silent refusal was the finding."""
    _, folder, refused = find_board(cwd)  # the project that was refused: the start folder's, else the shell's
    cwd = folder or cwd
    board = board or refused or board_dir(cwd)
    flag = board / "refused" / f"{_key(anchor, session_id)}.noted"
    if flag.exists():
        return None
    try:
        flag.parent.mkdir(exist_ok=True)
        flag.write_text("1")
    except OSError:
        return None
    return (
        "<board-elsewhere>\n"
        f"This project ({esc(_project_root(cwd))}) has no status board of its own: the board folder for its name, "
        f"{esc(board.name)}, belongs to {esc(_recorded_repo(board) or 'another project')}. Nothing from that board reaches this session. "
        "To give this project its own board, rename its folder.\n"
        "</board-elsewhere>\n"
    )


def seed_note_due(board: Path) -> bool:
    marker = read_marker(board) or {}
    return bool(marker.get(SEEDED_COUNT)) and not marker.get(SEEDED_NOTED)


def take_seed_note(board: Path) -> Optional[str]:
    """Said once, to the session that is told: the record of what was already
    told was lost and rebuilt, and the answers then on the board are counted as
    told (F3). Marked under the lock, so two sessions do not both say it."""
    lock = _open_lock(board)
    if lock is None:
        return None
    with lock:
        marker = read_marker(board)
        if not marker or not marker.get(SEEDED_COUNT) or marker.get(SEEDED_NOTED):
            return None
        if not write_marker(board, {**marker, SEEDED_NOTED: True}):
            return None
        return (
            "<board-answers-note>\n"
            "The plugin's record of which answers you were already told was missing and was rebuilt at "
            f"{marker.get(SEEDED_AT)}: the {marker[SEEDED_COUNT]} answers on the board then are counted as told, and none of "
            "them was sent to you again. If you are not sure you acted on all of them, read answers.json and compare it "
            "with `answered` in narrative.json.\n"
            "</board-answers-note>\n"
        )


def is_headless(command: Optional[str]) -> bool:
    """A `claude -p` run: a script or a scheduled job. It cannot act on a ruling
    and its async hooks are killed at teardown, so it is never told and never
    counts as a session. Known from its command line (`claude -p <prompt>`,
    observed 14 Nov 2026). CLAUDE_CODE_ENTRYPOINT is `sdk-cli` there and `cli` in
    an interactive session, but an Orca chat surface may be an SDK entrypoint,
    and silencing the owner's own session is the worse mistake: not used."""
    tokens = (command or "").split()
    return any(t in ("-p", "--print") for t in tokens[1:12])
