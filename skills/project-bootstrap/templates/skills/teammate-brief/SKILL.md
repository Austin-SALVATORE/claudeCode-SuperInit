---
name: teammate-brief
description: Write a self-contained work brief for a teammate — architect, dev, or QA — that survives having no shared conversation history. Use when handing off any implementation, design, or audit task.
---

# Teammate brief

A teammate does not inherit the lead's conversation. Whatever context
the work needs must be *in the brief*. A brief that says "the thing we
discussed" is a defect.

The same text serves both delivery paths: pasted into a separate
session, or sent over the mailbox to a spawned teammate.

## Structure

1. **The ask, in one sentence.** What will exist when this is done.
2. **Where the authority lives.** Point at the spec, plan, or review
   contract that governs this work — by path. If the document changed
   recently, say so and say what changed, so the teammate reads it
   fresh instead of relying on a stale summary.
3. **Constraints that are not negotiable.** Rules that already exist in
   `.claude/rules/**` don't need repeating — but anything task-specific
   does: files not to touch, sequencing against other in-flight work,
   decisions already ruled and closed.
4. **Open questions with their rulings.** If the work raised questions
   the owner already answered, state the answers here. If a question is
   still open, say who owns it and whether the teammate should proceed
   around it or stop.
5. **What "done" looks like.** The verification the teammate runs
   before reporting: suite, typecheck, build numbers, a manual check.
6. **What is out of scope.** Especially the adjacent work that looks
   tempting — name it and defer it explicitly.

## Rules

- Plans before milestones: substantial work gets a reviewed plan first,
  not code first.
- Never bundle an irreversible action (migration, deploy, force push,
  published artifact) into a brief without the owner's explicit
  approval recorded in it.
- If the brief is for a spawned teammate, require plan approval for
  anything beyond a contained batch.
- One brief, one coherent batch. Two unrelated concerns are two briefs,
  so either can be rejected without blocking the other.
- State discovered defects as defects even when they're outside the ask
  — with severity and whether they block.
