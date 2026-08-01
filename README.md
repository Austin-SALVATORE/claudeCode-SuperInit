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

Answers you tap reach the agent two ways, kept deliberately redundant
because they fail differently. A `UserPromptSubmit` hook surfaces them
whenever you send a message — durable, since it is plugin config, but it
cannot fire until you speak. A watcher armed under the agent's Monitor
tool emits each answer within seconds of the tap — fast, but it dies with
the session.

That second path was missing for a while, and the reason is the most
useful thing in this repository. The latency was diagnosed as structural:
a hook only fires on `UserPromptSubmit`, therefore nothing can reach the
agent between messages. The first half is true and the conclusion is
false — a background command re-invokes the agent when it exits, and a
monitor delivers output while it is idle. **Every check performed on the
hook passed**: registered, hook count risen, fired on the next message.
None of them could have detected the real problem, and their agreement
read as confirmation. It is `verification.md`'s own rule — *change
instrument, not effort* — broken by its author.

## Requirements

Python 3 for the status board. The agent-team roles need
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set in `settings.json` **before
a session starts** — setting it mid-session puts the variable in the
environment but never builds the team infrastructure, so spawns quietly
become subagents instead. The scaffolded `.claude/settings.json` sets
it; the first session after bootstrap is the one where it takes effect.

## Releasing

**Bump `version` in `.claude-plugin/plugin.json` with every change that
should reach an installed copy.** `/plugin update` compares versions, not
content: at an unchanged version it reports *"already at the latest
version"* and pulls nothing, however many commits have landed.

That failure is silent in the worst direction — the command succeeds, says
something reassuring, and leaves the installed plugin stale. It happened
here: 24 commits accumulated behind `0.1.0`, including a confirmation
dialog, a hook and a security-relevant network option, none of which
`/plugin update` would ever have delivered.

So a push is not a release. The version bump is the release.

## License

MIT
