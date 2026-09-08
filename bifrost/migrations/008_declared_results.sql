-- Bifrost 008 — a mis-parse is a typo, not a measurement; and where the
-- interpretation goes.
--
-- Two columns, both answering failures of the ingest path rather than of any
-- probe.
--
-- supersedes
--   Append-only is right for evidence: nobody should be able to quietly unsay a
--   measurement. But run 32 of this database was never a measurement -- it was
--   `PARSED OK: 1387   FAILED: 0` read backwards, binding 1387 to fail and
--   turning a clean gate red. The correction had to be written as run 33, and
--   the digest then listed both, so the cold reader saw a failing gate that had
--   passed.
--
--   The new row points BACK at the one it replaces. Nothing is updated and
--   nothing is deleted -- the bad row keeps its evidence value as a record of
--   what the parser did -- but the views stop counting it as this gate's state.
--   A run is superseded iff some other run names it, which is why there is no
--   `superseded_by` column to keep in step.
--
-- note
--   stdout was doing two jobs. The bimodality analysis on corpus_wii_anim and
--   the root-mismatch note on corpus_res are both in the probe's log because
--   there was nowhere else to put them -- but a probe did not write them, a
--   person did, and afterwards nothing separates what was measured from what
--   was concluded. That distinction is the one the rest of this schema is
--   fanatical about. stdout_path stays the probe's own bytes; commentary goes
--   here, and a conclusion worth keeping becomes a claim with a citation.

ALTER TABLE gate_run ADD COLUMN supersedes INTEGER REFERENCES gate_run(id);
ALTER TABLE gate_run ADD COLUMN note TEXT;

CREATE INDEX idx_gate_run_supersedes ON gate_run(supersedes);

-- A run that some later run supersedes is not this gate's current state.
DROP VIEW IF EXISTS v_gate_latest;
CREATE VIEW v_gate_latest AS
SELECT g.id AS gate_id, g.name, g.readers, g.trees,
       r.id AS run_id, r.ts, r.commit_sha, r.tree_dirty,
       r.ok, r.pass, r.fail, r.refused, r.metrics, r.note
FROM gate g
LEFT JOIN gate_run r ON r.id = (
    SELECT id FROM gate_run
     WHERE gate_id = g.id
       AND id NOT IN (SELECT supersedes FROM gate_run WHERE supersedes IS NOT NULL)
     ORDER BY ts DESC, id DESC LIMIT 1
);
