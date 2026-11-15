#!/usr/bin/env python3
"""The Orca-only scaffold of project-bootstrap (release 2.2.0, 15 Nov 2026).

    python3 test_bootstrap_templates.py --plugin <checkout of the plugin>

What a bootstrapped project holds is seven files (BOOTSTRAP-ORCA-PLAN.md, section 1): `CLAUDE.md`, the rules
`operating-model.md`, `verification.md` and `release-choreography.md`, the workstream register
`docs/workstreams/README.md`, and the skills `task-spec` and `milestone-acceptance`. These tests read the
files of the plugin under test (skills/project-bootstrap/ and README.md) and nothing else: no hook, no server, no
network, no write outside a temporary folder, so there is no port to keep clear of. The sections of the plan they
follow are named in each docstring (the plan's step 2: the templates; step 3: the skill; step 6: the README).

Run it against the code before the change and the tests of steps 2, 3 and 6 fail, each for its own reason.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
SKILL_DIR = PLUGIN / "skills" / "project-bootstrap"
TEMPLATES = SKILL_DIR / "templates"
SKILL = SKILL_DIR / "SKILL.md"
README = PLUGIN / "README.md"
sys.dont_write_bytecode = True
TMP = Path(tempfile.mkdtemp(prefix="bootstrap-templates-")).resolve()  # nothing is written; kept for the form of the suites

# The seven files, as paths under templates/ and as the project that is scaffolded has them.
SEVEN = {
    "CLAUDE.md": "CLAUDE.md",
    "rules/operating-model.md": ".claude/rules/operating-model.md",
    "rules/verification.md": ".claude/rules/verification.md",
    "rules/release-choreography.md": ".claude/rules/release-choreography.md",
    "docs/workstreams/README.md": "docs/workstreams/README.md",
    "skills/task-spec/SKILL.md": ".claude/skills/task-spec/SKILL.md",
    "skills/milestone-acceptance/SKILL.md": ".claude/skills/milestone-acceptance/SKILL.md",
}
# The names of the agent files oh-my-claudecode 5.8.6 ships (its agents/ folder, names only), as the fixture of the roster.
OMC_AGENTS = {
    "analyst", "architect", "code-reviewer", "code-simplifier", "critic", "debugger", "designer", "document-specialist",
    "executor", "explore", "git-master", "planner", "qa-tester", "scientist", "security-reviewer", "test-engineer",
    "tracer", "verifier", "writer",
}
TIERS = {"Opus", "Sonnet", "Haiku", "Fable"}
# The old set: the six bench agents and the files that go with them.
BENCH = ("architect", "dev", "qa", "claim-verifier", "git-op", "repo-inventory")
OPERATING_MODEL_SECTIONS = [
    "the coordinator", "the roster", "starting a worker", "the task title", "worktrees",
    "when an independent check is required", "decisions",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def template_files() -> list[str]:
    if not TEMPLATES.is_dir():
        return []
    return sorted(str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*") if p.is_file())


def template_text(rel: str) -> str:
    return read(TEMPLATES / rel)


def headings(text: str, level: int = 2) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(rf"^{'#' * level} (.+)$", text, re.M)]


def section(text: str, title_start: str, level: int = 2) -> str:
    """The body of the `##` section whose title starts with `title_start` (case-insensitive), up to the next one."""
    marks = [(m.start(), m.group(1).strip()) for m in re.finditer(rf"^{'#' * level} (.+)$", text, re.M)]
    for i, (pos, title) in enumerate(marks):
        if title.lower().startswith(title_start.lower()):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            return text[pos:end]
    raise AssertionError(f"no '{'#' * level} {title_start}…' section; the headings are {[t for _, t in marks]}")


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def all_text_of_the_scaffold() -> dict:
    out = {rel: template_text(rel) for rel in template_files()}
    out["SKILL.md"] = read(SKILL)
    return out


# ── step 2: the templates ───────────────────────────────────────────────────────────────────────────────────────────
def test_templates_are_exactly_the_seven():
    """Plan section 1, step 2: no bench agent, no team-roles.md, no settings.json, no teammate-brief; the three new files."""
    found = template_files()
    assert found == sorted(SEVEN), f"templates/ holds {found}, expected {sorted(SEVEN)}"


def test_claude_md_points_only_at_files_that_exist():
    """Every `.claude/…` and `docs/…` path CLAUDE.md names is a template. `.claude/rules/architecture.md` is the one file
    that is deliberately not created (the plan, section 1: the skill says so), and CLAUDE.md says it does not exist yet."""
    text = template_text("CLAUDE.md")
    exempt = {".claude/rules/architecture.md"}
    named = set(re.findall(r"`((?:\.claude|docs)/[A-Za-z0-9_./-]+)`", text))
    assert named, "CLAUDE.md names no file at all"
    shipped = set(SEVEN.values())
    missing = sorted(p for p in named if p not in shipped and p not in exempt)
    assert not missing, f"CLAUDE.md points at files the scaffold does not make: {missing}"
    for rule in (".claude/rules/operating-model.md", ".claude/rules/verification.md", ".claude/rules/release-choreography.md"):
        assert rule in named, f"CLAUDE.md does not point at {rule}"
    assert "docs/workstreams/README.md" in text, "CLAUDE.md names no workstream register"
    assert "task-spec" in text and "milestone-acceptance" in text, "CLAUDE.md names neither skill"


def test_claude_md_is_short_and_keeps_status_and_definition_of_done():
    text = template_text("CLAUDE.md")
    lines = text.count("\n") + 1
    assert lines <= 50, f"CLAUDE.md is {lines} lines (the plan: about 40)"
    heads = [h.lower() for h in headings(text)]
    assert heads[:3] == ["status", "how this project is run", "definition of done"], heads
    assert "add the criteria this project actually has" in text.lower(), "the 'add your own criteria' comment is gone"


def test_no_template_names_teams_flag_teammate_mailbox_or_tree_lock():
    forbidden = ["AGENT_TEAMS", "~/.claude/teams", "teammate", "mailbox", "tree-lock", "tree_lock", "tree lock"]
    hits = []
    for rel in template_files():  # templates/ only: the skill's update section names the old set on purpose
        text = template_text(rel)
        for word in forbidden:
            if word.lower() in text.lower():
                hits.append((rel, word))
    assert not hits, f"retired words in the scaffold: {hits}"


def test_no_template_names_a_retired_bench_agent_or_file():
    hits = []
    for rel, text in {r: template_text(r) for r in template_files()}.items():
        for word in ("claim-verifier", "git-op", "repo-inventory", "team-roles", "teammate-brief", ".claude/agents", "settings.json"):
            if word in text:
                hits.append((rel, word))
    assert not hits, f"the old set is still named in templates/: {hits}"


ECOSYSTEM = [
    r"\b(?:npm|npx|yarn|pnpm|pip3?|cargo|poetry|mvn|gradle)\s+[a-z]",
    r"\bgo\s+(?:test|build|run|vet|mod)\b",
    r"\b(?:package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt|pom\.xml|build\.gradle)\b",
    r"\bstat\s+-[fc]\b",
    r"\b(?:pytest|jest|vitest|mypy|tsc|webpack|eslint|ruff|rustc|clippy)\b",
    r"\bsrc/\*\*",
    r"(?<![A-Za-z.])(?:lib|cmd)/",
]


def test_no_template_carries_an_ecosystem_command():
    """The list of the skill's own rule ('Anything you add to templates/ must survive four ecosystems')."""
    hits = []
    for rel in template_files():
        text = template_text(rel)
        for pattern in ECOSYSTEM:
            for m in re.finditer(pattern, text):
                if pattern.startswith(r"\bstat") and "not portable" in text:
                    continue  # verification.md (kept as it was) shows both flavours of `stat`, beside the warning that it is not portable
                hits.append((rel, m.group(0)))
    assert not hits, f"an ecosystem-specific command or path in a template: {hits[:8]}"


