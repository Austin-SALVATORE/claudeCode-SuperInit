#!/usr/bin/env python3
"""Who is working, read from Orca itself: coordinator, workers, sessions.

Under cmux the board learned who was working from Claude Code's team config
and from transcript mtimes. Under Orca there is no team to read: the main
session is a *coordinator* that starts *workers* with `orca orchestration
worker-start`, each worker is a session of its own, and Orca keeps the
record of both. So the board asks Orca.

Three properties this file exists to hold, each with its reason:

  READ-ONLY.   Every command line is assembled from `READ_ONLY` (four
               listings), `AGENTS_PLAIN` (one of the four, without a flag an
               older Orca may not know) or `TASK_TITLES` (a fifth listing).
               There is no code path that can build another subcommand, so
               the board cannot start, stop, message or release anything in
               Orca.
               `check` in particular is never run: it takes the caller's
               mail, and the board has no mail.

  NEVER ON THE REQUEST PATH.  The page polls every few seconds and each
               `orca` call costs a quarter of a second of CPU. `Probe`
               refreshes in a background thread and `view()` returns the
               last snapshot at once. A missing, slow or failing CLI costs
               a request nothing; the board then derives the team the way
               it did before Orca.

  NO GUESSING. A coordinator row is shown only for a live terminal that an
               Orca Run names as its `coordinator_handle` and that sits in
               this project's checkout. The alternative, "the newest
               session in this folder is the lead", is the wrong
               attribution SKILL.md records, and under Orca it is wrong
               more often: workers started with `--worktree current` write
               their transcripts into the same folder as the coordinator.

The caller's identity is removed from the environment of every call. The
board server is usually started by a session-start hook, so it inherits
that session's `ORCA_TERMINAL_HANDLE`; with it, `worker-list` is scoped to
the Run bound to that one terminal, and the answer would depend on which
session happened to start the board. Without it the scope is every Run,
which is what a project-wide board needs.

Measured against Orca 1.4.257 (12 Nov 2026). Every field is read with
`.get()` and a type check: a field that moves or disappears in a later
Orca costs the board that fact, never the report.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

# ── the only commands this module can run ─────────────────────────────
READ_ONLY: dict[str, tuple[str, ...]] = {
    "runs": ("orchestration", "run-list", "--json", "--full", "--limit", "100"),
    "workers": ("orchestration", "worker-list", "--json", "--full", "--limit", "100"),
    "terminals": ("terminal", "list", "--json"),
    # `--agent-wait` adds Orca's verdict on an agent parked at a prompt only a
    # person can answer. Still a listing; it costs nothing measurable (0.18 s
    # with it, 0.17 to 0.25 s without, 13 Nov 2026).
    "agents": ("worktree", "ps", "--json", "--agent-wait"),
}
# The same listing for an Orca that does not know `--agent-wait`. Tried only
# after the line above was refused, so an older Orca loses the "waiting"
# state and nothing else.
AGENTS_PLAIN: tuple[str, ...] = ("worktree", "ps", "--json")
# `task-list --run <id>` gives a Task its title. Asked only for a Run whose
# worker has no title yet (see `Probe._fetch_titles`), then remembered: a
# Task's title does not change.
TASK_TITLES: tuple[str, ...] = ("orchestration", "task-list", "--json", "--run")

CLI_TIMEOUT_S = 5.0  # one call normally answers in 0.3 s
REFRESH_BUSY_S = 10.0  # someone in this project is working
REFRESH_IDLE_S = 30.0  # Orca knows the project, nobody is working
REFRESH_UNKNOWN_S = 60.0  # Orca has nothing for this project
BUSY_WITHIN_MIN = 5.0  # "working", or moved this recently, keeps the busy cadence
RETRY_AFTER_FAILURE_S = 20.0
RECHECK_MISSING_S = 120.0  # the CLI is not installed: look again rarely
# A snapshot older than this is not served. Longer than the idle refresh, so
# one failed call does not flip the page between two derivations.
STALE_AFTER_S = 90.0
MAX_TITLE_RUNS = 6  # task lists fetched per refresh, at most
# A Run id is passed to `task-list --run`. One that began with `-` would be
# read there as a flag, so anything that is not plainly an id is not passed.
RUN_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")

# Which Orca the board talks to is the session's choice; who is asking is not.
KEEP_ORCA_ENV = ("ORCA_USER_DATA_PATH", "ORCA_DEV_REPO_ROOT", "ORCA_CLI_COMMAND")
OFF_VALUES = ("0", "off", "false", "no")

# Orca's own words for a worker that has settled, mapped to what the page says.
SETTLED = {"succeeded": "done", "failed": "failed", "stopped": "stopped", "abandoned": "abandoned"}
# An agent whose turn ended is not "done" with its task; it is waiting for input.
AGENT_STATE = {"done": "idle"}
LIVE_STATES = ("working", "monitoring", "waiting")


class OrcaError(Exception):
    """The CLI did not give a usable answer. Never reaches the page."""


class OrcaTimeout(OrcaError):
    """The CLI did not answer in time. Asking it again would wait again."""


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return (env.get("STATUS_BOARD_ORCA") or "").strip().lower() not in OFF_VALUES


def resolve_cli(
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The Orca executable for this session, by Orca's own rule, or None.

    Never a bare `orca` on Linux outside an Orca terminal: there it is
    normally the GNOME screen reader, and running it starts speech.
    """
    raw = (env.get("ORCA_CLI_COMMAND") or "").strip()
    if raw:
        try:
            argv = shlex.split(raw)
        except ValueError:
            return None
    elif env.get("ORCA_DEV_REPO_ROOT"):
        argv = ["orca-dev"]
    elif platform.startswith("linux") and not (
        env.get("ORCA_TERMINAL_HANDLE") or env.get("TERM_PROGRAM") == "Orca"
    ):
        argv = ["orca-ide"]
    else:
        argv = ["orca"]
    if not argv:
        return None
    exe = which(argv[0])
    return [exe, *argv[1:]] if exe else None


