-- Bifrost 001 — the initial schema.
--
-- The bridge to the corpus. Every fact about the BF3 360/Wii reverse-engineering
-- effort lives here; bf3_execution_roadmap.md is rendered from it.
--
-- Two design rules run through the whole thing, and both come out of AGENTS.md:
--
--   1. Current state is DERIVED, never stored.  A format's status is a view over
--      its gate runs; a capability's status is a view over the formats that gate
--      it. Nothing an agent types can assert a status into existence. This is
--      rule 7 ("status reflects verified behaviour, not intent") made structural.
--
--   2. History is APPEND-ONLY.  claim, discriminator, refutation, tautology and
--      gate_run describe a moment of discovery. They never drift, so they never
--      need maintenance, so agents may write them freely. Only todo / capability
--      / edge are mutable, and those carry review_state.
--
-- Provenance is the spine: rule 6 says every non-obvious constant names where it
-- came from, which is a NOT NULL foreign key. record_claim rejects an empty
-- citation set at the tool boundary rather than trusting convention.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- meta
-- ---------------------------------------------------------------------------

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- substrate — what exists on disc. Kept true by a filesystem scan, so it is
-- self-healing and never needs hand maintenance.
-- ---------------------------------------------------------------------------

CREATE TABLE build (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,      -- bf3_360, bf3_wii
    platform      TEXT NOT NULL,             -- xenon, broadway
    root_path     TEXT NOT NULL,
    exe_path      TEXT,
    image_base    INTEGER,                   -- 0x82000000 on the 360
    endianness    TEXT NOT NULL DEFAULT 'big',
    symbol_origin TEXT                       -- which `source.build_id` rows resolve here
);

CREATE TABLE tree (
    id            INTEGER PRIMARY KEY,
    build_id      INTEGER NOT NULL REFERENCES build(id),
    name          TEXT NOT NULL,             -- ob_xb_v184
    path          TEXT NOT NULL,
    version       INTEGER,                   -- from the asset-type table at 0x82A80FE0
    file_count    INTEGER,
    bytes         INTEGER,
    ext_histogram TEXT,                      -- json {".rax": 4990, ".tpf": 324}
    scanned_at    TEXT,
    UNIQUE (build_id, name)
);

-- The Wii has no prebuilt symbol database. 83 of the 277 distinct addresses the
-- roadmap cites are Broadway-range and currently resolve to nothing;
-- RABAZZ_full.map carries 3,107 symbols, which is what fills this.
CREATE TABLE wii_symbol (
    addr   INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'RABAZZ_full.map'
);

CREATE INDEX idx_wii_symbol_name ON wii_symbol(symbol);

-- Rows are created on demand: a file only earns one when a gate names it or a
-- claim cites it. 117k rows of inventory would be noise.
CREATE TABLE file (
    id       INTEGER PRIMARY KEY,
    tree_id  INTEGER NOT NULL REFERENCES tree(id),
    relpath  TEXT NOT NULL,
    bytes    INTEGER,
    notable  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tree_id, relpath)
);

-- ---------------------------------------------------------------------------
-- provenance — the spine. Every fact-bearing row cites at least one source.
-- ---------------------------------------------------------------------------

-- kind:
--   disasm_fn       a function in a retail executable, resolved to a symbol
--   data_addr       a constant pool / table / string blob. NOT a function --
--                   the 360 map holds functions and classes only, so 17% of the
--                   roadmap's addresses have no symbol and are correctly cited
--                   by meaning instead (pi/180 at 0x82068EE4, s_blendTable at
--                   0x8207235C). Conflating the two marks them permanently
--                   unresolved when they are perfectly well cited.
--   pdb_type        a struct in bf_gold.xenon.pdb, joinable to fields.offset
--   map_symbol      a name from a linker map
--   build_manifest  data/bf/buildfiles/... -- authoritative for converter choices
--   shipped_file    a .res / .x2t / .rax the claim was measured against
--   shipped_shader  data/shaders/xenon/... -- ships as named-uniform assembly
--   design_doc      TDD / FRD / Wii design overview
--   measurement     a corpus figure with no code reading behind it
--   cross_build     the 360 and Wii agreeing independently
CREATE TABLE source (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL,
    locator           TEXT NOT NULL,   -- "0x825ae690" | "obdef_s.numParts" | a path
    build_id          INTEGER REFERENCES build(id),
    symbol            TEXT,            -- resolved exactly
    containing_symbol TEXT,            -- resolved to inside a function...
    offset_in_symbol  INTEGER,         -- ...at this offset
    note              TEXT,
    UNIQUE (kind, locator, build_id)
);

