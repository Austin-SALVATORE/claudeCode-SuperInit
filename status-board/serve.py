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

Binds 127.0.0.1 only. Read-only against the repo: it runs git in reporting
modes and never writes to the working tree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import zlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        """Owner-written items first, then whatever Claude parked, both aged."""
        out = []
        for row in self.load_backlog():
            if row.get("state") == "done":
                continue
            out.append({**row, "ageDays": age_days(row.get("createdAt"))})
        for row in narrative.get("open", []):
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

    def record_answer(self, question: str, answer: str) -> dict:
        question, answer = str(question).strip(), str(answer).strip()
        if not question or not answer:
            raise ValueError("An answer needs both a question and a choice.")
        rows = self.load_answers()
        entry = {
            "answer": answer[:500],
            "at": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        rows[question[:500]] = entry
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
                answered.append(
                    {
                        "q": d["q"],
                        "chose": hit["answer"],
                        "note": "Recorded from the board. Not yet folded into my report.",
                        "when": hit["at"][11:16],
                    }
                )
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
        return sorted(newest.values(), key=lambda r: r["lastMin"])

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


def make_handler(board: Board):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE), **kw)

        def do_GET(self):  # noqa: N802 - stdlib naming
            if self.path.split("?")[0].rstrip("/") in ("/report.json", "/report"):
                return self.send_report()
            return super().do_GET()

        def do_POST(self):  # noqa: N802 - stdlib naming
            """The only write path in the server, and it touches one file.

            Bound to 127.0.0.1 with no auth, which is fine for a single local
            reader — but it is why the body is capped and the payload is
            whitelisted field by field rather than merged wholesale.
            """
            route = self.path.split("?")[0].rstrip("/")
            if route not in ("/backlog", "/answer"):
                return self.send_error(404)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 8192:
                    raise ValueError("Body must be between 1 byte and 8 KB.")
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Per-project status board server.")
    ap.add_argument("--repo", default=".", help="path to the project repo")
    ap.add_argument("--port", type=int, help="override the derived port")
    ap.add_argument("--name", help="display name (defaults to the directory name)")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    board = Board(repo, args.name)
    port = args.port or port_for(repo)

    print(f"{board.name} board → http://127.0.0.1:{port}")
    print(f"  narrative: {board.narrative}")
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(board)).serve_forever()
