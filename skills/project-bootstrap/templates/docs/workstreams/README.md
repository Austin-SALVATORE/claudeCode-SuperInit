# Workstreams

The register of this project's workstreams: one subject, one row. The
status board's Workstreams list is a copy of this table (the `workstreams`
field of its narrative); when they differ, this file is the one to fix.

**Status words:** `planned` · `active` · `blocked` · `done`.

| ID | Name | Status | Next step | Record |
|---|---|---|---|---|
| WS-00 | Set-up | active | Fill in the command table in `.claude/rules/verification.md` | |

## How to add a workstream

1. Take the next number (`WS-01`, `WS-02`, …) and give it a short name.
2. Add a row with its status and the one next step.
3. Use the id in the Task title of every worker that works on it
   (`role | WS-id | subject`), so the board shows who is on what.
4. When the subject has more to say than a row, start a record,
   `WS-NN-<name>.md`, beside this file.

## A record (`WS-NN-<name>.md`)

Five headings, in this order:

- **Goal**: what will be true when it is done.
- **Decisions**: what was decided, by whom, and when.
- **Open**: what is not decided or not known yet.
- **Done**: what has been done, with the evidence.
- **Links**: the specs, reviews and branches that belong to it.

Write it whenever a workstream moves, and update the board in the same
action: a register that is a day behind the board is wrong twice.