CREATE INDEX idx_source_locator ON source(locator);
CREATE INDEX idx_source_symbol  ON source(symbol);

-- ---------------------------------------------------------------------------
-- knowledge — the formats being read out of the corpus
-- ---------------------------------------------------------------------------

CREATE TABLE format (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    tree_id        INTEGER REFERENCES tree(id),
    container_id   INTEGER REFERENCES format(id),  -- ob_xb_v184 is-a rax_container
    version        INTEGER,
    summary        TEXT,
    roadmap_anchor TEXT                            -- "6.9"
);

-- Where pdb_struct/pdb_field are set the offset is CHECKED against the symbol
-- database's own `fields` table. A disagreement is an error, not a comment.
CREATE TABLE format_field (
    id            INTEGER PRIMARY KEY,
    format_id     INTEGER NOT NULL REFERENCES format(id),
    struct_name   TEXT,
    offset        INTEGER NOT NULL,
    size          INTEGER,
    ctype         TEXT,
    name          TEXT NOT NULL,
    semantic      TEXT,
    pdb_struct    TEXT,
    pdb_field     TEXT,
    pdb_validated INTEGER,        -- NULL = not checkable, 1 = agrees, 0 = DISAGREES
    pdb_note      TEXT,
    source_id     INTEGER REFERENCES source(id)
);

CREATE INDEX idx_field_format ON format_field(format_id);

CREATE TABLE constant (
    id        INTEGER PRIMARY KEY,
    format_id INTEGER REFERENCES format(id),
    value     TEXT NOT NULL,
    meaning   TEXT NOT NULL,
    source_id INTEGER REFERENCES source(id)     -- rule 6: audit with WHERE source_id IS NULL
);

CREATE TABLE decoded_enum (
    id        INTEGER PRIMARY KEY,
    format_id INTEGER REFERENCES format(id),
    enum_name TEXT NOT NULL,
    member    TEXT NOT NULL,
    value     INTEGER,
    arm_addr  TEXT,
    behaviour TEXT,
    source_id INTEGER REFERENCES source(id)
);

-- ---------------------------------------------------------------------------
-- epistemics — append-only. These describe a moment of discovery and cannot
-- drift, which is why agents may write them without review.
-- ---------------------------------------------------------------------------

-- asserted_status is what an agent CLAIMS. The effective status is v_claim_status,
-- which downgrades an unbacked 'verified' to 'asserted-unbacked'. An agent cannot
-- declare something verified; only a gate or a discriminator can.
CREATE TABLE claim (
    id              INTEGER PRIMARY KEY,
    subject_type    TEXT NOT NULL,     -- format | format_field | tree | capability | gate | corpus
    subject_id      INTEGER,
    statement       TEXT NOT NULL,
    asserted_status TEXT NOT NULL DEFAULT 'plausible'
                    CHECK (asserted_status IN ('assumed','plausible','verified')),
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    roadmap_anchor  TEXT,
    para_id         INTEGER REFERENCES migration_para(id)
);

CREATE INDEX idx_claim_subject ON claim(subject_type, subject_id);

CREATE TABLE citation (
    claim_id  INTEGER NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source(id),
    note      TEXT,
    PRIMARY KEY (claim_id, source_id)
);

-- The project's central epistemic move, and the thing that separates a real
-- check from one a wrong reading also passes. "natural table 9.50 deg mean step
-- vs cyclic 15.45" is a row here, not prose.
CREATE TABLE discriminator (
    id          INTEGER PRIMARY KEY,
    claim_a     INTEGER NOT NULL REFERENCES claim(id),
    claim_b     INTEGER REFERENCES claim(id),   -- NULL when b is an unnamed control
    check_desc  TEXT NOT NULL,
    result_a    TEXT NOT NULL,
    result_b    TEXT NOT NULL,
    separation  TEXT,
    gate_run_id INTEGER REFERENCES gate_run(id),
    created_at  TEXT NOT NULL
);

