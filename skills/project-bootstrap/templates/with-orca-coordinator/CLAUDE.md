# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

**No code exists yet.** The stack, architecture, and commands are
undecided. Nothing below asserts a fact about a codebase, because there
isn't one — the working practice is scaffolded first so the first
commit is already governed.

Two things must be filled in before the first feature lands, and until
they are, any claim that depends on them is unverifiable:

- `.claude/rules/verification.md` — the real commands (install,
  typecheck, lint, test, single-test, build, dev), **and any command
  that passes without checking anything.**
- `.claude/rules/architecture.md` — does not exist yet. Write it once
  there are layers to describe, and state the dependency direction as
  an invariant, not a convention.

## How this project is run

| Rule | Governs |
|---|---|
| `.claude/rules/operating-model.md` | what goes to the owner, and how |
| `.claude/rules/verification.md` | how a claim becomes evidence; the project's commands |
| `.claude/rules/release-choreography.md` | commit/push/deploy sequencing |

The orca-coordinator plugin runs the workers: who coordinates, who does
what, and the list of workstreams. Skill: `milestone-acceptance`
(close-out).

## Definition of done

Works correctly · is tested · handles its failure cases · fits the
product vision · **and the claim that it does has been checked by
someone other than whoever wrote it.**

<!-- Add the criteria this project actually has, and delete the ones it
     doesn't. A UI adds accessible and responsive; a library adds
     documented and semver-correct; a service adds observable and
     rollback-safe. A criterion nobody can check is decoration. -->