def test_operating_model_has_its_seven_sections():
    text = template_text("rules/operating-model.md")
    found = [h.lower() for h in headings(text)]
    assert len(found) == 7, f"{len(found)} sections: {found}"
    for want, got in zip(OPERATING_MODEL_SECTIONS, found):
        assert got.startswith(want), f"expected a section '{want}', found '{got}' (all: {found})"
    lines = text.count("\n") + 1
    assert 50 <= lines <= 110, f"operating-model.md is {lines} lines (the plan: about 80)"
    assert "one writing worker per worktree" in text.lower(), "the worktree rule (which replaces the tree lock) is not stated"
    assert "never revert another worker" in text.lower(), "the third line of the worktree rule is missing"
    assert "role | WS-" in text.replace("`", "") or "role | ws-" in text.lower().replace("`", ""), "the Task title form is not shown"
    for word in ("worker-start", "--model", "--effort", "--task-title", "worker_done"):
        assert word in text, f"operating-model.md never mentions {word}"
    assert "agents.md" in text, "it must point at the owner's global agents.md for the lead's discipline, not repeat it"


def test_roster_names_only_agents_omc_ships():
    text = template_text("rules/operating-model.md")
    rows = table_rows(section(text, "The roster"))
    header, body = [c.lower() for c in rows[0]], rows[1:]
    assert header[:4] == ["role", "agent", "model tier", "effort"], header
    assert len(body) >= 7, f"only {len(body)} rows in the roster"
    seen = set()
    for role, agent, tier, effort, *_ in body:
        names = re.findall(r"`([^`]+)`", agent)
        if role.lower().startswith("coordinator"):
            assert not names and "session" in agent.lower(), f"the coordinator is the session itself: {agent!r}"
        else:
            assert names, f"row '{role}' names no agent: {agent!r}"
        for name in names:
            bare = name.split(":")[-1]  # oh-my-claudecode:architect
            assert bare in OMC_AGENTS, f"'{name}' is not an agent oh-my-claudecode ships ({sorted(OMC_AGENTS)})"
            seen.add(bare)
        assert tier in TIERS, f"row '{role}': tier {tier!r} is not one of {sorted(TIERS)}"
        assert effort.strip("` ").lower() == "high", f"row '{role}': effort {effort!r}"
    for must in ("architect", "designer", "executor", "qa-tester", "code-reviewer", "verifier"):
        assert must in seen, f"the roster has no row for {must}"


