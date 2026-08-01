---
name: project-bootstrap
description: Scaffold a project's .claude/ directory — team roles, rules, subagents, and skills — so a new repository starts with the same working practice as an established one. Use when starting a project, when running /init on a repo with no .claude/ structure, or when an existing project has no .claude/ structure.
---

# Project bootstrap

Set up `.claude/` so the way a project is built lives in the
repository instead of in one session's memory.

**On `/init` in a project with no `.claude/`, scaffold from
`templates/` first, then document whatever code exists.** A CLAUDE.md
without the working practice around it is half the job.

## Copy from `templates/`, never from another project

`templates/` in this skill directory is the general source: rules,
agents, skills, `settings.json`, and a greenfield `CLAUDE.md`. Copy
those, then adapt.

**Do not lift files from an existing repository.** Its rules are
entangled with its stack and its incident history, and porting them
carries another project's past in as if it were this one's — which is
exactly the "a citation is a claim" failure the rules themselves warn
about. Read another repo for *form* if you like; take content only
from `templates/`.

Ask what the project is and who works on it before adapting anything.
Scaffolding a rule nobody follows is worse than no rule.

## The team model

One **team lead** (planning, review, verification, the task list) and
teammates for work that needs conversation. Work that needs only a
result is a **subagent**, not a teammate — subagents keep persistent
memory (`~/.claude/agent-memory/<name>/`), run in the background, and
cost far less.

The lead does not implement. That separation is the point: a reviewer
who writes the code has no independent view of it.

**Write the lead's dispatch discipline into `team-roles.md`, not just
"does not implement".** The rule that gets broken is the softer one:
the lead asks the right person the right question, gets the answer,
and then drafts the solution itself because it has the context and
drafting feels faster than briefing. Same collapse, one level up from
code — and harder to notice, because the work still ships. State it as
*the lead's output is a brief, not a draft*, and name the tells:
writing the approach into a reply instead of a brief, producing a
prompt for the human to paste instead of dispatching the teammate,
"I'll just sketch it and hand it over".

## What to create

```
.claude/
  rules/
    team-roles.md            # always loaded — who owns what, and what
                             # each role must never do
    verification.md          # how claims are checked, with the exact
                             # commands (and known-broken ones)
    release-choreography.md  # commit/push/deploy sequencing rules
    <domain>.md              # paths: gated to the directories it governs
  agents/
    claim-verifier.md        # verifies reports against the repo
    <domain-validator>.md    # validates the project's core artifact
  skills/
    teammate-brief/          # self-contained handoff briefs
    <ceremony>/              # the project's repeated procedures
```

Plus `CLAUDE.md` at the project root for conventions every session
needs. When it passes ~200 lines, split the overflow into `rules/`.

`templates/` covers everything above except the two stack-dependent
pieces, which have no honest content before the stack exists:

- **`architecture.md` is not created at all.** Write it when there are
  layers to describe.
- **The command table in `verification.md` ships as `_TBD_`.** Fill it
  from commands you have actually run, and record known-broken ones
  beside the working one — a check that exits 0 without checking
  anything is indistinguishable from success, so the whole team keeps
  reaching for it until it is written down.

Guessing either one is worse than leaving it blank: a stub says
"unknown", while an invented build command says "verified" to every
session that follows.

## Writing rules that work

- **Always-loaded rules cost context in every session.** Keep them
  short and universal. Anything file-specific gets `paths:` globs in
  frontmatter and loads only when a matching file is read.
- **Write the reason, not just the rule.** "Generated data and the
  code that reads it ship in one commit" is followed; "follow the
  commit conventions" is not. Rules earned from real incidents are
  the ones worth keeping — cite the incident in one clause.
- **Rules are guidance, not enforcement.** For behavior that must not
  be bypassed, use hooks or permissions instead.
- Prefer prohibitions that name the tempting shortcut: the rule that
  gets broken is the one that didn't anticipate pressure.

## Writing subagents that work

Frontmatter: `name`, `description` (say *when* to use it — that's the
routing signal), optional `tools` allowlist and `model`.

- Restrict `tools` to the job. An auditor with write access will
  eventually fix what it was asked to measure, destroying its own
  evidence.
- Say what the agent must **not** produce. "Report discrepancies,
  nothing else — no praise, no suggestions" keeps output usable.
