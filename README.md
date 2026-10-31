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

## At session start

Two `SessionStart` hooks remove the manual setup a fresh session used to
need. Each prints nothing when there is nothing to say and exits 0 on every
path, so a bug in either costs you the old behaviour, never the session.

### The bootstrap is offered

A new session in a new folder never suggested `/project-bootstrap`, so the
working practice this plugin exists to provide arrived only if you
remembered to ask for it. `hooks/offer-bootstrap.py` now tells the agent,
in a fresh session, to offer it — and to ask what the project is and who
works on it *before* anything is written. A hook cannot run a skill, only
instruct the agent, so what is guaranteed is the instruction and its
order: no scaffolding without your yes.

"No team structure" means no `.claude/rules/team-roles.md` at the working
directory or at its git top-level. A bare `.claude/` is not enough: Claude
Code creates `.claude/settings.local.json` by itself, so testing for the
directory would count nearly every folder as done.

The offer stays silent:

- in `$HOME`, in `/`, and anywhere inside `~/.claude`;
- unless the session is a fresh start or `/clear` — never on resume,
  compact or fork, so it does not repeat;
- in an agent session rather than yours (below);
- when you have opted out.

**Opting out.** Either of these:

- `PROJECT_INIT_NO_OFFER=1` in the environment turns the offer off
  everywhere. Put it in the `env` block of `~/.claude/settings.json` to
  make it permanent.
- An empty file `.claude/no-bootstrap-offer` turns it off for that folder
  (checked at the working directory and at its git top-level). Commit it
  or git-ignore it, as you prefer.

**Teammates.** Subagents and in-process teammates never trigger
`SessionStart`, so they never see the offer. A split-pane teammate is a
separate Claude Code process and does. The hook recognises one from
`agent_type` or `agent_id` in its payload, and from the `--agent-id` /
`--parent-session-id` flags on an ancestor process: recorded launches of
seven real pane teammates carry both, and the lead sessions inspected carry
neither. Neither tell has been seen in a live teammate's `SessionStart` —
the flags come from those recorded launches, `agent_type` from the
documentation and from the `agent-setting` record that every teammate
transcript examined opens with. If both are missing a teammate sees the
offer: noise, not damage.

### A dead board comes back

A board's state lives in `~/.claude/status-board/boards/<slug>/` and
survives a reboot. The server that serves it does not, and the first sign
was a dead URL. `hooks/revive-board.py` checks at session start whether the
project has a board (`boards/<slug>/narrative.json`) and whether anything
answers on its port:

- the board page answers: nothing happens;
- nothing is listening: `serve.py` is started, detached, and one line
  prints the URL;
- something else is listening: nothing is started, and one line says the
  port is taken.

The slug and port are `serve.py`'s own. The hook imports `slug_for` and
`port_for` rather than copying them, so the two cannot drift. The server
runs in its own session with stdout and stderr in
`boards/<slug>/serve.log`; if it cannot be brought up, the hook prints one
line pointing at that log. Nothing waits more than a few seconds, and a lock
file keeps two sessions that start together from launching two servers.

What a revived board does not keep: `serve.py` stores no display name, so
it comes back under the folder name; and it comes back on loopback only, so
a board you had exposed with `--host` needs `--host` again. Nor can the
hook tell a different project's board on the same derived port (there are
120 ports) from this one; it leaves that alone.

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

If the server is down when a session starts — after a reboot, say — a hook
starts it again; see [At session start](#at-session-start).

Answers you tap reach the agent two ways, kept deliberately redundant
because they fail differently. A `UserPromptSubmit` hook surfaces them
whenever you send a message — durable, since it is plugin config, but it
cannot fire until you speak. A watcher armed under the agent's Monitor
tool emits each answer within seconds of the tap — fast, but it dies with
the session.

### The one field that could lie without aging

Everything on the board is either **re-derived per request** (branch, SHAs,
dirty files — cannot go stale) or **dated** (the narrative prints its own
age, and turns amber past 25 minutes). `inFlight` — "Running now" — was
neither, and it is the only present-tense claim on the page.

It went wrong exactly as that gap predicts. Writing the board is triggered
by replying to the owner; agents start, finish and go idle *while the owner
is silent*. So the field most likely to be wrong was refreshed on the
cadence least correlated with its changes, and a finished verifier sat
there spinning until the owner asked why the board disagreed with the chat.

The fix is not "remember to clear it" — that is the rule that had already
been written and already been broken. `inFlight` entries now carry `since`,
and the page ages each one live in the browser: past 20 minutes the card
turns amber and dashed, its label changes from "started 4 min ago" to
"unconfirmed for 34 min", and the section heading stops saying *Running
now*. The board still cannot see agents and still cannot know whether the
claim is true. It can stop asserting something it has no way to check, and
that is the whole of the fix.

### A finding that is a picture

Some defects cannot survive as prose. One was described here in three
careful, measured paragraphs and read as minor; a single screenshot made
it obvious in two seconds. Before that, the best the board could do was
print a path and ask the reader to go and open it — falling back to prose
wearing a filename.

Cards and decision options take a `shot`: a **bare filename**, served from
the board's own `shots/` directory. That is the whole security model
rather than a limitation — there is no path to traverse because there is
no path. The name is matched against a strict pattern, and containment is
re-checked *after* resolution, because a symlink dropped into `shots/` is
an escape the pattern cannot see. Both guards were attacked on purpose;
the symlink case is caught only by the second one. It also fixes a
lifetime problem: shots taken in a session scratchpad vanish with the
session, so a board citing them rots into broken images.

### The answer path

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

Python 3 for the status board and the session-start hooks. The agent-team roles need
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