-- Why the wrong reading was PLAUSIBLE is the expensive knowledge. Deleting it
-- means re-deriving it.
CREATE TABLE refutation (
    id             INTEGER PRIMARY KEY,
    refuted_claim  INTEGER NOT NULL REFERENCES claim(id),
    by_claim       INTEGER REFERENCES claim(id),
    why_plausible  TEXT,
    what_killed_it TEXT NOT NULL,
    source_id      INTEGER REFERENCES source(id),
    created_at     TEXT NOT NULL
);

-- A check that could not have failed. Flagged so it is never counted as
-- evidence again -- e.g. taking N, S and T from three rows of one rotation
-- matrix whose determinant is +1 by construction.
CREATE TABLE tautology (
    id                    INTEGER PRIMARY KEY,
    claim_id              INTEGER NOT NULL REFERENCES claim(id),
    why_it_could_not_fail TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    retired_at            TEXT
);

-- Narrative that is not an assertion about a format -- the method essay, the
-- "where the XEX paid for itself" argument. Rendered back out verbatim.
CREATE TABLE passage (
    id             INTEGER PRIMARY KEY,
    subject_type   TEXT NOT NULL,
    subject_id     INTEGER,
    ordinal        INTEGER NOT NULL,
    prose          TEXT NOT NULL,
    roadmap_anchor TEXT,
    para_id        INTEGER REFERENCES migration_para(id)
);

-- ---------------------------------------------------------------------------
-- verification
-- ---------------------------------------------------------------------------

CREATE TABLE gate (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,     -- corpus_mesh
    probe_path  TEXT,                     -- tools/probes/corpus_mesh.cpp
    exe_path    TEXT,                     -- build/corpus_mesh.exe
    description TEXT,
    trees       TEXT,                     -- json ["ob_xb_v184"]
    readers     TEXT                      -- json source paths it exercises -> staleness
);

-- "which verified claims have no gate?" is a LEFT JOIN over this.
CREATE TABLE gate_invariant (
    id        INTEGER PRIMARY KEY,
    gate_id   INTEGER NOT NULL REFERENCES gate(id),
    claim_id  INTEGER REFERENCES claim(id),
    assertion TEXT NOT NULL
);

-- stdout lives on disk; stdout_head keeps queries and the digest small.
CREATE TABLE gate_run (
    id          INTEGER PRIMARY KEY,
    gate_id     INTEGER NOT NULL REFERENCES gate(id),
    ts          TEXT NOT NULL,
    commit_sha  TEXT,
    tree_dirty  INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    pass        INTEGER,
    fail        INTEGER,
    refused     INTEGER,
    metrics     TEXT,                     -- json, parser-extracted
    stdout_path TEXT,
    stdout_head TEXT
);

CREATE INDEX idx_run_gate ON gate_run(gate_id, ts DESC);

CREATE TABLE exception (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,       -- "krayt stride contradiction"
    file_id   INTEGER REFERENCES file(id),
    disposition TEXT NOT NULL             -- reported | refused | defect_in_shipped_data
                CHECK (disposition IN ('reported','refused','defect_in_shipped_data')),
    rationale TEXT NOT NULL
);

CREATE TABLE exception_gate (
    exception_id INTEGER NOT NULL REFERENCES exception(id),
    gate_id      INTEGER NOT NULL REFERENCES gate(id),
    PRIMARY KEY (exception_id, gate_id)
);

-- ---------------------------------------------------------------------------
-- engine
-- ---------------------------------------------------------------------------

CREATE TABLE reader (
    id          INTEGER PRIMARY KEY,
    format_id   INTEGER REFERENCES format(id),
    class_name  TEXT NOT NULL,
    source_path TEXT,
    symbol      TEXT
);

CREATE TABLE test (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    source_path TEXT,
    claim_id    INTEGER REFERENCES claim(id),
    pins        TEXT
);

-- ---------------------------------------------------------------------------
-- work + graph
-- ---------------------------------------------------------------------------

