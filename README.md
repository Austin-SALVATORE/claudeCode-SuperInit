# claudeCode-SuperInit

A Claude Code plugin that gives a new repository a working practice on
day one, instead of letting it acquire one by accident.

Two skills:

- **`project-bootstrap`** — scaffolds how a project is run under Orca and
  oh-my-claudecode, in seven small files: the operating model (the
  coordinator, the worker roster, the Task title, worktrees, when an
  independent check is required), the verification and release rules,
  the workstream register, and two skills (`task-spec`, the brief for a
  worker; `milestone-acceptance`). Where the `orca-coordinator` plugin is in
  use, that plugin alone writes the project's worker rules and its register,
  so the bootstrap leaves those out and writes the rest.
- **`status-board`** — a local web page per project, built around two
  lists: the questions that need you, and the project's workstreams.
  Status, findings and everything else sit under them, so structured work
  stops being buried in chat scrollback.

## Install

```
/plugin marketplace add Austin-SALVATORE/claudeCode-SuperInit
/plugin install project-init@claudecode-superinit
```

Then, in any project:

```
/project-bootstrap
```

Running `/init` on a repository with no `.claude/rules/operating-model.md`
triggers the same thing. The scaffold names Orca's commands and
oh-my-claudecode's agents, so it is for projects run under both; the skill
checks that first and stops if either is missing (with the `orca-coordinator`
plugin in use, only Orca is required). A project that has the
older set (made before the Orca-only scaffold) is moved on by
`/project-bootstrap update`, a step you run: it shows what it would add and
retire, asks, and leaves every change uncommitted.

