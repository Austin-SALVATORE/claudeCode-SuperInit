# The hooks of project-init

`hooks.json` holds the plugin's command hooks and, under `modules`, the board-link mod (`board-link.tsx`). Until release 2.3.0 each
of its ten hook groups also carried a `description` key with what the group is for. Claude Code's documentation names a
description in one place only, at the top of the file, as one string; it names none for a hook group or a hook. Claude Code
2.1.325 therefore reported those keys at the start of a session (`project-init: hooks.json: unknown keys "description" in
hooks.UserPromptSubmit[0], ... and 5 more ignored`), and ignored them.

What each group is for now stands here, one section per group, in the order of the file. Each section says what the group runs
(its matcher, its command, its other keys) and then gives the group's text, word for word as it was in `hooks.json`. The section
heading is the event and the group's place in that event's list, as Claude Code's message names it. Some texts name tags such as
`<board-link-offer>`; a Markdown viewer may hide a tag it does not know, so read the file as text if one looks missing.

## UserPromptSubmit[0] (board-answers.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/board-answers.py"`.

Surface newly-tapped status-board answers into context. The board records answers server-side and notifies nobody; relying on the agent to remember to read the file failed within an hour of the rule being written. Durable but slow: it can only fire when the owner sends a message, so a tap waits for their next prompt. The waiter (hooks/answer-waiter.py, registered on this event too, on SessionStart and on Stop) is the fast path and removes that latency — keep both, they fail differently. Both take their answers from status-board/answer_ledger.py, which marks what it hands out under one lock, so an answer is told once, whichever path got to it first. This hook also says to the agent, once, when no waiter is running for the session. Silent in a worker that Orca dispatched (hooks/orca_session.py): the answers are for the session that keeps the board, and a worker that read them also marked them surfaced, hiding them from the coordinator.

## UserPromptSubmit[1] (answer-waiter.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/answer-waiter.py"` (timeout 1814400, async True, asyncRewake True).

Fast path for status-board answers: waits in the background and wakes the session within seconds of a tap (asyncRewake: exit 2 wakes Claude immediately even when the session is idle; measured 14 Nov 2026, 0.06 s to a new turn). Started on SessionStart, UserPromptSubmit and Stop, so it runs again after every turn with nothing asked of the agent; Stop alone misses a turn the owner interrupts. `async` is set as well as `asyncRewake` so that a Claude Code which ignored asyncRewake would still background it rather than block the end of a turn. `timeout` is 21 days (1 814 400 s), above the waiter's own deadline of 21 days minus an hour, so Claude Code never kills it, and under the 2^31-1 ms that a JavaScript timer can hold (Claude Code 2.1.325 clamps a longer one and logs it; a hook with timeout 20 was killed at 20 s, so the figure is enforced: spike of 14 Nov 2026). A session can sit idle for days and its keeper must still be woken by the owner's tap. After 21 days with no hook the waiter ends; the owner is then told by the other sessions that an answer waits unheard (hooks/board-answers.py). Replaces the Monitor watcher, which could not be armed as instructed (no `persistent` option), ended after 30 minutes, and made /restart refuse; a waiting hook does not. Shares the seen-marker with board-answers.py under one lock. Claims nothing when its session was restarted (/restart re-execs Claude Code in place and leaves a deaf waiter behind): see the file. Silent in a worker that Orca dispatched.

## SessionStart[0] (answer-waiter.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/answer-waiter.py"` (timeout 1814400, async True, asyncRewake True).

The answer waiter, started with the session (startup, resume, clear, compact), so an answer that lands while nothing is waiting is delivered at once and a restarted session gets a waiter that can reach it: a waiter from before /restart survives it but is deaf. See the entry on UserPromptSubmit. Replaces the instruction to arm a Monitor watcher, which the agent could not follow as described and which was not given at all on a board with no answers yet.

## SessionStart[1] (offer-bootstrap.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/offer-bootstrap.py"` (timeout 10).