-- A capability is neither a format nor a task: "wii_level_instantiation" is
-- gated by N formats and unlocks M todos. Without this node the closure query
-- has to hop mismatched types. Status is PURELY derived -- see v_capability.
CREATE TABLE capability (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE todo (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    rationale      TEXT,
    phase          TEXT,
    difficulty     TEXT,
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','in_progress','done','abandoned')),
    priority       INTEGER,
    review_state   TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (review_state IN ('proposed','confirmed','rejected')),
    proposed_by    TEXT,
    proposed_at    TEXT,
    confirmed_at   TEXT,
    roadmap_anchor TEXT
);

-- kind:
--   gates        format   -> capability   (a format the capability needs)
--   unlocks      capability -> todo
--   blocks       (derived, not written by hand -- see v_blocked)
--   verified_by  claim    -> gate
--   read_from    format   -> source
--   implements   reader   -> format
--   diverges_from format  -> format       (360 <-> Wii)
--   consumes     format   -> format
--   pins         test     -> claim
--   derived_from claim    -> claim
CREATE TABLE edge (
    id           INTEGER PRIMARY KEY,
    src_type     TEXT NOT NULL,
    src_id       INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    dst_type     TEXT NOT NULL,
    dst_id       INTEGER NOT NULL,
    note         TEXT,
    review_state TEXT NOT NULL DEFAULT 'confirmed'
                 CHECK (review_state IN ('proposed','confirmed','rejected')),
    proposed_by  TEXT,
    proposed_at  TEXT,
    confirmed_at TEXT,
    UNIQUE (src_type, src_id, kind, dst_type, dst_id)
);

CREATE INDEX idx_edge_src ON edge(src_type, src_id, kind);
CREATE INDEX idx_edge_dst ON edge(dst_type, dst_id, kind);

-- ---------------------------------------------------------------------------
-- git state — populated by the staleness resolver. Makes "is this gate run
-- current?" computable rather than declared.
-- ---------------------------------------------------------------------------

CREATE TABLE git_state (
    path           TEXT PRIMARY KEY,
    last_commit    TEXT,
    last_commit_ts TEXT,
    dirty          INTEGER NOT NULL DEFAULT 0,
    scanned_at     TEXT
);

CREATE TABLE git_ancestry (            -- commit -> its ordinal in first-parent order
    commit_sha TEXT PRIMARY KEY,
    ordinal    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- migration — the roadmap is rewritten into rows and the original discarded.
-- The original is recoverable from git via migration_source.commit_sha, which
-- is what keeps "did the rewrite lose anything?" answerable for free.
-- ---------------------------------------------------------------------------

CREATE TABLE migration_source (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    commit_sha  TEXT NOT NULL,
    note        TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE (path, commit_sha)
);

-- One row per paragraph of the source document. `citation_tokens` is the set of
-- addresses, symbols, paths and figures the paragraph carried; the coverage gate
-- asserts that every one of them survives into some claim derived from it.
-- Prose may be rewritten; evidence may not be dropped.
CREATE TABLE migration_para (
    id                 INTEGER PRIMARY KEY,
    migration_source_id INTEGER NOT NULL REFERENCES migration_source(id),
    ordinal            INTEGER NOT NULL,
    text               TEXT NOT NULL,
    citation_tokens    TEXT NOT NULL DEFAULT '[]',   -- json
    accounted          INTEGER NOT NULL DEFAULT 0,
    accounted_kind     TEXT,        -- claim | passage | table | code | heading | skipped
    UNIQUE (migration_source_id, ordinal)
);

-- ---------------------------------------------------------------------------
-- rules & requirements — AGENTS.md stays hand-written and authoritative for
-- itself; a copy lives here so dispositions can reference it.
-- ---------------------------------------------------------------------------

CREATE TABLE rule (
    id     INTEGER PRIMARY KEY,
    doc    TEXT NOT NULL,
    number TEXT NOT NULL,
    text   TEXT NOT NULL,
    UNIQUE (doc, number)
);

CREATE TABLE design_requirement (
    id      INTEGER PRIMARY KEY,
    doc     TEXT NOT NULL,
    section TEXT,
    text    TEXT NOT NULL
);
