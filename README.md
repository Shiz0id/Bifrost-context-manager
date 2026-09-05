# Bifrost

A context database for reverse-engineering projects, and the agent surface over it.

Long-running RE work accumulates two things that markdown handles badly: **a lot of small
verified facts**, and **the reasons one reading was chosen over another**. A roadmap document
grows past the point where anyone — person or agent — can hold it, and the moment it does,
status starts drifting from reality because nothing computes it.

Bifrost stores the facts, derives the status, and renders the document.

```bash
python -m bifrost digest              # whole-project state, ~1k tokens
python -m bifrost realm <format>      # status and why, fields, claims + citations, gates
python -m bifrost trace format <name> # what it gates, and what that blocks
python -m bifrost render              # regenerate the project's markdown
```

It ships an MCP server (nine typed tools) and four Claude Code hooks, so an agent starts a
session already oriented instead of loading a 200 KB file to answer a question a query answers
in hundreds of tokens.

---

## Two rules the schema is built on

**1. Current state is derived, never stored.** A format's status is a view over its gate runs;
a capability's status is a view over the formats gating it; staleness is computed from git.
Nothing a caller types can assert a status into existence:

> An agent may record `asserted_status = 'verified'`. `v_claim_status` downgrades it to
> **`asserted-unbacked`** unless something actually backs it — the code was read, an artefact
> was examined, a gate passes, or a discriminator separates it from its rival.

**2. History is append-only.** `claim`, `discriminator`, `refutation`, `tautology` and
`gate_run` describe a moment of discovery. They cannot drift, so they never need maintenance,
so agents write them freely. Only `todo`, `capability` and `edge` are mutable, and those carry
`review_state`.

The drift surface of the whole system is therefore about fifty rows. Everything else is
scanned, ingested, derived, or immutable.

### The entity that makes it worth having

A **discriminator** is the check that separates two candidate readings, with both results:

```
claim   the permutation table is the natural one
check   mean key-to-key angular step over the clone tree, 26,991 steps
  a     natural table: 9.50 deg mean, 0.89% of steps over 45
  b     cyclic  table: 15.45 deg mean, 6.0% over 45
```

A corpus gate says *this reading is self-consistent across the tree*. Only a discriminator says
*this reading and not that one*. In the project this was built for, four of five defects
corrected in one week had survived a gate reporting millions of verified values — because the
wrong reading satisfied the same invariant. `v_claim_risk` surfaces claims that have no
discriminator and no gate.

## Pointing it at a project

Bifrost knows nothing about any particular project. A project supplies
`<root>/.bifrost/profile.py`:

| Field | Drives |
| :--- | :--- |
| `NAME`, `DOCUMENT` | identity, and the markdown rendered from the database |
| `BUILDS` | the shipped builds and their symbol sources |
| `GATES` | probes, what they assert, and **which source files they exercise** — the last is what makes staleness computable |
| `FORMATS`, `CAPABILITIES` | the things being read, and what they unblock |
| `EXCEPTIONS` | named, justified non-passing files |
| `CLAIMS`, `TODOS` | seeded judgements |
| `ANCHOR_MAP` | document section → format, for the migration |
| `INVARIANTS` | what each gate asserts, and which claims that backs |
| `DISCRIMINATORS` | recorded checks that separate two readings |
| `SYMBOL_DB` | a symbol database to `ATTACH` read-only |

Every field is optional. A profile defining only `NAME` gets a working database with an empty
knowledge layer, which is what a new project looks like.

The root resolves from `--project`, then `$BIFROST_PROJECT`, then `$CLAUDE_PROJECT_DIR`, then
by walking up for a `.bifrost/` directory. **The database lives under the project's `build/`,
not here** — it is project state; Bifrost is only what reads and writes it.

```bash
export BIFROST_PROJECT=/path/to/project
python -m bifrost bootstrap    # scan, ingest symbols, register gates
python -m bifrost seed         # the profile's formats, capabilities, exceptions
python -m bifrost link         # attach claims to formats, link evidence
```

## The MCP surface