**With the `orca-coordinator` plugin in use, it alone writes the worker rules
and the register.** Before it writes anything, the skill looks for that
plugin by two signs: it is enabled in `claude plugin list`, or its project
part `.claude/orca-coordinator/project.md` is in the project and valid as
that plugin reads it (the register's marker line alone is no sign). When it
is found, the bootstrap writes `CLAUDE.md`, the verification and release
rules, the milestone close-out, the board and the decisions part of the
operating model, and leaves out the register, the worker roster, the start
command, the Task title, the worktree rule, the independent checks and
`task-spec`, with one line saying they come from that plugin. Found or not,
it never overwrites a `docs/workstreams/README.md` that exists: the board
copies it as it is, all its columns. Alone, nothing changes.

**After an update: start each running board's server again, once.** A board
made by an earlier version has no `repo.json` (the record, written when the
server starts, of which folder the board belongs to). The plugin finds a
session's board from the folder the session was started in, and a board of a
subfolder of a project (not the top of a repository) that has no record is not
guessed from its name: a session started there is told nothing until the
board's server has been started once on this version. A board whose project
is the top of its repository is found without the record.

## At session start

Two `SessionStart` hooks remove the manual setup a fresh session used to
need. Each prints nothing when there is nothing to say and exits 0 on every
path, so a bug in either costs you the old behaviour, never the session.

### The bootstrap is offered

A new session in a new folder never suggested `/project-bootstrap`, so the
working practice this plugin exists to provide arrived only if you
remembered to ask for it. `hooks/offer-bootstrap.py` now tells the agent,
in a fresh session, to offer it — and to ask what the project is *before*
anything is written. A hook cannot run a skill, only instruct the agent, so
what is guaranteed is the instruction and its order: no scaffolding without
your yes. The hook never edits a repository.

It offers **only inside Orca** (`ORCA_TERMINAL_HANDLE` is set), in a git
project (the working directory has a git top-level that is not `$HOME`),
when there is no `.claude/rules/operating-model.md` at the working
directory, at its git top-level or **in its main checkout**. The main
checkout is found from the worktree's own `.git` file, without running git,
so a worktree of a bootstrapped project stays silent even before the
scaffold is committed on its branch. A bare `.claude/` is not enough:
Claude Code creates `.claude/settings.local.json` by itself, so testing for
the directory would count nearly every folder as done.

Where the `orca-coordinator` project part (`.claude/orca-coordinator/project.md`,
valid as that plugin reads it) is at one of those three places, the offer says
only what the bootstrap would add and leaves the worker side to that plugin, so
the first reply does not offer a roster and a register twice. The hook looks
for that file only: it does not ask `claude plugin list`, which the skill does.

A project that has the older set (`.claude/rules/` with its role file and
no `operating-model.md`) is not offered the new one. A session there is
told once per session, at startup or `/clear`, that `/project-bootstrap update` can show
what it would add and retire, and is told not to edit those files itself.

The offer and that note stay silent:

- outside Orca, in a folder that is not a git project, in `$HOME`, in `/`,
  and anywhere inside `~/.claude`;
- unless the session is a fresh start (`startup`) or `/clear` — never on
  resume, compact or fork, so it does not repeat;
- in an agent session rather than yours (below);
- in a run nobody attends: `claude -p` and the SDK, where Claude Code sets
  `CLAUDE_CODE_SESSION_ATTENDED=0` and a `CLAUDE_CODE_ENTRYPOINT` that starts
  with `sdk` (either one is enough; an older Claude Code that sets neither
  is treated as attended);
- when you have opted out.

**Opting out.** Either of these:

- `PROJECT_INIT_NO_OFFER=1` in the environment turns the offer off
  everywhere. Put it in the `env` block of `~/.claude/settings.json` to
  make it permanent.
- An empty file `.claude/no-bootstrap-offer` turns it off for that folder
  (checked at the working directory, at its git top-level and in its main checkout). Commit it
  or git-ignore it, as you prefer.

**Agent sessions.** Subagents and in-process teammates never trigger
`SessionStart`, so they never see the offer. A split-pane teammate is a
separate Claude Code process and does. The hook recognises one from
`agent_type` or `agent_id` in its payload, and from the `--agent-id` /
`--parent-session-id` flags on an ancestor process: recorded launches of
seven real pane teammates carry both, and the sessions inspected that were
not one carry neither. Neither tell has been seen in a live teammate's
`SessionStart` — the flags come from those recorded launches, `agent_type`
from the documentation and from the `agent-setting` record that every
teammate transcript examined opens with. If both are missing a teammate sees
the offer: noise, not damage. An Orca worker is recognised as below.

### A dead board comes back

A board's state lives in `~/.claude/status-board/boards/<slug>/` and
survives a reboot. The server that serves it does not, and the first sign
was a dead URL. `hooks/revive-board.py` checks at session start whether the
project has a board (`boards/<slug>/narrative.json`) and whether anything
answers on its port:

- the connection is refused (the one error that says nothing listens):
  `serve.py` is started, detached, and one line prints the URL;
- something answers: the hook asks it for its proof (`GET /_proof`, which only
  the holder of `servers/board.key` can answer, the same request the button's
  opener makes). A board that proves itself: nothing happens. A server is
  stopped with one SIGTERM, and the board started again with one line saying
  so, only when two things hold. The server itself said, in an HTTP answer
  on each of two separate connections, that it cannot prove itself: a board
  of an older release (no proof route), one with no usable key, or one that
  holds an older key. And the process is this project's own recorded server:
  its record `servers/<port>.json` is a regular file of yours naming this
  project and a loopback board, its pid is alive and yours, it is the process
  that listens on the port, and its command line is `python
  .../status-board/serve.py --repo <this project>`. All five are measured,
  and measured again right before the signal. Nothing else ever leads to a
  signal: not a hang, a reset, an answer no board gives, or a connection the
  hook could not make (no source port to give it, no descriptor, a filter: the
  hook's own failure, which says nothing about the board). Each of those gets
  one line that says what the hook saw and is never a stop. When the hook
  cannot connect, it reads the record: a recorded pid that is dead means the
  board is not running and the hook starts it; a live pid, or no record,
  means one line saying it could not check. After a stop the hook waits for
  the process to be gone, not for the port, and then starts the board.
  Anything else that holds the port (another program, another project's
  server, another account's process, a board in phone mode) gets one line
  that says so and is never stopped, and so does anything that cannot be
  confirmed; a server that ignores SIGTERM is reported, never killed by force;
  a board is restarted at most once in ten minutes. A key that others can read
  is reported too (no proof can be checked, and a restart cannot help).

The slug and port are `serve.py`'s own. The hook imports `slug_for` and
`port_for` rather than copying them, so the two cannot drift. The server
runs in its own session with stdout and stderr in
`boards/<slug>/serve.log`; if it cannot be brought up, the hook prints one
line pointing at that log. Nothing waits more than a few seconds, and a lock
file keeps two sessions that start together from launching two servers.

What a revived board does not keep: `serve.py` stores no display name, so
it comes back under the folder name; and it comes back on loopback only, so
a board you had exposed with `--host` needs `--host` again (the hook never
restarts one). Another project's board on the same derived port (there are
120 ports) holds the same key and proves itself, so it is left alone.

### The board's link is in the session

In a session of a project that has a board, a row above the prompt shows one
button, `9: board localhost:8800/<name>` (`hooks/board-link.tsx`, with
`status-board/board_link.py`), at the right end of the row, beside the `[-]`
that Claude Code draws there; it opens the board in your browser (see below),
and says `(not running)` when the board's server is down. It is a button and
not a link, so that nothing prints an address. The row follows the width of
the terminal, and a name too long for it is cut with an ellipsis and stays
one row. The row is the plugin's own:
it leaves other mods' rows alone, including on Claude Code before 2.1.323,
where all mods shared one drawing in the band. It is a mod,
which needs Claude Code 2.1.287 or later; an older one ignores the file and
every hook above still works. A row you do not want can be folded away with
ctrl+x ctrl+a. After the address, `(not running)` says that the board's server
is not alive: it comes within five minutes of the server stopping (a server
that dies changes no file, so the row finds out at its next five-minute look),
and a process id that another program has taken since keeps the address
without the mark. The button does not go through the front door: it opens the
board itself, and the opener says so when the server on the board's port does
not prove it is this project's board.

The project, the board and the name are found as the rest of the plugin finds
them: the board from the folder the session was started in
(`answer_ledger.find_board`), the name by the front door's own rule
(`FrontDoor.resolve`). Nothing is shown where there is nothing to show: no
project, no board, a board never started, a name that the front door would not
send to this project's board, a worker, a teammate, a subagent, `claude -p`.
It follows the session: a board started later appears within ten seconds,
without a restart, and the row survives `/clear`, `/compact` and a resume.

It costs nothing: it sends no request, to a board or to the front door (the
helper reads `repo.json` and the records under `servers/`, and never a board's
answers or narrative), writes no file, and any error draws nothing. A session
looks at two folders every ten seconds with no process, starts the helper at
the start, when a folder has changed, and every five minutes otherwise (about
thirteen runs of 70 ms an hour). Opening the link records nothing: only a tap
on the board does.

### The button above the prompt, and the link in the status line

Two switches of the plugin's own say what is shown. They are Claude Code's
plugin options: shown when the plugin is installed (a dialog), kept per user in
your settings, unchanged by updates, and changeable at any time in `/config`
(search for "board link"); a change takes effect at once, without a restart.

| Switch | Default |
|---|---|
| **Board link above the prompt** | on: the button above the prompt, as described above |
| **Board link in the status line** | off: nothing is put in your status line until you ask; when it is on, the link is an addition beside the button |

The button above the prompt is shown whenever either switch is on: the status
line's link is an addition beside it and never replaces it (that link is the
board's own address, which opens the board in whichever browser it is opened
in). That link is a real OSC 8 hyperlink where the terminal supports them
(iTerm2, Ghostty, WezTerm, kitty: command-click or ctrl-click), and plain text
`board` elsewhere. Only both switches off hides the button, and that is your choice too:
nothing is shown.

Nothing here needs anything else: with the defaults the button is shown and the
plugin works without claude-hud, as it did before; claude-hud is optional and
the plugin has no dependency on it. Someone who updates from an older release
is not asked anything and gets the defaults.

To have the link in the status line as well, run `/project-init:board-link`. It
asks once whether the link should also be in the status line and, for the
status line:

- if you use claude-hud (another author's status line plugin, which shows the
  model, the context bar and git), it adds a small filter after your status
  line command, so that a clickable `board` sits right after the context usage;
  it shows you the old and the new command first and writes nothing without
  your yes; where your permission settings ask, Claude Code's own permission
  prompt asks again;
- if you do not have claude-hud, it only names claude-hud's own commands for you
  to type (it never installs another author's plugin, and never sets your
  status line for you); the button above the prompt stays;
- if you have a status line of your own, the same filter can be added after it;
  any failure of the filter prints your status line exactly as it came.

The button is never lost to the status line: it is there whether or not the
status line shows the link (claude-hud's set-up may rewrite the command, claude-hud
may be removed, Python may be missing, or the plugin may be gone: the button
does not depend on any of it). The skill never switches the button off. The
question is raised at most once per user: by `/project-bootstrap`, or by one
line in the first interactive session in a project with a board, never in a
worker, a teammate or `claude -p`.

To take the link out of the status line, choose "Take the link out of the status
line" in `/project-init:board-link`, or run `python3
status-board/statusline_setup.py remove` from the plugin: it puts your old
command back exactly; the button above the prompt was never taken away. If the plugin was removed
first, the filter in your status line command is harmless (it prints your
status line unchanged): your old command is the part inside `out=$( … )`, and
you can put it back by editing the `statusLine` command in your
`settings.json`.

### Under Orca, a worker is not the owner's session

Orca's coordinator starts workers with `orca orchestration worker-start`,
and each worker is a Claude Code session of its own, so these hooks fire
in it too. Three of them were written for your session and are silent in
a worker's (`hooks/orca_session.py`):

- the bootstrap offer, which arrived ahead of the task the worker was
  dispatched for, in a worktree that is one task's checkout;
- the answer waiter, which woke the worker with your answers and marked
  them told, hiding them from the coordinator;
- the answer hook, which printed your answers into the worker's context
  and marked them surfaced, hiding them from the coordinator.

Nothing in the environment tells a worker from a coordinator. The hooks
recognise one from the dispatch that opens its first prompt, or from a
dispatch that Orca lists as still live on its terminal, read with one
listing command. A terminal that hosted a worker once does not count: a
session you start there yourself is yours. Any doubt counts as "not a
worker", and the hook does what it did before.

Nothing in the plugin guards a shared checkout against two writers: the
rule is the worktree rule of `operating-model.md`. **One writing worker per
worktree**: a worker that changes files gets its own
(`--worktree new-child`), a worker that only reads may share the
coordinator's, the coordinator merges one branch at a time, and nobody
reverts another worker's uncommitted work. (Where the `orca-coordinator`
plugin is in use, `operating-model.md` carries no worktree rule: that plugin's
rule is the one.) The hook that used to watch
`Agent`, `Task` and `SendMessage` never saw an Orca dispatch, since a worker
is started from a shell command; it is gone, and the board's report no
longer carries its row.

## What the scaffold actually asserts

The point is not the file tree — it is the separation:

**Design, implementation, and verification are held by different workers.**
Whoever designs a thing is the worst judge of whether the design is
sound, and whoever built it is the worst judge of whether it works.

So the coordinator reads, judges, and briefs, and hands work to Orca
workers: an architect plans, an executor carries out a reviewed plan, QA
files findings and never fixes them. The roster in `operating-model.md`
names oh-my-claudecode's agents and gives each a model tier, not a model
name, so it does not go stale when a model is renamed. An independent
check, by a different worker, is required only where a fault is expensive
and cannot be seen by looking: the path that records your decisions,
secrets, data, accounts, and before a commit or a release. It never holds
back your preview. (With the `orca-coordinator` plugin in use, the roster and the
checks are that plugin's, and the scaffold does not write them.) A completion report is checked against the repository
before it is believed.

Work is routed by size, in both directions — a one-line fix does not get
a plan, and a subsystem does not get hand-written because briefing felt
slower than drafting. Every worker is started with a Task title,
`role | WS-id | subject`, and the status board links it to its workstream
from the id; the register is `docs/workstreams/README.md`.

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

A board opens by its address in any browser on this machine, from the status
line's link, a bookmark or a typed address; any program running on this
machine can then read it, and record an answer to a question it asks by
sending the board's own headers. To have each browser tab opened from Claude
Code instead (2.4.0's lock), create the empty file
`~/.claude/status-board/board-lock`; delete it to open boards by address
again. A running board follows the file at its next request: no restart,
nothing to reload.

What a board keeps checking whether the lock is on or off: a request must be
addressed to this machine by one of its own names (a web site that points a
name of its own at `127.0.0.1` reads nothing); a read is refused when the
browser says it comes from another site or from another port of this machine
(a dev server open in the same browser); a write must be JSON, with the
board's own `Origin`, and goes only to a question the board asks (one shown in
the last 30 minutes); an answer sent without a browser's same-page mark shows
as "not marked as sent by a browser", and the session confirms it with you
before it acts on anything hard to undo; the board cannot be framed by another
site; the front door tells no path, and the status-board home is closed to
other accounts (0700, files 0600). A board started with `--host` also takes
answers from the network with the printed token, which is not a proof of you:
the board records them as coming from the network, and the session confirms
them with you in the same way.

The lock, when you turn it on, is the 2.4.0 behaviour. The board keeps its
data (`report.json`, the shots) and every write behind a key,
`servers/board.key` in the status-board home: 0600, in a 0700 folder, so
another account on the machine, or a program that can reach the network and
not your files, cannot read it. The key is in no link, no address and no text
that Claude Code draws: the row above the prompt is a button, and the status
line links the board itself, `http://127.0.0.1:<port>/`, never `localhost` (a
browser given `localhost` may try `::1` first, where another program can
listen), and neither holds a key. Your browser never gets the key either: it
gets a token for that one board, from a small opener
(`status-board/open_board.py`): click **board** in the row above the prompt (a
click needs fullscreen rendering; otherwise type the digit 9 into an empty
prompt, which presses it and is not typed, or press ctrl+x then Tab, Tab to
the button and Enter), or tell the session "open the board". With the lock on,
a board is opened from Claude Code for each browser tab. The opener writes a
form that only your account can read and that holds the token, never the key;
before it writes the form it asks the board's port to prove it holds the key
(one request with a random challenge, carrying no secret); if the port does
not prove it, nothing is opened and the opener says so, so the form's token is
never posted to another program that happens to hold that port. It hands the
file to your system's opener, which opens it as a new document, in a tab of
its own (its name is new each time), where the form posts it to the board; the
file is then removed. The token is bound to that run of the board (a new id
each time the board starts), so a program that takes a stopped board's port
and keeps what it was sent holds nothing the board will take when it is back.
The board's own page keeps the token in that tab's own storage and sends it as
a header to its own address, so no other tab and no server on another port
receives it (a cookie would go to every one of them). A program that takes the
port of a stopped board can still be sent the token by a tab that stays open,
but it belongs to the run that ended: no board will take it, now or when the
board is back. A token dies with that run of the board and with its tab: a
reload keeps working, and, with the lock on, a new tab, a bookmark or the
status line's link shows a page that asks you to open the board from Claude
Code. Restart each running board once after updating: a board of an older
release has no switch, and ignores the key.

