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
import json
import os
import re
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
PORT_BASE, PORT_SPAN = 8800, 120

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
        rows.append(item)
        self.save_backlog(rows)
        return item

    def set_state(self, item_id: str, state: str) -> dict | None:
        rows = self.load_backlog()
        for row in rows:
            if row.get("id") == item_id:
                row["state"] = state
                self.save_backlog(rows)
                return row
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
        question, answer = str(question).strip(), str(answer).strip()
        if not question or not answer:
            raise ValueError("An answer needs both a question and a choice.")
        rows = self.load_answers()
        truncated = len(answer) > self.ANSWER_MAX_CHARS
        entry = {
            "answer": answer[: self.ANSWER_MAX_CHARS],
            "at": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        if truncated:
            entry["truncated"] = True
            entry["originalLength"] = len(answer)
            entry["truncatedAt"] = self.ANSWER_MAX_CHARS
        rows[question[: self.QUESTION_MAX_CHARS]] = entry
        tmp = self.answers.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.answers)
        return entry

    def split_decisions(self, narrative: dict) -> tuple[list, list]:
        """Anything the owner has answered leaves 'Needs you' immediately.

        The note distinguishes *they told me* from *I acted on it* — those are
        different states and collapsing them is what made the board lie.
        """
        recorded = self.load_answers()
        pending, answered = [], []
        for d in narrative.get("decisions", []):
            hit = recorded.get(d.get("q", ""))
            if hit:
                entry = {
                    "q": d["q"],
                    "chose": hit["answer"],
                    "note": "Recorded from the board. Not yet folded into my report.",
                    "when": hit["at"][11:16],
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
        return pending, answered + narrative.get("answered", [])

    # ── teammates, measured rather than reported ──────────────────────
    def team(self, within_hours: float = 12.0) -> list[dict]:
        """Who has been working, and when they last moved.

        Derived from agent transcript mtimes, not from anyone's self-report —
        an agent that acts touches its transcript, so this stays true even
        when a teammate goes idle without telling anyone. Only metadata and
        mtimes are read; the .jsonl bodies are never opened (they run to
        megabytes).
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
        lead_file = None
        for cfg in sorted((Path.home() / ".claude" / "teams").glob("*/config.json")):
            try:
                team = json.loads(cfg.read_text())
            except (OSError, ValueError):
                continue
            if not any(
                Path(m.get("cwd", "")) == self.repo for m in team.get("members", [])
            ):
                continue
            candidate = root / f"{team.get('leadSessionId')}.jsonl"
            if candidate.exists():
                lead_file = candidate
                break
        if lead_file is None:
            files = [t for t in root.glob("*.jsonl") if t.is_file()]
            lead_file = max(files, key=lambda t: t.stat().st_mtime, default=None)

        if lead_file is not None:
            try:
                age = (datetime.now().timestamp() - lead_file.stat().st_mtime) / 60
            except OSError:
                age = None
            if age is not None and age <= within_hours * 60:
                rows_lead = {
                    "who": "team-lead",
                    "kind": "lead",
                    "model": (self._lead_model(lead_file) or "inherited").replace(
                        "claude-", ""
                    ),
                    "lastMin": round(age, 1),
                }
                return [rows_lead] + sorted(
                    newest.values(), key=lambda r: r["lastMin"]
                )
        return sorted(newest.values(), key=lambda r: r["lastMin"])

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

        pending, answered = self.split_decisions(narrative)

        out = dict(narrative)
        out.pop("checks", None)
        out.update(
            {
                "project": self.name,
                "decisions": pending,
                "answered": answered,
                "pipeline": self.resolve_pipeline(narrative, up),
                "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
                "state": state,
                "held": held,
                "team": self.team(),
                "backlog": self.backlog_view(narrative),
                "freshness": {
                    "narrativeAgeMin": age_minutes(narrative.get("writtenAt")),
                    "checksAgeMin": age_minutes(checks.get("at")),
                    "dirtyFiles": dirty,
                    "commitsSinceNarrative": stale,
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


def make_handler(board: Board, token: str | None = None):
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
            super().end_headers()

        def do_GET(self):  # noqa: N802 - stdlib naming
            if not self.authorised():
                return self.deny()
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
            this mistake.
            """
            if not self.authorised():
                return self.deny()
            route = self.path.split("?")[0].rstrip("/")
            if route not in ("/backlog", "/answer"):
                return self.send_error(404)
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
                body, code = json.dumps({"ok": True, "item": item}).encode(), 201
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode()
                code = 400
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
