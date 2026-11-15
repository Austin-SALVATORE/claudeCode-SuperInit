# Operating model

How work moves in this project under Orca and oh-my-claudecode. The
session you are in is the **coordinator**; the work is done by **workers**
that Orca starts. The discipline of the coordinator's role is in your
global `agents.md`; this file holds only what is particular to running
work here.

## The coordinator

- Hands out Tasks, follows them, and keeps the workstream register
  (`docs/workstreams/README.md`) true.
- Decides what goes to the owner, and puts it on the status board.
- Reads, judges and briefs. A change of state (code, tests, git, a
  deploy) belongs to a worker, even when doing it directly looks quicker.

Judge the size first: a one-line fix is done directly; anything
milestone-sized gets an architect worker's plan before an executor
touches it.

## The roster

Workers are oh-my-claudecode agents, started as Orca workers. A tier, not
a model name, is written here: the owner changes one row per project if
the project needs another.

| Role | Agent | Model tier | Effort |
|---|---|---|---|
| Coordinator | the session itself | Opus | `high` |
| Architect | `architect` | Opus | `high` |
| Designer | `designer` | Fable | `high` |
| Executor, complex work | `executor` | Sonnet | `high` |
| QA | `qa-tester` | Sonnet | `high` |
| Executor, ordinary work | `executor` | Haiku | `high` |
| Reviewer | `code-reviewer` | Opus | `high` |
| Verifier | `verifier` | Sonnet | `high` |

## Starting a worker

```
orca orchestration worker-start --spec-file <brief> --agent claude \
  --model <the model of the role's tier> --effort high \
  --task-title "<role> | WS-NN | <subject>" --worktree <see Worktrees>
```

- The brief is a `task-spec` (`.claude/skills/task-spec/`), a file the
  worker can read without having seen this conversation.
- `--effort` needs `--model`: set both on every start.
- More work for a worker that has finished goes to the same worker, with
  `orca orchestration send --to dispatch:<id>`, not to a new one: its
  context is the expensive part.
- A worker ends with `worker_done`: three sentences, what it did, what it
  found, what is left. A reported failure is a failure; never mark it done.

## The Task title

`role | WS-id | subject`, for example `executor | WS-02 | the import step`.
The status board links a worker to its workstream from the id in the
title, and shows the role from the first word. A title without an id still
runs; it just belongs to no workstream on the board.

## Worktrees

- **One writing worker per worktree.** A worker that changes files gets
  its own (`--worktree new-child --name <short>`), unless it is the only
  writer in the coordinator's worktree.
- A worker that only reads may share the coordinator's
  (`--worktree current`), and its spec says "read only".
- The coordinator merges, one branch at a time, after the check the work
  needed.
- **Never revert another worker's uncommitted work.**

## When an independent check is required

Only where a fault is expensive and cannot be seen by looking:

- the path that records the owner's decisions;
- secrets, data and accounts;
- before a commit or a release.

One reviewer or verifier, one narrow question, started as its own worker.
It never holds back the owner's preview: the owner sees the work at once,
and the check follows. Everything else goes to the owner on the worker's
own tests and the coordinator's reading of the result.

## Decisions

The owner's go, and every choice that is the owner's, goes on the status board as
a `decisions` card with a recommendation, not into a chat message. Label
each choice with who decided it and when ("the owner, 3 Mar"; "the
coordinator"), so that nobody has to remember which it was.