A new session in the project restarts a board that holds no key or does not
answer as its project's board, when its server is the project's own (see "A
dead board comes back"); that is what the button's sentence asks for. A
board's server can also be stopped by hand: its record is
`servers/<port>.json` in the status-board home (`~/.claude/status-board`, or
where `STATUS_BOARD_HOME` points), and the `pid` in it is the board's server;
stop that process (`kill <pid>`). A session in the project then starts the
board again, with a key, the next time one starts there.

Each board has its own port, derived from the project's path, which is
the right thing for the board and hard for a person to keep in mind. So
one address is fixed: `http://localhost:8800` lists every board by name,
port and whether it is up (for a worktree, the name of its project) and
shows no project path, and `http://localhost:8800/<name>` (the project's
folder name, in any case, or the start of it when that is unique) sends
the browser on to that project's own board. Nothing is installed or
started for it: every board server tries to hold port 8800, whichever
holds it serves the list, and when that board stops another takes over
within five seconds. It only redirects, never proxies, so each board
keeps its own origin and its own checks, and it listens on `127.0.0.1`
only. A board that is down says so, and says how it starts, without a
folder. A
board gets its name once its server has been started again on this
version; until then its port address works as before. The port is
`STATUS_BOARD_FRONT_PORT`; boards in a home set by `STATUS_BOARD_HOME`
take part only when it is given. A board announces the address by name
only when whatever holds the port proves that it is this home's front
door (it answers a fresh challenge with a secret kept in
`servers/front.key`, readable by your account only); otherwise it gives
the port address and says that the port is held by something that is not
a board. `/<name>` goes only to a port where a `serve.py` of yours
listens; the door looks that up in one background check (at most once a
second while a board is new to it, once a minute otherwise), never in the
request itself, so a list of twenty boards answers at once, and a board it
has not looked at yet says "not checked yet". A port set in
`STATUS_BOARD_FRONT_PORT` inside 8801 to 8920, where the boards take their
own, is refused when a server starts, with one line saying so.

It writes three small files of its own, and only when its own page asks:
a write must be sent as JSON, from the board's own origin, to this
machine under its own name. Any other page open in your browser is
refused, so nothing but your tap can record an answer the agent will act
on.

It is read under this machine's own names only: `127.0.0.1`, `localhost`
and `::1`. A request from this machine under any other name is refused
with a 403, for the page, the report and the shots alike. That is what
closes the other half of the same door: a web page that points a name of
its own at your machine cannot read the board through your browser.

If you read the board under another name yourself (an entry in
`/etc/hosts`, a tunnel, a reverse proxy), list the names:

```
STATUS_BOARD_HOSTS=board.test,other.name
```

It is read once, when the server starts. Put it in the `env` block of
`~/.claude/settings.json` so that a board started again by the session
hook has it too. Nothing is added by default, not even your machine's
own hostname: a foreign name is exactly what an attacker's request looks
like, so the exception is yours to state.

Screenshots under `shots/` are served as pictures and nothing more. An
SVG is also a document, so every shot is sent sandboxed: opened in a tab
of its own it runs no script and reaches nothing.

If the server is down when a session starts — after a reboot, say — a hook
starts it again; see [At session start](#at-session-start).

### Two lists first

The first screen is the headline, the stamp that dates it, and two
lists. **Needs you** is the queue of questions: one open at a time, with
its reasoning and options; the rest are a list to read down, each with
the agent's pick. **Workstreams** is the project's register: every
subject on one row with its status and next step, counts by status, a
filter, and the rest of a row on demand. In a narrow pane the two are
tabs; from 1100 px they sit side by side. A board whose narrative has no
`workstreams` field shows the first list alone.

### Who is working, read from Orca

Run under Orca, the board reads the coordinator
and its workers from Orca itself, with five listing commands of the
`orca` CLI (`run-list`, `worker-list`, `terminal list`, `worktree ps`,
`task-list`) and never one that changes anything. Each worker shows the
role that opens its Task title, the title, its state and when it last
moved, and appears on the workstream whose id its title carries. The
listings run in the background, so a missing, slow or failing `orca`
costs a request nothing: the board then derives the team from the team
config and transcripts, as it does for a project outside Orca.
`STATUS_BOARD_ORCA=0` switches the reading off.

The coordinator is never guessed. It is the terminal an Orca Run names;
if no Run names one in this project, the row is absent and the page says
why.

Answers you tap reach the agent two ways, kept deliberately redundant
because they fail differently. A `UserPromptSubmit` hook surfaces them
whenever you send a message — durable, since it is plugin config, but it
cannot fire until you speak. A second hook waits in the background after
each of the agent's turns and wakes the session within seconds of a tap.
It needs nothing from the agent and ends with the session. Both read and
mark the same record, so an answer is told once.

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

Python 3 for the status board and the session-start hooks. The scaffold
that `project-bootstrap` writes needs Orca (its `orca` command, or the
terminal handle it sets) and oh-my-claudecode, whose agents the worker
roster names; the skill checks both before it writes anything. It writes no
setting and no environment variable. With the `orca-coordinator` plugin in
use, the scaffold names no oh-my-claudecode agent, so only Orca is required
and a missing oh-my-claudecode does not stop the bootstrap.

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