```
bifrost_digest            session orientation
bifrost_realm             everything about one subject
bifrost_query             one of the derived views
bifrost_symbol            resolve an address / struct field
bifrost_trace             closure over the graph
bifrost_record_run        a gate run, parsed from the probe's own stdout
bifrost_record_claim      a claim + citations  (REJECTED without a citation)
bifrost_record_evidence   discriminator | refutation | tautology
bifrost_propose           todo | capability | edge, landing as 'proposed'
```

**There is deliberately no `bifrost_review`.** Confirming proposals exists only in the CLI, so
an agent is structurally incapable of approving its own work. A test pins that.

## Hooks

| Hook | Job |
| :--- | :--- |
| `SessionStart` | Injects the digest as `additionalContext` |
| `PreToolUse` | Denies a hand-edit of a generated document, naming the row to change |
| `PostToolUse` | A gate ran → record it. If the output was not passed to the hook, it **asks** rather than guessing |
| `Stop` | A reader was edited and no gate exercising it has run since |

Every hook fails open. A hook that breaks a session because a database is missing is worse than
no hook. There is **no per-turn logging mandate** — that manufactures rows written to satisfy a
hook, which is fabricated data by another name.

Two things learned wiring these, both pinned by tests: hooks run with the *session's* cwd, not
the project root; and the MCP entry point must not depend on a working directory at all — a
relative `cwd` resolved elsewhere by the real client, the process died on `ModuleNotFoundError`,
and the client reported only `Connection closed`.

## Migrating an existing document

`bifrost migrate` splits a markdown document into blocks, extracts typed citation tokens,
validates struct layouts against a symbol database, and reports what is not yet accounted for.
The gate on the rewrite is **citation preservation** — prose may change, evidence may not:

```
for every paragraph p:
    citations(p)  ⊆  ⋃ citations(claims derived from p)
```

Any address, symbol, path or figure dropped in the rewrite fails the gate and names the
paragraph it was lost from. A retained *passage* satisfies it by construction, so verbatim
retention is the safe default and only claims are audited.

`migration_source` records the commit the document was ingested at, so `git show <sha>:<path>`
recovers the original forever — no attic directory, nothing to maintain.

One blind spot, reported rather than hidden: an assertion whose evidence sits in the
*neighbouring* block carries no citable token of its own. `migrate.orphan_assertions()` lists
them; `promote_passage()` turns one into a claim inheriting the nearest three cited blocks in
its section.

## Backing up the part that cannot be rebuilt

The database is a build product: scans, symbol tables and gate runs rebuild from the profile in
seconds. The **knowledge layer** does not — claims, citations, discriminators, refutations and
migrated passages are hand-written judgements, and recreating them means reading the source
document again and rewriting several hundred statements.

Since the database is binary and gitignored, that work would otherwise live in one file on one
machine.

```bash
python -m bifrost dump      # -> <project>/.bifrost/dump/*.jsonl, committable
python -m bifrost restore   # after bootstrap + seed, on a fresh database
```

Deterministic by construction and tested as such: byte-stable across runs, carrying no absolute
paths, and a restore reproduces the **derived views** — not just the row counts.

## Layout

```
bifrost/
  profile.py      what Bifrost is pointed at
  core.py         connection, migrations, provenance, the write path, git state
  ingest.py       symbol/tree scanners, gate registry, stdout parsers, bootstrap
  migrate.py      document split, citation extraction, the preservation gate
  render.py       emit the document, deterministically
  digest.py       the session digest
  seed.py         profile data -> rows
  reattach.py     claims -> the format they are about
  invariants.py   gate invariants -> the claims they back
  discriminators.py
  cli.py          the human surface (a superset of MCP)
  mcp_server.py   dependency-free JSON-RPC over stdio
  hooks.py        SessionStart / PreToolUse / PostToolUse / Stop
  dump.py         the knowledge layer as committable JSONL
  migrations/     numbered SQL, applied in order
  tests/          74 tests across five suites
bifrost_server.py cwd-independent MCP launcher
```

No dependencies beyond the standard library. `python -m bifrost test` runs everything.

## Status

Built for and in use on one project: a clean-room reverse-engineering of *Star Wars Battlefront
III* across its Xbox 360 and Wii builds. That corpus is 31 asset trees and 111,223 files, and
its 641-block roadmap migrated at 100% coverage with 822 of 822 citation tokens preserved.

The generic/project split is new, so the profile interface should be expected to move.