def test_no_model_id_or_local_path_in_templates():
    """The skill's own rule: tiers, not model IDs; nothing of this machine (a path, a home folder, a handle)."""
    bad = [r"claude-(?:opus|sonnet|haiku|fable)-\d", r"\b(?:opus|sonnet|haiku|fable)[ -]?\d", r"/Users/", r"/home/", r"/private/",
           r"[A-Z]:\\", r"\bterm_[0-9a-f-]{8,}", r"\bctx_[0-9a-f]{6,}", r"\brun_[0-9a-f]{6,}", r"\btask_[0-9a-f]{8,}"]
    hits = []
    for rel, text in all_text_of_the_scaffold().items():
        for pattern in bad:
            for m in re.finditer(pattern, text, re.I):
                hits.append((rel, m.group(0)))
    assert not hits, f"a model id, a path or an id in the scaffold: {hits[:8]}"


def test_workstreams_register_has_the_status_words_one_row_and_the_record_outline():
    text = template_text("docs/workstreams/README.md")
    lines = text.count("\n") + 1
    assert 20 <= lines <= 55, f"{lines} lines (the plan: about 35)"
    low = text.lower()
    for word in ("planned", "active", "blocked", "done"):
        assert word in low, f"status word '{word}' missing"
    rows = table_rows(text)
    assert rows and [c.lower() for c in rows[0]][:5] == ["id", "name", "status", "next step", "record"], rows[:1]
    assert len(rows) == 2 and rows[1][0] == "WS-00" and rows[1][1].lower().startswith("set-up"), f"the register ships with one row, WS-00 Set-up: {rows[1:]}"
    assert "WS-NN-" in text, "no outline of a record file (WS-NN-<name>.md)"
    for heading in ("goal", "decisions", "open", "done", "links"):
        assert re.search(rf"^#+ {heading}\b", low, re.M) or re.search(rf"\b{heading}\b", low), f"the record outline lacks '{heading}'"
    assert "how to add a workstream" in low
    assert "board" in low, "it must say that the board's workstreams list is a copy of this register"


def test_task_spec_skill_has_its_parts_and_no_mailbox():
    text = template_text("skills/task-spec/SKILL.md")
    assert text.startswith("---\nname: task-spec\n"), text[:60]
    assert re.search(r"^description: .*(?:worker|Orca)", text, re.M), "the description must say it briefs an Orca worker"
    assert text.count("\n") + 1 <= 60, "task-spec is about 40 lines"
    low = text.lower()
    for part in ("ask", "authority", "limits", "worktree", "evidence", "out of scope", "worker_done"):
        assert part in low, f"task-spec lacks '{part}'"
    assert "read only" in low or "read-only" in low, "it must say a reader's spec says 'read only'"
    assert "plan approval" not in low and "mailbox" not in low


