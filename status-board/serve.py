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

Binds 127.0.0.1 by default, with no auth — right for a single local reader.
`--host 0.0.0.0` makes it reachable from a phone on the same network and
requires a token, printed once at startup and stored in the state directory.
The token is not real security: it travels in a URL over plaintext HTTP. It
stops casual access from other devices on a shared network and makes the
exposure deliberate rather than a side effect of a flag.

Read-only against the repo: it runs git in reporting modes and never writes
to the working tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Sibling module in this same directory, not a standalone script invoked
# from elsewhere — unlike the hooks/ scripts, which duplicate this file's
# helpers rather than import them because they run from a different
# working directory. `serve.py`'s own directory is on `sys.path[0]`
# automatically, so this import needs nothing extra.
import tree_lock

# Two more siblings, same reasoning. `orca_crew` reads who is working from
# Orca when the project is run there; `workstreams` shapes the workstream
# list. Importing either starts nothing: hooks/revive-board.py imports this
# file only for `slug_for` and `port_for`.
import orca_crew
import workstreams

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


def slug_for(repo: Path) -> str:
    return repo.name.lower().replace(" ", "-")


def port_for(repo: Path) -> int:
    # crc32, not hash(): hash() is salted per process and would move the port
    # on every restart. The whole point is that the URL is stable.
    return PORT_BASE + zlib.crc32(str(repo).encode()) % PORT_SPAN