Two plugins in one project (19 Nov 2026): where the orca-coordinator plugin's project part (`.claude/orca-coordinator/project.md`) is at the cwd, its git top-level or its main checkout, and is valid as that plugin reads it (a plain file of at most 64 KiB with one of its own keys, a value it takes, before the line `## How this project differs`; an empty file, prose or other keys are not), the offer says only what the bootstrap would add (`CLAUDE.md`, the verification and release rules, the milestone close-out, the decisions part of the operating model, the board) and leaves the worker side to that plugin. The register's marker line alone is no sign. The hook looks for the file only and never asks `claude plugin list`; the skill checks both signs. The older-set note and every other condition are as they were.

Offer the Orca bootstrap, only inside Orca (ORCA_TERMINAL_HANDLE set), in a git project that has no Orca operating model. A new session in a new project never suggested /project-bootstrap, so the working practice this plugin exists to provide arrived only if the owner remembered to ask. A hook cannot run a skill, only instruct the agent, so this is an instruction — and it insists on the order: ask what the project is, get a yes, only then scaffold. Silent when .claude/rules/operating-model.md exists at the cwd, its git top-level or its main checkout (found without git, from a worktree's .git file, so the worktree of a bootstrapped project is silent even before the scaffold is committed on its branch; a bare .claude/ proves nothing: Claude Code creates settings.local.json itself), outside Orca, in a folder that is not a git project, in $HOME, / and ~/.claude, on resume/compact/fork, in an agent session, in a run nobody attends (claude -p and the SDK: Claude Code sets CLAUDE_CODE_SESSION_ATTENDED=0 and a CLAUDE_CODE_ENTRYPOINT starting with sdk there), and when opted out (PROJECT_INIT_NO_OFFER=1, or an empty .claude/no-bootstrap-offer in the project). A project with the older set only (.claude/rules/team-roles.md, no operating-model.md) is told once, instead of the offer, that /project-bootstrap update exists. Nothing here edits a repository. Fails quiet: any error prints nothing and exits 0, so a bug costs the offer, not the session. A pane teammate is recognised by agent_type/agent_id, or by --agent-id/--parent-session-id on an ancestor process; neither has been seen in a live teammate yet, so one may still see the offer — noise, not damage. An Orca worker is a plain claude process with neither tell; it is recognised from a dispatch that Orca lists as still live on its terminal (hooks/orca_session.py), on one observation that the record exists before SessionStart. A settled worker's terminal does not count. A second, independent block (2.3.0) says two things about where the board's link shows, each once, to the owner's own interactive session (not a worker, a teammate or claude -p; at startup only; in a project that has a board; inside or outside Orca): a <board-link-offer> line, once per user, that /project-init:board-link lets the owner choose between the row above the prompt, the status line or both (a marker created with O_EXCL in the plugin's data folder, which a command hook is given as CLAUDE_PLUGIN_DATA, so two sessions starting together say it once; silent once the owner has answered); and a <board-link-statusline> line, once per change of the status line command, when the link was added to the status line (statusline-before.json) and its command no longer carries it while the status-line switch is on. It writes only those markers, in the data folder.

## SessionStart[2] (revive-board.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/revive-board.py"` (timeout 25).

Start a project's status board when it is not running, and restart a board of its own that cannot prove itself. A board's state survives a reboot but its server does not, and the owner found out by opening a dead URL. If the project has a board and nothing answers on its port (derived by serve.py itself — imported, not copied, so the two cannot disagree), this starts serve.py detached, logging to boards/<slug>/serve.log, and prints the URL. If something answers, it asks `GET /_proof` (the opener's request: a connection of 1 second at most, then one request of 2 seconds, no redirect, 1 kB) and does nothing when the board proves that it holds servers/board.key. A process is stopped only when two things hold: the listener itself said, in an HTTP answer on each of two separate connections, that it has no proof route (an older version), no usable key, or a proof the key does not make (an older key); and the process is this board's own: its record servers/<port>.json is a regular file of this account's naming this project and a loopback board, the pid is alive and this account's, it is the process that listens on the port, and its argv is python .../status-board/serve.py with this project's folder as --repo. All five facts are measured, and measured again right before the one SIGTERM; anything that cannot be established means one line that says so and nothing stopped. No silence, hang, reset, failed connect or odd answer ever leads to a signal: the absence of an answer can be the hook's own failure to ask (no source port to give its connection, no descriptor, a filter), and only a refused connection says that nothing listens. When the hook cannot connect it reads the record: a dead recorded pid (gone, or a zombie nothing has collected) means the board is not running and it is started; a live pid, or no record, means one line saying it could not check. After a stop the wait is for the pid only, and once it is gone the board is started whatever the port reads. Another program, another project's serve.py, another account's process and a board in phone mode are reported and never stopped; a server that ignores SIGTERM is reported and never killed by force; a board is restarted at most once in ten minutes, under a lock. A key that others can read is reported (a restart cannot help). Fails quiet but not blind: it always exits 0, and when it cannot bring the board up it prints one line pointing at the log. Limits: it restarts with serve.py's defaults (loopback only; the folder name as display name, since serve.py stores none), it only looks at the board's derived port, a connect that times out on loopback is read as the hook's failure to ask (a listener whose accept queue is full looks the same: nothing is stopped), and on macOS the window between its last check and the signal is a few milliseconds (on Linux the signal goes through a pidfd).

## Stop[0] (answer-waiter.py)

Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/answer-waiter.py"` (timeout 1814400, async True, asyncRewake True).

The answer waiter again, after every turn of the agent: the waiter ends when it wakes the session, when it is replaced, and at its deadline, and this starts the next one with nothing asked of the agent. See the entry on UserPromptSubmit.

## PostToolUse[0] (board-keeper.py)

Matcher: `Write`. Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/board-keeper.py"` (if `Write(//**/boards/*/narrative.json)`, timeout 5).

Record which session writes the board, so that only it is told the owner's answers (the owner's ruling, 14 Nov 2026: the session that last wrote the board's content; another session that takes the board over becomes the one that is told). Runs only for a write to a board's narrative.json: the `if` rule keeps every other Write free. A Write has its own rule: an Edit(...) rule does not match a Write (measured 14 Nov 2026, Claude Code 2.1.325).

## PostToolUse[1] (board-keeper.py)

Matcher: `Edit|MultiEdit|NotebookEdit`. Runs `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/board-keeper.py"` (if `Edit(//**/boards/*/narrative.json)`, timeout 5).

Same, for the Edit tool (the Edit rule applies to the other editing tools too, by the documentation; only Edit itself was run).

## PostToolUse[2] (board-keeper.py)

Matcher: `Bash`. Runs `p=$(cat); case "$p" in *narrative.json*) printf '%s' "$p" | python3 "${CLAUDE_PLUGIN_ROOT}/hooks/board-keeper.py";; esac` (timeout 5).

The end of a Bash call that names narrative.json (cat, cp, tee, a script's argument, a `>` redirect or a heredoc): its session is recorded as the board's writer only if the file changed during this very call and no other session's call that named it could have changed it (see PreToolUse). It has no `if` rule on purpose: a `>` redirect's target is not part of what a Bash rule sees (measured 14 Nov 2026), so a rule could never see `cat > narrative.json`. The shell `case` starts no Python for a payload that does not mention the file (about 6 ms per Bash call, in every session; Python's own start-up is 30 ms); the hook checks the command text itself, since the payload also holds the output. A script that writes the file without naming it records nobody, and neither does the owner's editor: there is no sweep at the end of the turn (the review of 14 Nov 2026, findings 2 and 3).

## PreToolUse[0] (board-keeper.py)

Matcher: `Bash`. Runs `p=$(cat); case "$p" in *narrative.json*) printf '%s' "$p" | python3 "${CLAUDE_PLUGIN_ROOT}/hooks/board-keeper.py";; esac` (timeout 5).

The start of a Bash call that names narrative.json: stores the file's signature, so that the end of the call (PostToolUse) can tell whether this call changed it. A Bash call's Pre and Post carry the same tool_use_id, Pre first, for a session and for a subagent too (spike of 14 Nov 2026, Claude Code 2.1.325). Same shell test as there: no Python for a command that does not mention the file.