def cli_env(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The environment minus the caller's Orca identity (see module docstring)."""
    return {k: v for k, v in env.items() if not k.startswith("ORCA_") or k in KEEP_ORCA_ENV}


def run_json(
    cli: list[str], args: tuple[str, ...], env: Mapping[str, str], timeout: float = CLI_TIMEOUT_S
) -> dict:
    """One listing, as the `result` object of Orca's JSON answer."""
    try:
        proc = subprocess.Popen(
            [*cli, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # errors repeat on stderr as plain text
            env=dict(env),
            cwd=str(Path.home()),
            start_new_session=True,
        )
    except OSError as exc:
        raise OrcaError(f"cannot run: {exc}") from exc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        raise OrcaTimeout("timed out") from exc
    if proc.returncode != 0:
        raise OrcaError(f"exit {proc.returncode}")
    # Read as bytes and decoded leniently. Orca cuts terminal previews and
    # agent messages at a fixed length, sometimes through the middle of a
    # character, and a strict decode then refused the whole answer: one such
    # preview on screen switched every Orca row off (seen 13 Nov 2026, byte
    # 0xED in `terminal list` and in `worktree ps`). The board never shows
    # those strings, so a replaced character costs nothing.
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError as exc:
        raise OrcaError("not JSON") from exc
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(result, dict):
        raise OrcaError("not ok")
    return result


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the CLI and anything it started
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=1)
    except (OSError, subprocess.SubprocessError):
        pass


# ── parsing: keep the few fields the board shows, drop the rest ───────
#
# `terminal list` and `worktree ps` also return terminal previews, prompts,
# tool input and the last assistant message. None of it is kept, so none of
# it can reach report.json.


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(value: object) -> str:
    """A worktree id is `<repo-id>::<path>`; a path is a path. Resolved, so it
    compares equal to the board's own resolved repo path (/tmp, /private/tmp)."""
    text = _text(value)
    return os.path.realpath(text.split("::", 1)[-1]) if text else ""


def _millis(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _rows(result: dict, key: str) -> list[dict]:
    rows = result.get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _run_id(value: object) -> str:
    """A Run id, or "" for anything that does not look like one."""
    text = _text(value)
    return text if RUN_ID.match(text) else ""


def parse_runs(result: dict) -> dict[str, dict]:
    return {
        _run_id(r.get("id")): {
            "objective": _text(r.get("objective")),
            "coordinator": _text(r.get("coordinator_handle")),
        }
        for r in _rows(result, "runs")
        if _run_id(r.get("id"))
    }


def parse_terminals(result: dict) -> dict[str, dict]:
    out = {}
    for t in _rows(result, "terminals"):
        handle = _text(t.get("handle"))
        if not handle:
            continue
        tab, leaf = _text(t.get("tabId")), _text(t.get("leafId"))
        out[handle] = {
            "path": _path(t.get("worktreePath")),
            "title": _text(t.get("title")),
            "pane": f"{tab}:{leaf}" if tab and leaf else "",
        }
    return out


def parse_agents(result: dict) -> dict[str, dict]:
    """Agents by pane key. `updatedAt` is the agent's last status event, which
    is what "last moved" means; a terminal's `lastOutputAt` is not, because an
    idle session still redraws its status line every minute."""
    out = {}
    for tree in _rows(result, "worktrees"):
        path = _path(tree.get("path"))
        for a in _rows(tree, "agents"):
            pane = _text(a.get("paneKey"))
            if not pane:
                continue
            out[pane] = {
                "path": path,
                "state": _text(a.get("state")),
                "mode": _text(a.get("workingMode")),
                "updatedAt": _millis(a.get("updatedAt")),
                "title": _text(a.get("taskTitle")) or _text(a.get("displayName")),
                "name": _text(a.get("conversationName")),
                "parent": _text(a.get("parentPaneKey")),
                "waiting": bool(a.get("agentWait")),
            }
    return out


def parse_workers(result: dict) -> tuple[dict, ...]:
    out = []
    for w in _rows(result, "workers"):
        dispatch = _text(w.get("dispatchId"))
        if not dispatch:
            continue
        proj = w.get("projection") if isinstance(w.get("projection"), dict) else {}
        live = proj.get("liveness") if isinstance(proj.get("liveness"), dict) else {}
        stage = proj.get("stage") if isinstance(proj.get("stage"), dict) else {}
        space = proj.get("workspace") if isinstance(proj.get("workspace"), dict) else {}
        res = w.get("resource") if isinstance(w.get("resource"), dict) else {}
        out.append(
            {
                "dispatch": dispatch,
                "task": _text(w.get("taskId")),
                "run": _run_id(w.get("runId")),
                # `--full` says workerState; the concise view says state.
                "state": _text(w.get("workerState")) or _text(w.get("state")),
                "terminal": _text(w.get("agentTerminalHandle")) or _text(w.get("terminal")),
                "path": _path(space.get("id")) or _path(res.get("worktreeId")) or _path(w.get("workspace")),
                "liveness": _text(live.get("verdict")) or _text(w.get("liveness")),
                "observedAt": _millis(live.get("observedAt")),
                "activity": _text(stage.get("activity")),
            }
        )
    return tuple(out)


def parse_titles(result: dict) -> dict[str, str]:
    return {
        _text(t.get("id")): _text(t.get("title"))
        for t in _rows(result, "tasks")
        if _text(t.get("id")) and _text(t.get("title"))
    }


@dataclass(frozen=True)
class Snapshot:
    taken: float  # time.time() when Orca answered
    runs: dict[str, dict] = field(default_factory=dict)
    workers: tuple[dict, ...] = ()
    terminals: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, dict] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class View:
    """What the board may say about Orca right now.

    state: `ok` (snapshot is fresh), `checking` (an answer is on its way:
    the first one, or the first after nobody asked for a while), `off`
    (disabled), `missing` (no CLI) or `down` (the CLI is failing).
    """

    state: str
    snapshot: Snapshot | None = None


# ── from Orca's records to the board's rows: pure, no I/O ─────────────


def role_of(title: str) -> str:
    """The role that opens a Task title: `architect | WS-03 | options` -> architect.

    A title with no separator names no role: the whole title is not a name.
    """
    parts = re.split(r"\s*[|·]\s*", title.strip(), maxsplit=1)
    head = parts[0].strip()
    return head if len(parts) == 2 and 0 < len(head) <= 40 else ""


def _coordinators(snap: Snapshot, paths: frozenset[str]) -> dict[str, list[str]]:
    """Coordinator terminals in this project -> the Runs each one coordinates."""
    out: dict[str, list[str]] = {}
    for run_id, run in snap.runs.items():
        handle = run.get("coordinator") or ""
        term = snap.terminals.get(handle)
        if term and term["path"] in paths:
            out.setdefault(handle, []).append(run_id)
    return out


def _ours(snap: Snapshot, paths: frozenset[str], coords: dict[str, list[str]]) -> list[dict]:
    """This project's workers: those of a Run coordinated from here, and those
    working in one of its checkouts whoever coordinates them."""
    run_ids = {r for runs in coords.values() for r in runs}
    return [w for w in snap.workers if w["run"] in run_ids or (w["path"] and w["path"] in paths)]


def _agent_state(agent: dict | None) -> str:
    if not agent:
        return ""
    if agent.get("waiting"):
        return "waiting"
    if agent.get("mode"):
        return agent["mode"]
    return AGENT_STATE.get(agent.get("state", ""), agent.get("state", ""))


def _worker_state(worker: dict, agent: dict | None) -> str:
    settled = SETTLED.get(worker["state"])
    if settled:
        return settled
    if worker["liveness"] == "exited":
        return "exited"
    return _agent_state(agent) or ("working" if worker["activity"] == "working" else "unknown")


def _last_min(stamp: float | None, now: float) -> float | None:
    return None if stamp is None else round(max(0.0, (now * 1000 - stamp) / 60000), 1)


def _recent(state: str, last_min: float | None, within_hours: float) -> bool:
    if state in LIVE_STATES:
        return True
    return last_min is not None and last_min <= within_hours * 60


def _where(path: str, repo: str) -> str:
    return "" if not path or path == repo else Path(path).name


def knows_project(snap: Snapshot, paths: frozenset[str]) -> bool:
    """True when Orca has anything in this project: a terminal, an agent, a
    worker, or a Run coordinated from it. When it has nothing, the project is
    not run under Orca and the board keeps its older derivation."""
    return (
        any(t["path"] in paths for t in snap.terminals.values())
        or any(a["path"] in paths for a in snap.agents.values())
        or any(w["path"] in paths for w in snap.workers)
    )


def busy(snap: Snapshot, paths: frozenset[str], now: float) -> bool:
    """Somebody here is working, or moved in the last few minutes. Decides how
    often Orca is asked; a session that has sat in `monitoring` for an hour
    does not keep the fast cadence."""
    return any(
        row["state"] == "working" or (row["lastMin"] is not None and row["lastMin"] < BUSY_WITHIN_MIN)
        for row in crew_rows(snap, paths, "", now)
    )


def _shown_workers(
    snap: Snapshot, paths: frozenset[str], coords: dict[str, list[str]], now: float, within_hours: float
) -> list[tuple[dict, dict | None, str, float | None]]:
    """This project's workers that are live or moved recently, each with its
    agent row, its state and the minutes since it last moved."""
    out = []
    for worker in _ours(snap, paths, coords):
        term = snap.terminals.get(worker["terminal"])
        agent = snap.agents.get(term["pane"]) if term else None
        state = _worker_state(worker, agent)
        last = _last_min((agent["updatedAt"] if agent else None) or worker["observedAt"], now)
        if _recent(state, last, within_hours):
            out.append((worker, agent, state, last))
    return out


def runs_wanting_titles(
    snap: Snapshot,
    paths: frozenset[str],
    known: Mapping[str, str],
    now: float,
    within_hours: float = 12.0,
) -> list[str]:
    """Runs with a worker the page will show whose Task title nobody has yet."""
    wanted: list[str] = []
    coords = _coordinators(snap, paths)
    for worker, agent, _, _ in _shown_workers(snap, paths, coords, now, within_hours):
        if worker["task"] in known or (agent and agent["title"]) or not worker["run"]:
            continue
        if worker["run"] not in wanted:
            wanted.append(worker["run"])
    return wanted


def crew_rows(
    snap: Snapshot, paths: frozenset[str], repo: str, now: float, within_hours: float = 12.0
) -> list[dict]:
    """Coordinator rows, then workers, then other sessions, newest move first.

    `paths` is the repo and its git worktrees; `repo` is the main checkout,
    used only to say where a worker is when it is somewhere else.
    """
    coords = _coordinators(snap, paths)
    claimed: set[str] = set()
    rows: list[dict] = []

    for handle, run_ids in coords.items():
        term = snap.terminals[handle]
        agent = snap.agents.get(term["pane"])
        claimed.add(term["pane"])
        rows.append(
            {
                "id": handle,
                "who": "coordinator",
                "kind": "coordinator",
                "title": term["title"],
                "state": _agent_state(agent) or "unknown",
                "lastMin": _last_min(agent["updatedAt"] if agent else None, now),
                "runs": [snap.runs[r]["objective"] for r in run_ids if snap.runs[r]["objective"]],
            }
        )

    # A worker's pane is its own even when the row is too old to show: it must
    # not come back below as an unnamed session.
    for worker in _ours(snap, paths, coords):
        term = snap.terminals.get(worker["terminal"])
        if term and term["pane"]:
            claimed.add(term["pane"])

    workers = []
    for worker, agent, state, last in _shown_workers(snap, paths, coords, now, within_hours):
        title = snap.titles.get(worker["task"]) or (agent["title"] if agent else "")
        run = snap.runs.get(worker["run"], {})
        row = {
            "id": worker["dispatch"],
            "who": role_of(title) or "worker",
            "kind": "worker",
            "title": title,
            "state": state,
            "lastMin": last,
        }
        where = _where(worker["path"], repo)
        if where:
            row["where"] = where
        # A worker here for another project's coordinator: say whose it is.
        if run.get("coordinator") not in coords and run.get("objective"):
            row["for"] = run["objective"]
        workers.append(row)

    sessions = []
    for pane, agent in snap.agents.items():
        if pane in claimed or agent["parent"] or agent["path"] not in paths:
            continue
        state = _agent_state(agent) or "unknown"
        last = _last_min(agent["updatedAt"], now)
        if not _recent(state, last, within_hours):
            continue
        term = next((t for t in snap.terminals.values() if t["pane"] == pane), None)
        sessions.append(
            {
                "id": pane,
                "who": (term["title"] if term else "") or agent["name"] or "session",
                "kind": "session",
                "title": "",
                "state": state,
                "lastMin": last,
            }
        )

    def by_move(row: dict) -> float:
        return row["lastMin"] if row["lastMin"] is not None else float("inf")

    return rows + sorted(workers, key=by_move) + sorted(sessions, key=by_move)


# ── the background reader ─────────────────────────────────────────────


class Probe:
    """Keeps one recent Snapshot without ever making a request wait.

    `view(paths)` returns immediately. It also starts a refresh when one is
    due, so Orca is asked only while somebody has the page open.
    """

    def __init__(
        self,
        env: Mapping[str, str] = os.environ,
        runner: Callable[..., dict] = run_json,
        resolver: Callable[..., list[str] | None] = resolve_cli,
    ) -> None:
        self._env = env
        self._runner = runner
        self._resolver = resolver
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._titles: dict[str, str] = {}
        self._paths: frozenset[str] = frozenset()
        self._busy = False
        self._agent_wait = True  # False once this Orca has refused `--agent-wait`
        self._failure = ""  # "" | "missing" | "down": how the last attempt ended
        self._next_at = 0.0  # time.monotonic() of the next allowed refresh

    def view(self, paths: frozenset[str]) -> View:
        if not enabled(self._env):
            return View("off")
        with self._lock:
            self._paths = paths
            if not self._busy and time.monotonic() >= self._next_at:
                self._busy = True
                threading.Thread(target=self._refresh, name="orca-probe", daemon=True).start()
            snap, failure = self._snap, self._failure
        if snap is not None and time.time() - snap.taken <= STALE_AFTER_S:
            return View("ok", snap)
        # No fresh snapshot. Either Orca is failing, or nobody has asked for a
        # while (the page was closed) and the refresh started above has not
        # landed yet. Only the first is a reason to fall back; the second is
        # `checking`, and the page asks again in a moment.
        return View(failure or "checking")

    def _refresh(self) -> None:
        snap, failure, wait = None, "", RETRY_AFTER_FAILURE_S
        try:
            cli = self._resolver(self._env)
            if cli is None:
                failure, wait = "missing", RECHECK_MISSING_S
            else:
                snap = self._read(cli)
                paths = self._paths
                if not knows_project(snap, paths):
                    wait = REFRESH_UNKNOWN_S
                else:
                    wait = REFRESH_BUSY_S if busy(snap, paths, time.time()) else REFRESH_IDLE_S
        except Exception:  # noqa: BLE001 - a reader thread must never die loudly
            failure = "down"
        with self._lock:
            if snap is not None:
                self._snap = snap
            self._failure = failure
            self._next_at = time.monotonic() + wait
            self._busy = False

    def _read(self, cli: list[str]) -> Snapshot:
        env = cli_env(self._env)
        with ThreadPoolExecutor(max_workers=len(READ_ONLY)) as pool:
            jobs = {key: pool.submit(self._list, key, cli, env) for key in READ_ONLY}
            got = {key: job.result() for key, job in jobs.items()}
        snap = Snapshot(
            taken=time.time(),
            runs=parse_runs(got["runs"]),
            workers=parse_workers(got["workers"]),
            terminals=parse_terminals(got["terminals"]),
            agents=parse_agents(got["agents"]),
            titles=dict(self._titles),
        )
        if self._fetch_titles(cli, env, snap):
            snap = Snapshot(snap.taken, snap.runs, snap.workers, snap.terminals, snap.agents, dict(self._titles))
        return snap

    def _list(self, key: str, cli: list[str], env: Mapping[str, str]) -> dict:
        """One of the four listings. `agents` is asked with `--agent-wait`, and
        without it once an Orca has refused the flag."""
        if key != "agents":
            return self._runner(cli, READ_ONLY[key], env)
        if not self._agent_wait:
            return self._runner(cli, AGENTS_PLAIN, env)
        try:
            return self._runner(cli, READ_ONLY["agents"], env)
        except OrcaTimeout:
            raise  # a hung Orca is not an old Orca: do not wait a second time
        except OrcaError:
            result = self._runner(cli, AGENTS_PLAIN, env)  # raises too if Orca is down
            self._agent_wait = False
            return result

    def _fetch_titles(self, cli: list[str], env: Mapping[str, str], snap: Snapshot) -> bool:
        """Task titles for workers that have none. A failure here costs the
        title, not the snapshot: the row still shows, named `worker`."""
        changed = False
        wanted = runs_wanting_titles(snap, self._paths, self._titles, time.time())
        for run_id in wanted[:MAX_TITLE_RUNS]:
            if not RUN_ID.match(run_id):  # checked again where it becomes an argument
                continue
            try:
                found = parse_titles(self._runner(cli, (*TASK_TITLES, run_id), env))
            except OrcaError:
                continue
            # A Task the list does not name keeps an empty title, so its Run
            # is asked once and not again on every refresh.
            asked = {w["task"]: "" for w in snap.workers if w["run"] == run_id and w["task"]}
            self._titles = {**asked, **self._titles, **found}
            changed = True
        return changed
