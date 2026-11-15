---
name: project-bootstrap
description: Scaffold how a project is run under Orca and oh-my-claudecode: the operating model, the worker roster, the workstream register and the worker brief. Use when starting a project in Orca, when `/init` runs in a repository with no `.claude/rules/operating-model.md`, or with `update` in a project that has the older team-roles set.
---

# Project bootstrap

## What it is for

Set up the files that say how a project is run under Orca, so that it
lives in the repository instead of in one session's memory: seven files,
about 300 lines a session reads at its start.

**On `/init` in a project with no `.claude/rules/operating-model.md`,
scaffold from `templates/` first, then document whatever code exists.** A
CLAUDE.md without the working practice around it is half the job.

## Before anything: is this an Orca project?

The scaffold names Orca's commands and oh-my-claudecode's agents. Check:

- `orca` is on `PATH`, or `ORCA_TERMINAL_HANDLE` is set in this session;
- oh-my-claudecode is installed: `claude plugin list` shows it. Do not
  look for a folder: the plugin cache path carries a version.

If either is missing, say so to the owner and **stop**: the scaffold would
name commands and agents that do not exist there.

## Ask the owner first

One message, and on the board as `decisions` where a choice has a
recommendation (see the `status-board` skill):

- what the project is, and who works on it;
- whether a stack exists yet (if not, the command table stays `_TBD_`);
- which workstreams to open beside `WS-00 Set-up`;
- whether the default roster in `operating-model.md` suits them (a tier
  per role; they may change a row);
- what releasing means here: it fills the comment at the top of
  `release-choreography.md`.

**No file is written before the owner answers.** Scaffolding a rule nobody
follows is worse than no rule.

## Copy from `templates/`, never from another project

`templates/` in this skill directory is the general source. Copy those
files, then adapt. **Do not lift files from an existing repository.**
Its rules are entangled with its stack and its incident history, and
porting them carries another project's past in as if it were this one's.
Read another repo for *form* if you like; take content only from
`templates/`.

## What to create

| In the project | For |
|---|---|
| `CLAUDE.md` | what every session needs first: status, how the project is run, definition of done |
| `.claude/rules/operating-model.md` | the coordinator, the worker roster, starting a worker, the Task title, worktrees, independent checks, decisions |
| `.claude/rules/verification.md` | how claims are checked, with the exact commands (and known-broken ones) |
| `.claude/rules/release-choreography.md` | commit/push/deploy sequencing |
| `docs/workstreams/README.md` | the register the board's Workstreams list copies |
| `.claude/skills/task-spec/` | the brief for an Orca worker |
| `.claude/skills/milestone-acceptance/` | the close-out of a milestone |

Add `<domain>.md` rules, with `paths:` globs, when a directory earns one.
Two stack-dependent pieces have no honest content before the stack
exists: **`architecture.md` is not created at all** (write it when there
are layers to describe), and **the command table in `verification.md`
ships as `_TBD_`** (fill it from commands you have actually run, and
record known-broken ones beside the working one). A stub says "unknown";
an invented build command says "verified" to every session that follows.

## Anything you add to `templates/` must survive four ecosystems

Before writing a line into `templates/`, ask whether it is still true
for a Go service, a Python package and a Rust crate. If not, it belongs
in the *project's* `.claude/rules/**`. The ecosystem-specific version is
the more useful one, which is why this regresses more than any other
rule here: state the principle in the template and let the project record
its own command. Watch for, in a template diff: a package-manager
invocation (`npm`, `pip`, `cargo`, `go`); a file of one ecosystem
(`package.json`, `pyproject.toml`, `Cargo.toml`); a path layout assumed
universal (`src/**`, `lib/`, `cmd/`); a platform-specific shell flag
(`stat -f` is BSD, `stat -c` is GNU); a test-runner, type-checker or
bundler; acceptance criteria only a UI can meet. Nothing of any project
goes into a template either: no name, no model ID (the roster names
tiers), no local path.

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

## After the scaffold, you coordinate

The session that ran the bootstrap is the coordinator from now on; the
scaffold looks like the deliverable, which is why this is where it stops
being followed. Say it once, plainly: that workers do the work and how the
next piece goes out. Write `WS-00` in the register, and open the
workstreams the owner named. **The next piece of work goes out as an Orca
Task with a `task-spec`**, titled `role | WS-id | subject`; not the one
after it. The discipline of the role is in the owner's global `agents.md`.

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
quoting it. Then write the first `narrative.json` under
`~/.claude/status-board/boards/<slug>/` — headline, the decisions the
bootstrap surfaced, and what was deliberately left blank; its
`workstreams` field is a copy of `docs/workstreams/README.md`. See the
`status-board` skill for the shape and the rules.

A bootstrap always ends with real decisions for the owner (stack,
domain, whether to `git init`). Those belong on the board as
`decisions` with a recommendation, not in a chat message.

## Update an older set

`/project-bootstrap update`, a step the owner runs: nothing rewrites a
repository unasked. A project made before the Orca-only scaffold has
`.claude/rules/team-roles.md` (written for Claude Code agent teams) and
no `operating-model.md`; the offer hook says so once per session.

1. **List the project's `.claude/` and sort it into three groups:**
   - *from the old templates, unchanged*: same name as an old template
     file and the same text. These are retired;
   - *from the old templates, edited*: same name, different text. These
     are kept and shown to the owner: they hold the project's own lessons;
   - *the project's own*: every other file. Never touched.
2. **Show the plan as a table on the board**: what it adds, what it
   deletes, what it leaves. The `decisions` card is "Apply the update?".
3. **On yes, write the new files.** Move `team-roles.md` to
   `.claude/rules/team-roles.old.md` rather than deleting it, so that its
   project-specific parts can be carried over by hand into
   `operating-model.md`. Delete the unchanged retired files (the six
   bench agents, `teammate-brief`), remove the agent-teams flag from
   `settings.json` (the file goes if nothing else is in it), and leave
   every change uncommitted.
4. **Say what was done**, and that committing is the owner's.

## Finish

Verify by opening a fresh session: confirm always-on rules appear,
confirm a gated rule stays absent until a matching file is read, and
start one read-only worker with the Task title convention, then see it on
the board's row. Then commit `.claude/` and `docs/workstreams/` — it is
team infrastructure, not personal configuration.

`settings.local.json` stays gitignored for per-machine overrides.