class Board:
    def __init__(self, repo: Path, name: str | None):
        self.repo = repo
        self.slug = slug_for(repo)
        self.name = name or repo.name
        self.dir = STATE / "boards" / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)
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
            raise ValueError("An item needs a title.")
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

    def record_answer(self, question: str, answer: str) -> dict:
        if question is None or answer is None:
            raise ValueError("An answer needs both a question and a choice.")
        question, answer = str(question).strip(), str(answer).strip()
        if not question or not answer:
            raise ValueError("An answer needs both a question and a choice.")
        truncated = len(answer) > self.ANSWER_MAX_CHARS
        entry = {
            "answer": answer[: self.ANSWER_MAX_CHARS],
            "at": datetime.now(timezone.utc).astimezone().isoformat(),
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
        # Read-only here — this row exists so the owner can see who holds the
        # tree without asking, the same argument as every other row in this
        # dict. `hooks/tree-lock-guard.py` is the actual enforcement; this
        # never writes the lock, only reports it. `"none"` (not "free")
        # deliberately reuses tone()'s existing green-if-none rule on the
        # client, rather than adding a client-side case for a second word
        # that means the same thing.
        lock = tree_lock.status(self.repo)
        if lock.get("held"):
            mins = lock.get("ageMin")
            age = f"{mins:.0f}m" if isinstance(mins, (int, float)) else "?"
            state["lock"] = f"{lock.get('holder', '?')} ({age})"
        else:
            state["lock"] = "none"
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
# (`write_refusal`). The page's own requests pass all of them unchanged, and
# so does a plain client such as curl.
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
    3. `Origin`, or failing that `Referer`, must be this board's own origin:
       the scheme and the `Host` the request itself was addressed to.
    4. The check every request gets, `host_refusal`: without it a page can
       point a name of its own at 127.0.0.1 and then pass check 3, because it
       is asking "itself".
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
    referer = headers.get("Referer")
    if origin is not None:
        if _origin_of(origin) != own:
            return 403, "The board records only requests made by its own page."
    elif referer is not None and _origin_of(referer) != own:
        return 403, "The board records only requests made by its own page."
    return host_refusal(headers, from_loopback, names)


def make_handler(board: Board, token: str | None = None):
    names = allowed_hosts()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def authorised(self) -> bool:
            """True when no token is configured, or the request carries it.

            Accepted from ?k= on the URL or from the cookie set on first use,
            because the page's own fetches are relative and would otherwise
            lose the query string. Compared with compare_digest: the timing
            leak is not a realistic threat on a LAN, but writing `==` here is
            the kind of thing that gets copied into somewhere it matters.
            """
            if not token:
                return True
            # A request arriving on loopback came from this machine, which is
            # the trust boundary the token was defending — and anything running
            # as this user can read the token file anyway, so demanding it here
            # buys nothing and breaks every existing localhost tab. Enabling
            # --host must not change how the board behaves for the person
            # sitting at the machine.
            if self.client_address and self.client_address[0] in (
                "127.0.0.1",
                "::1",
                "::ffff:127.0.0.1",
            ):
                return True
            from http.cookies import SimpleCookie
            from urllib.parse import parse_qs, urlparse

            supplied = parse_qs(urlparse(self.path).query).get("k", [None])[0]
            if not supplied:
                raw = self.headers.get("Cookie")
                if raw:
                    try:
                        supplied = SimpleCookie(raw).get("board_k").value
                    except Exception:
                        supplied = None
            import hmac

            return bool(supplied) and hmac.compare_digest(supplied, token)

        def deny(self):
            body = b"Not authorised. Open the URL printed by the server, including ?k=."
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            if getattr(self, "_set_cookie", False):
                self._set_cookie = False
                self.send_header(
                    "Set-Cookie",
                    f"board_k={token}; Path=/; SameSite=Strict; Max-Age=604800",
                )
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

        def do_HEAD(self):  # noqa: N802 - stdlib naming
            if not self.authorised():
                return self.deny()
            if self.refused_by_name():
                return None
            return super().do_HEAD()

        def do_GET(self):  # noqa: N802 - stdlib naming
            if not self.authorised():
                return self.deny()
            # Every read, not only report.json: the page, the report and the
            # shots all answer to the same names (see host_refusal).
            if self.refused_by_name():
                return None
            # Persist the token so the page's relative fetches, which carry no
            # query string, keep working after the first load.
            if token and "k=" in (self.path or ""):
                self._set_cookie = True
            route = self.path.split("?")[0].rstrip("/")
            if route in ("/report.json", "/report"):
                return self.send_report()
            if route.startswith("/shot/"):
                return self.send_shot(route[len("/shot/") :])
            return super().do_GET()

        def do_POST(self):  # noqa: N802 - stdlib naming
            """The only write path in the server, and it touches one file.

            Loopback has no auth, which is fine for a single local reader — and
            it is why the body is capped and the payload is whitelisted field by
            field rather than merged wholesale. Bound beyond loopback a token is
            required, checked here as well as on GET: a write path left open
            because only the read path was guarded is the classic version of
            this mistake. A local reader is not the same thing as any page the
            local browser has open, so the request must also come from the
            board's own page or from a plain client: see `write_refusal`.
            """
            if not self.authorised():
                return self.deny()
            route = self.path.split("?")[0].rstrip("/")
            if route not in ("/backlog", "/answer"):
                return self.send_error(404)
            refusal = write_refusal(self.headers, self.from_this_machine(), names)
            if refusal is not None:
                return self.send_json(refusal[0], {"ok": False, "error": refusal[1]})
            try:
                length = int(self.headers.get("Content-Length", 0))
                # board.MAX_BODY_BYTES, not a literal here: this gate exists
                # to stop something pathological, not to be the board's real
                # content limit — that limit is board.ANSWER_MAX_CHARS, and
                # this number is derived from it precisely so it cannot
                # become the binding one again by accident.
                if length <= 0 or length > board.MAX_BODY_BYTES:
                    raise ValueError(
                        f"Body must be between 1 byte and {board.MAX_BODY_BYTES} bytes."
                    )
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object.")
                if route == "/answer":
                    item = board.record_answer(payload.get("q"), payload.get("answer"))
                elif payload.get("id"):
                    item = board.set_state(payload["id"], payload.get("state", "done"))
                    if item is None:
                        raise ValueError(f"No backlog item with id {payload['id']}.")
                else:
                    item = board.add_item(payload)
                code, answer = 201, {"ok": True, "item": item}
            except Exception as exc:
                code, answer = 400, {"ok": False, "error": str(exc)}
            self.send_json(code, answer)

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

        def send_shot(self, name: str):
            name = unquote(name)
            root = (board.dir / "shots").resolve()
            suffix = Path(name).suffix.lower()
            if (
                not self.SHOT_NAME.match(name)
                or name.startswith(".")
                or suffix not in self.SHOT_TYPES
            ):
                return self.send_error(404, "no such shot")
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
                body = json.dumps(board.report(), ensure_ascii=False).encode()
                code = 200
            except Exception as exc:  # say what broke; never go blank
                body = json.dumps(
                    {
                        "project": board.name,
                        "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
                        "headline": "The board cannot read its own report",
                        "error": f"{type(exc).__name__}: {exc}",
                        "state": {},
                    }
                ).encode()
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # a chatty board is noise in the terminal it was started from

    return Handler


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


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

    repo = Path(args.repo).expanduser().resolve()
    board = Board(repo, args.name)
    port = args.port or port_for(repo)
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
        token_file = STATE / "lan-token"
        try:
            token = token_file.read_text().strip()
        except Exception:
            token = None
        if not token:
            import secrets

            token = secrets.token_urlsafe(16)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token)
            try:
                token_file.chmod(0o600)
            except Exception:
                pass

    handler = make_handler(board, token)
    shown = args.host if args.host not in LOOPBACK else "127.0.0.1"
    # Line-buffer stdout: this process is usually backgrounded into a log, and
    # Python block-buffers to a pipe, so the startup banner — which is the only
    # place the URL and the LAN token appear — would sit unflushed until the
    # buffer filled. A token you cannot read is a token you cannot use.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if token:
        ip = lan_ip() if args.host == "0.0.0.0" else args.host
        print(f"{board.name} board → http://{ip}:{port}/?k={token}")
        print("  ! bound beyond loopback. Anything on this network can reach it.")
        print("  ! plaintext HTTP, and the token is in the URL. Do not use on")
        print("    a network you do not control.")
        print(f"  token stored at: {STATE / 'lan-token'}")
    else:
        print(f"{board.name} board → http://{shown}:{port}")
    print(f"  narrative: {board.narrative}", flush=True)
    ThreadingHTTPServer((args.host, port), handler).serve_forever()