def test_milestone_acceptance_and_release_choreography_say_worker_not_teammate():
    accept = template_text("skills/milestone-acceptance/SKILL.md")
    assert "verifier" in accept and "worker" in accept, "milestone-acceptance must start a verifier worker"
    assert "claim-verifier" not in accept and "teammate" not in accept.lower()
    assert "exploratory acceptance pass" in accept.lower(), "the exploratory pass is the part that is kept"
    release = template_text("rules/release-choreography.md")
    assert "another worker may be writing in the same worktree" in " ".join(release.split()).lower()
    assert "teammate" not in release.lower()
    assert release.count("\n") + 1 <= 30


def test_verification_rule_is_kept_as_it_was():
    text = template_text("rules/verification.md")
    assert text.count("\n") + 1 >= 130, "verification.md was cut"
    assert "_TBD_" in text, "its command table ships as _TBD_"


# ── step 3: the skill ───────────────────────────────────────────────────────────────────────────────────────────────
def test_skill_mentions_every_template_file():
    text = read(SKILL)
    missing = [project_path for project_path in SEVEN.values() if project_path not in text and project_path.rsplit("/SKILL.md")[0] not in text]
    assert not missing, f"SKILL.md never names {missing}"


def test_skill_has_no_agent_teams_section():
    heads = [h.lower() for h in headings(read(SKILL))]
    for gone in ("the team model", "agent teams", "writing subagents", "take the lead role"):
        assert not any(h.startswith(gone) for h in heads), f"the section '{gone}' is still there: {heads}"
    text = read(SKILL)
    update = section(text, "Update an older set") if any(h.lower().startswith("update an older set") for h in headings(text)) else ""
    outside = text.replace(update, "")  # the update section names the teams flag it removes from an old project
    for word in ("AGENT_TEAMS", "~/.claude/teams", "mailbox", "tree-lock", "teammate"):
        assert word not in outside, f"SKILL.md still says {word} outside the update section"


def test_skill_asks_before_writing():
    text = read(SKILL)
    heads = [h.lower() for h in headings(text)]
    ask = next((i for i, h in enumerate(heads) if h.startswith("ask the owner")), None)
    create = next((i for i, h in enumerate(heads) if h.startswith("what to create")), None)
    assert ask is not None and create is not None, f"headings: {heads}"
    assert ask < create, "the questions must come before 'What to create'"
    asked = section(text, "Ask the owner").lower()
    for question in ("what the project is", "stack", "workstreams", "roster", "releas"):
        assert question in asked, f"the questions do not cover '{question}'"
    assert "no file is written" in asked or "nothing is written" in asked, "it must say that no file is written before the answer"


def test_skill_checks_for_orca_and_omc_before_anything():
    text = read(SKILL)
    heads = [h.lower() for h in headings(text)]
    first = next((i for i, h in enumerate(heads) if h.startswith("before anything")), None)
    ask = next((i for i, h in enumerate(heads) if h.startswith("ask the owner")), None)
    assert first is not None and ask is not None and first < ask, heads
    body = section(text, "Before anything")
    for word in ("ORCA_TERMINAL_HANDLE", "orca", "oh-my-claudecode", "stop"):
        assert word.lower() in body.lower(), f"'Before anything' lacks {word}"


def test_skill_description_and_outline_are_the_orca_ones():
    text = read(SKILL)
    m = re.search(r"^description: (.+)$", text, re.M)
    assert m, "no description"
    desc = m.group(1)
    for word in ("Orca", "oh-my-claudecode", "operating model", "worker roster", "workstream register", "worker brief", "update"):
        assert word in desc, f"description lacks '{word}': {desc}"
    assert "team roles" not in desc.lower() and "subagents" not in desc.lower()
    heads = [h.lower() for h in headings(text)]
    for want in ("what it is for", "before anything", "ask the owner", "copy from", "what to create", "survive four ecosystems", "writing rules",
                 "after the scaffold", "start the status board", "update an older set", "finish"):
        assert any(h.startswith(want) or want in h for h in heads), f"no section '{want}': {heads}"
    lines = text.count("\n") + 1
    assert 110 <= lines <= 175, f"SKILL.md is {lines} lines (the plan: about 150)"


