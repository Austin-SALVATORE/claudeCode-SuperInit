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

## How this project is built

| Rule | Governs |
|---|---|
| `.claude/rules/team-roles.md` | who owns what, what each role must never do, model policy, spawn mechanics |
| `.claude/rules/verification.md` | how a claim becomes evidence; the project's commands |
| `.claude/rules/release-choreography.md` | commit/push/deploy sequencing |

Skills: `teammate-brief` (handoffs), `milestone-acceptance` (close-out).
Bench: `architect`, `dev`, `qa` (teammates) · `claim-verifier`,
`git-op`, `repo-inventory` (subagents).

The short version: **design, implementation, and verification are held
by different roles.** The lead reads, judges, and briefs; it does not
write product code and does not mutate git. Anything irreversible or
outward-facing needs the owner's approval first.

## Working process

Before coding: understand the requirement, check existing architecture,
prefer improving an existing component over adding a near-duplicate.

After coding: run lint/tests/build, remove dead code, and report
numbers rather than adjectives — `68 files, 692 passed`, not "green".

## Definition of done

Works correctly · is tested · handles its failure cases · fits the
product vision · **and the claim that it does has been checked by
someone other than whoever wrote it.**

<!-- Add the criteria this project actually has, and delete the ones it
     doesn't. A UI adds accessible and responsive; a library adds
     documented and semver-correct; a service adds observable and
     rollback-safe. A criterion nobody can check is decoration. -->

## Conventions

- Immutable data: return new objects, never mutate in place.
- Many small focused files over few large ones.
- Validate at system boundaries; never trust external data.
- No placeholder implementations, no hardcoded config values.
- Commit format: `<type>: <description>` (feat, fix, refactor, docs,
  test, chore, perf, ci).
