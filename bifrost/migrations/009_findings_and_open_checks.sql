-- Bifrost 009 — the three states of a gate run, and the check nobody has run yet.
--
-- gate_run.finding
--   `ok` is binary, and the interesting state is a third thing: STRUCTURALLY
--   PASSED, SUBSTANTIVELY FAILED. corpus_wii_anim is the standing example. It
--   reads `ok  corpus_wii_anim  4694 pass  0 fail`, which a cold session takes
--   as an unqualified success -- while the actual result of running it is that
--   16.6% of re-encoded rotation channels disagree with the same animation in
--   the 360 tree. The gate passed on its own terms and found something anyway.
--
--   That was invisible at digest level, and the digest is trusted precisely
--   because people skip reading anything else, so it was the most misleading
--   row in the database. `note` (008) does not fix it: a note is commentary
--   that the digest has no reason to surface. A finding is a verdict qualifier
--   and the digest renders it.
--
-- gate_run.todo_id
--   A run that finds something usually bears on open work, and nothing
--   connected the two. There is open work on Wii animation and the finding
--   above belongs against it.
--
-- discriminator, rebuilt
--   result_a and result_b were NOT NULL, so the schema could only hold a check
--   that had ALREADY been run. But the moment you most want to capture a
--   discriminator is the moment you realise one is needed -- when you have the
--   observation, the rival readings and a cheap decisive check, and no results
--   at all, because not having run it is the whole point. That moment had no
--   verb, so the single most valuable output of a gate -- a specific next check
--   -- went into a run's prose where nothing would ever surface it.
--
--   A discriminator now has a lifecycle. Proposed, with what each reading
--   PREDICTS, becomes run, with what each reading GAVE. Same row: the link from
--   the moment of realisation to the answer is the part worth keeping.

ALTER TABLE gate_run ADD COLUMN finding TEXT;
ALTER TABLE gate_run ADD COLUMN todo_id INTEGER REFERENCES todo(id);

CREATE INDEX idx_gate_run_todo ON gate_run(todo_id);

-- sqlite cannot drop a NOT NULL, so the table is rebuilt. Column order and
-- names are preserved for the dump, which round-trips this table.
CREATE TABLE discriminator_new (
    id           INTEGER PRIMARY KEY,
    claim_a      INTEGER NOT NULL REFERENCES claim(id),
    claim_b      INTEGER REFERENCES claim(id),   -- NULL when b is an unnamed control
    check_desc   TEXT NOT NULL,
    result_a     TEXT,                            -- NULL until the check is run
    result_b     TEXT,
    separation   TEXT,
    gate_run_id  INTEGER REFERENCES gate_run(id),
    created_at   TEXT NOT NULL,
    -- What each reading says the check WILL give. This is what makes a proposed
    -- discriminator worth recording rather than a to-do item: a check whose two
    -- predictions are the same separates nothing.
    predicted_a  TEXT,
    predicted_b  TEXT,
    why_decisive TEXT,
    proposed_by  TEXT,
    proposed_at  TEXT
);

INSERT INTO discriminator_new
    (id, claim_a, claim_b, check_desc, result_a, result_b, separation, gate_run_id, created_at)
SELECT id, claim_a, claim_b, check_desc, result_a, result_b, separation, gate_run_id, created_at
FROM discriminator;

DROP TABLE discriminator;

-- legacy_alter_table, and it has to be on for this one statement. Modern sqlite
-- re-parses every view on a RENAME, and four of them (v_claim_backing,
-- v_claim_status, v_claim_risk, v_claim_measurement_only) name `discriminator`
-- -- which does not exist between the DROP above and the RENAME below. Without
-- this the rename fails with "error in view v_claim_measurement_only", leaving
-- the table dropped, the replacement unrenamed, and the database with no
-- discriminator table at all. The views are correct again the moment the rename
-- lands; it is only the check that cannot see that.
PRAGMA legacy_alter_table = ON;
ALTER TABLE discriminator_new RENAME TO discriminator;
PRAGMA legacy_alter_table = OFF;

-- Run or not. A discriminator with no result_a has not been run; nothing else
-- distinguishes them, so nothing can drift out of step.
CREATE VIEW v_discriminator_open AS
SELECT d.id, d.claim_a, d.claim_b, d.check_desc, d.predicted_a, d.predicted_b,
       d.why_decisive, d.gate_run_id, d.proposed_by, d.proposed_at,
       ca.statement AS statement_a,
       cb.statement AS statement_b
FROM discriminator d
JOIN claim ca ON ca.id = d.claim_a
LEFT JOIN claim cb ON cb.id = d.claim_b
WHERE d.result_a IS NULL
ORDER BY d.id;

-- probe_path and exe_path were in `gate` but in no view, so finding out which
-- file a gate actually runs meant grepping the project's profile. corpus_m0v's
-- gate is the Python script, not the C++ probe of the same name, and that cost
-- a build and a run of the wrong one.
DROP VIEW IF EXISTS v_gate_latest;
CREATE VIEW v_gate_latest AS
SELECT g.id AS gate_id, g.name, g.probe_path, g.exe_path, g.readers, g.trees,
       r.id AS run_id, r.ts, r.commit_sha, r.tree_dirty,
       r.ok, r.pass, r.fail, r.refused, r.metrics, r.note, r.finding, r.todo_id
FROM gate g
LEFT JOIN gate_run r ON r.id = (
    SELECT id FROM gate_run
     WHERE gate_id = g.id
       AND id NOT IN (SELECT supersedes FROM gate_run WHERE supersedes IS NOT NULL)
     ORDER BY ts DESC, id DESC LIMIT 1
);