def test_skill_update_section_is_a_step_the_owner_runs_and_rewrites_nothing_unasked():
    body = section(read(SKILL), "Update an older set")
    for word in ("/project-bootstrap update", "team-roles.old.md", "unchanged", "edited", "the project's own", "Apply the update?", "uncommitted"):
        assert word in body, f"the update section lacks '{word}'"
    low = body.lower()
    assert "committing is the owner's" in low or "the owner commits" in low or "owner's" in low
    assert "never" in low or "nothing rewrites" in low, "it must say that nothing is rewritten unasked"


def test_skill_after_the_scaffold_points_to_global_agents_md_and_the_register():
    body = section(read(SKILL), "After the scaffold")
    for word in ("agents.md", "WS-00", "task-spec", "Task"):
        assert word in body, f"'After the scaffold' lacks {word}"
    assert body.count("\n") <= 16, "it replaces about 25 lines in 8"


# ── step 6: the words ───────────────────────────────────────────────────────────────────────────────────────────────
def test_readme_names_no_retired_file():
    text = read(README)
    forbidden = ["team-roles", "tree-lock", "tree_lock", "AGENT_TEAMS", "claim-verifier", "git-op", "repo-inventory",
                 "agents/architect", "agents/dev", "agents/qa", ".claude/agents", "teammate-brief", "bench"]
    hits = [w for w in forbidden if w in text]
    assert not hits, f"README.md still names {hits}"
    assert not re.search(r"agent[- ]teams?\b", text, re.I), "README.md still speaks of agent teams"
    assert "subagent bench" not in text.lower()


def test_readme_describes_the_orca_only_scaffold():
    text = read(README)
    for word in ("Orca", "oh-my-claudecode", "operating-model.md", "docs/workstreams/README.md", "task-spec", "milestone-acceptance",
                 "/project-bootstrap update", "PROJECT_INIT_NO_OFFER", ".claude/no-bootstrap-offer"):
        assert word in text, f"README.md never says {word}"
    top = text.split("## Install")[0]
    assert "operating model" in top.lower() or "operating-model" in top, "the first list must describe the new scaffold"
    assert "role definitions" not in top and "subagent" not in top.lower(), "the first list still describes the old one"


def test_readme_offer_section_says_inside_orca_only_and_the_silences():
    body = section(read(README), "At session start", level=2)
    offer = body[body.index("### The bootstrap is offered"):body.index("### A dead board comes back")]
    low = offer.lower()
    assert "inside orca" in low, "the offer must say it is made only inside Orca"
    for word in ("git", "operating-model.md", "main checkout", "worktree", "worker", "startup", "clear", "update"):
        assert word in low, f"the offer section lacks '{word}'"
    assert "never edits a repository" in low or "does not edit" in low or "never rewrites" in low, "it must say the hook edits nothing"


def test_readme_lock_paragraph_is_the_worktree_rule():
    text = read(README)
    body = section(text, "At session start", level=2)
    orca = body[body.index("### Under Orca"):]  # to the end of the 'At session start' section
    low = " ".join(orca.split()).lower()
    assert "one writing worker per worktree" in low,"the paragraph that described the tree lock must say the worktree rule"
    assert "lock" not in low, "no lock is left to describe"


def test_readme_scaffold_section_describes_roles_under_orca_not_teams():
    text = read(README)
    body = section(text, "What the scaffold actually asserts")
    low = body.lower()
    for word in ("coordinator", "worker", "independent", "different"):
        assert word in low, f"the section lacks '{word}'"
    assert "team lead" not in low and "the dev " not in low and "architect plans and cannot implement" not in low


def test_readme_requirements_do_not_ask_for_the_teams_flag():
    body = section(read(README), "Requirements")
    assert "EXPERIMENTAL" not in body and "settings.json" not in body.replace("~/.claude/settings.json", ""), body
    assert "Python 3" in body
    assert "Orca" in body and "oh-my-claudecode" in body, "the scaffold needs Orca and oh-my-claudecode: say so"


def test_the_readme_names_no_model_id_or_local_path():
    text = read(README)
    for pattern in (r"claude-(?:opus|sonnet|haiku|fable)-\d", r"\b(?:opus|sonnet|haiku|fable)[ -]?\d", r"/Users/", r"/home/", r"/private/", r"\bterm_[0-9a-f-]{8,}"):
        assert not re.search(pattern, text, re.I), f"README.md matches {pattern}"


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
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed} passed, {failed} failed, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