- Omit `model` to inherit the session's, unless the task is clearly
  mechanical (cheaper) or clearly hard (stronger).

## Agent teams

Teams are experimental and off by default. Enable with
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"` under `env` in
`settings.json`.

**The flag must be present when the session starts.** Setting it
mid-session puts the variable in the environment but never creates
the team infrastructure, so spawn requests quietly become subagents
instead. Verify a real team formed by checking that
`~/.claude/teams/<team>/` exists — the agent panel looks identical
either way.

**Teammate vs subagent is decided by the `name` parameter on the spawn
(verified 27 Jul, Ghostty+tmux).** A spawn with only a `description`
becomes a headless background subagent — no pane, `members` stays at
1, and the agent self-reports "one-off subagent, no mailbox." A spawn
that passes a **`name`** registers as a real teammate: it joins the
team config's `members`, gets `backend: tmux` and its own pane, and is
addressable by that name over the mailbox. So: name it to get a paned
teammate; omit the name to get a headless helper. Confirm with
`tmux list-panes` (count grows) and the `members` array in
`~/.claude/teams/<team>/config.json`.

Context transfer works regardless of which you spawn: a fresh agent
with zero history answered repo-specific trap questions correctly,
sourced entirely to `.claude/rules/**`. The rules files are the
durable win; teammates-vs-subagents is just a spawn flag.

**The lead must PULL deliverables — they are not reliably pushed.**
Observed repeatedly: teammates coordinate laterally with each other
very well (they dedupe findings, hand off territory, and correct each
other unprompted), then go idle *without* sending their report to the
lead. An idle teammate is not a finished one, and `idleReason:
"available"` does not mean "done". Before concluding anything, check
the artifact — the commit, the file, the test run — rather than
waiting for a message that may never come; then message the teammate
by name to resume it (a send wakes it from its transcript with context
intact).

The worst version of this is a teammate that goes idle with
**uncommitted work in the tree**, which is the one state nobody can
verify or resume from. Tell teammates explicitly to commit at the
phase boundaries their own plan defines, and check `git status` when
one goes quiet.

## Take the lead role — the scaffold does not do this for you

Writing `team-roles.md` is not the same as operating by it. The session
that ran the bootstrap **is the team lead from that moment on**, and
the failure mode is finishing the scaffold and then carrying on exactly
as before — implementing directly, committing by hand — with a rules
file on disk that nobody is following. A rule nobody follows is worse
than no rule, and this is the most likely place for that to happen,
because the scaffold *looks* like the deliverable.

So, immediately after the scaffold:

- **Say it once, plainly** — that you are the lead, that you will not
  write product code, and how the next piece of work will be routed.
  The owner needs to be able to hold you to it.
- **Route the very next task through the table** in
  `team-roles.md § Routing work by size`. Not the one after it.
- **Stop touching `src/**` the moment it exists.** During bootstrap the
  lead legitimately writes everything, because everything is `.claude/`
  and `docs/`. That permission ends with the first line of product
  code, and the transition is silent — nothing will stop you.

The first real dispatch on a new project is almost always the
architect, once the stack and domain are settled. Those two are owner
decisions: surface them, do not assume them.

## Start the status board

Part of the bootstrap, not an optional extra. Every project reports on
its own board, so the owner glances at a page instead of reading prose:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/status-board/serve.py" --repo <abs repo path> --name "<Display Name>" &
```

The port is derived from the repo path, so a project always lands on
the same URL and two projects never collide. Backgrounded, the URL it
prints will not reach you — derive the port from the server's own PID
with `lsof -p <pid>`, never by process name, and `curl` it before
quoting it. Then write the first
`narrative.json` under
`~/.claude/status-board/boards/<slug>/` — headline, the decisions the
bootstrap surfaced, and what was deliberately left blank. See the
`status-board` skill for the shape and the rules.

A bootstrap always ends with real decisions for the owner (stack,
domain, whether to `git init`, whether to restart for the agent-teams
flag). Those belong on the board as `decisions` with a recommendation,
not in a chat message.

## Finish

Verify by opening a fresh session: confirm always-on rules appear,
confirm a gated rule stays absent until a matching file is read, and
invoke one subagent by name. Then commit `.claude/` — it is team
infrastructure, not personal configuration.

`settings.local.json` stays gitignored for per-machine overrides.
