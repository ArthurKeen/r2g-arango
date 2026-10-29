# Agent / contributor guide

Working rules for anyone — human or agent — changing this repo.

## Engineering rules

<!-- shared-engineering-rules -->
Shared across the Arango tooling estate. Agents that read `AGENTS.md` (Claude
Code, Codex) get these; Cursor reads `.cursor/rules/` instead, so where this repo
also has `.cursor/rules/*.mdc` covering the same ground, **both copies are
tracked — change a rule in both places.**

### Read before write
Match the codebase, don't fight it. Before writing anything, search for how it is
already done: existing patterns, utilities, similar types, endpoint shapes, test
helpers, error conventions. If a helper exists, call it — don't write a second
one. Reuse the established naming, file organisation, error handling and logging
style. Your job is to make the codebase *more* consistent, not less.

### Surface, don't guess
A wrong guess implemented is worse than an honest question asked. Try to resolve
uncertainty yourself first (codebase, docs, tests). If it is still unclear —
ambiguous requirements, several valid approaches, a breaking change, an
architectural choice, unclear scope — state what you understand, what is
unclear, and the options with trade-offs, then wait. Confidence is not a
substitute for the user's knowledge.

### Incremental over atomic
Small steps that individually work beat large steps that eventually work. Each
increment must be verifiable, reversible, reviewable in under five minutes, and
deployable on its own. At every checkpoint the tree must compile, pass tests and
be functional. Refactor by adding alongside, migrating callers one at a time,
then removing the old. Warning signs: more than ~5 files at once, an hour with no
commit, or a change you cannot describe in one sentence — slice smaller.

### Test what you touch
Changed code means changed behaviour, and behaviour needs a test. New function →
unit test. New endpoint → integration test. Bug fix → a regression test that
would have caught it. Refactor → existing tests must pass, and if they don't
cover the path, add coverage *first*. For every file you modify, check there is a
test file, that it covers what you changed, and that you actually ran it. Tests
must be deterministic, fast, isolated, clearly named, and assert behaviour rather
than absence of a crash. "I tested manually" and "I'll add tests later" are not
acceptable.

### Verify before claiming done
If you didn't run it, you didn't ship it. Before saying work is complete: build
it, test it, actually exercise the changed path, and look at the output. Frontend:
`npm run type-check`, `lint`, `test`, `build`. Backend: the test suite, `ruff`,
`mypy`. Claiming "I've implemented X" without verifying X works is not a mistake,
it's a false statement.

### Comprehensiveness over simplification
This is production software; simplification is the enemy of completeness. Every
change must address error handling (no swallowed errors — every handler does
something, with messages saying what failed, with what input, and why), edge
cases (empty/nil, boundaries, invalid input, unicode, concurrency), configuration
rather than hardcoding for anything that could differ across environments, test
completeness, observability (structured logging, metrics, health), security
(validate at boundaries, no secrets in code or logs, authz checks), documentation,
UI states (loading / error / empty / success, plus accessibility), data integrity
(validation, constraints, transactions, idempotency) and performance (pagination,
caching, indexing). Never ship happy-path-only, empty catch blocks, magic numbers,
copy-paste divergence, or `console.log` debugging.

### Wiring over deletion
Unused code usually means a missing feature, not garbage. A linter warning about
an unused import, variable or parameter is a request to *finish the
implementation*, not to delete it. Do not delete "unused" code without proving it
is genuinely obsolete. An unused `ctx` gets passed down; an unused `err` gets
handled; an unused prop gets wired to rendering or logic; a `useEffect` missing
dependencies gets a stabilised dependency via `useCallback`/`useMemo` — never an
`eslint-disable`. Anything added in the previous turn is mandatory to use. A
passing lint check on a broken feature is a failure.

### Modularity and structure
Everything has a place. Size limits: source 1500 lines, tests 2000, config 500,
docs 1000 — past that, split. Split also when you cannot find things without
searching or the file holds unrelated concerns. Placement: shared types in the
core/types module, tests beside their source (except E2E), configs in `configs/`,
scripts in `scripts/` — not the repo root. Each module should have one
responsibility, a minimal public API, hidden internals, and be independently
testable. A new developer should be able to find any file from what it does,
without searching.

### Mock fidelity
A test that passes against a wrong-signature mock is a test that didn't run.
Before mocking any production symbol, open the real declaration and read it; the
mock's constructor args, method signatures and field types must mirror the real
ones so a signature change breaks the test. Never infer a mock's shape from how
the test reads it, and never use `any` to make a mock "flexible" — that disables
the safety net. This rule exists because of a real bug: a mocked `ApiError` took
`(status, message)` while the real class takes `(status, body)`; every test was
internally consistent, CI was green, and the error-handling assertions proved
nothing. Jest specifics are in `frontend/AGENTS.md`.

### Checkpoint regularly
Commit early, push often; large uncommitted changesets are disasters waiting to
happen. Commit when a feature is complete, when tests pass, before a risky
refactor, before switching tasks, and before ending a session. One logical change
per commit — if you can't describe it in one line it's too big. Use scoped,
descriptive messages (`fix(curation): atomic reparent endpoint`), not "updates" or
"wip".
