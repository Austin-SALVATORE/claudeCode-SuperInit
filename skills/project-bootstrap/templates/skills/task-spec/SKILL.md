---
name: task-spec
description: Write the brief for an Orca worker so that it can do the work without having seen this conversation. Use when handing any implementation, design, review or audit task to a worker started with worker-start.
---

# Task spec

A worker does not inherit the coordinator's conversation. Whatever the
work needs must be in the spec, a file the worker reads first. A spec
that says "the thing we discussed" is a defect.

## Structure

1. **The ask, in one sentence.** What will exist when this is done.
2. **Where the authority lives.** The spec, plan or review that governs
   the work, by path. If it changed recently, say what changed, so the
   worker reads it fresh.
3. **Limits, and what may be edited.** The files and folders the worker
   may change, and the ones it must not. Decisions already ruled and
   closed. Anything the rules in `.claude/rules/` already say need not be
   repeated.
4. **The worktree it runs in, and who else writes there.** A worker that
   changes files has its own worktree; a worker that only reads says
   "read only" and may share the coordinator's.
5. **The evidence expected back.** The suite, the numbers, the check to
   run before reporting, and whether a non-zero exit counts as a failure.
6. **What is out of scope.** Especially the adjacent work that looks
   tempting: name it and defer it.
7. **How it reports.** `worker_done`, three sentences: what it did, what
   it found, what is left. A failure is reported as a failure.

## Rules

- Substantial work gets a reviewed plan first, not code first.
- Never put an irreversible action (a migration, a deploy, a force push,
  a published artifact) into a spec without the owner's approval written
  in it.
- One spec, one coherent batch: two unrelated concerns are two specs.
- State defects found outside the ask as defects, with their severity and
  whether they block.
