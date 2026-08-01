# claudeCode-SuperInit

A Claude Code plugin that gives a new repository a working practice on
day one, instead of letting it acquire one by accident.

Two skills:

- **`project-bootstrap`** — scaffolds `.claude/`: role definitions for a
  team lead, architect, dev and QA; a subagent bench; verification and
  release rules; and handoff skills.
- **`status-board`** — a local web page per project that reports status,
  decisions and findings, so structured work stops being buried in chat
  scrollback.

## Install

```
/plugin marketplace add Austin-SALVATORE/claudeCode-SuperInit
/plugin install project-init@claudecode-superinit
```

Then, in any project:

```
/project-bootstrap
```

Running `/init` on a repository with no `.claude/` triggers the same
thing.

## What the scaffold actually asserts

The point is not the file tree — it is the separation:

**Design, implementation, and verification are held by different roles.**
Whoever designs a thing is the worst judge of whether the design is
sound, and whoever built it is the worst judge of whether it works.

So the lead reads, judges, and briefs, and does not write product code
or mutate git. The architect plans and cannot implement. The dev
executes a reviewed plan. QA files findings and never fixes them. A
completion report is checked against the repository before it is
believed.

Work is routed by size, in both directions — a one-line fix does not get
a plan, and a subsystem does not get hand-written because briefing felt
slower than drafting.

## The verification rules are the part worth reading

`templates/rules/verification.md` is the file that earns its keep. Its
central claim:

> A clean result from an instrument that cannot see the subject is not
> evidence — and re-running it does not make it evidence.

The natural response to doubt is to run the same check harder — a wider
regex, a second `git status` — and the agreement reads as confirmation
while the blind spot is untouched. So the question is never "did the
check pass?" but **"could this check have failed for the reason I care
about?"**

Corollaries the file states explicitly: a guard is not evidence until it
has been made to fail on purpose; a citation is a claim until you open
the thing cited; git cannot answer questions about files it ignores; and
a guarantee you have not checked is a guarantee you do not have.

## Deliberately incomplete

Two things are left blank by design, because there is no honest content
for them before a stack exists:

- `architecture.md` is not created at all.
- The command table in `verification.md` ships as `_TBD_`.

A guessed build command reads as *verified* to every session that
follows. A stub reads as *unknown*. Fill both from commands you have
actually run — and record any command that exits 0 without checking
anything, because that is indistinguishable from success and the whole
team will keep reaching for it until it is written down.

## Status board

State lives in `~/.claude/status-board/boards/<slug>/`, never inside the
plugin — a plugin directory is replaced wholesale on update, and boards
written there would be destroyed by an upgrade. `STATUS_BOARD_HOME`
overrides the location.

The server is read-only against your repository and binds to
`127.0.0.1` only. It exposes repo paths, branch names and commit
subjects; do not bind it to anything else.

## Requirements

Python 3 for the status board. The agent-team roles need
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set in `settings.json` **before
a session starts** — setting it mid-session puts the variable in the
environment but never builds the team infrastructure, so spawns quietly
become subagents instead. The scaffolded `.claude/settings.json` sets
it; the first session after bootstrap is the one where it takes effect.

## License

MIT
