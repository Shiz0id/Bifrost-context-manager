-- Bifrost 003 — four corrections, all found by rendering the first real digest.
--
-- Every one of them made the digest say something untrue, which is the failure
-- this whole system exists to prevent. Recorded here rather than edited into 002
-- because 001 and 002 have been applied to a live database.

-- ---------------------------------------------------------------------------
-- 1. An exception accounts for a NUMBER of failures, not for one.
--
-- The first cut counted exception ROWS against a gate's fail count, so
-- corpus_wii_ob -- 4 failures, all of them the single "Wii cloth parts are
-- simulated" exception -- came out with 2 unexplained failures and marked
-- ob_wi_v194 as failing. One exception can cover many files.
-- ---------------------------------------------------------------------------

ALTER TABLE exception_gate ADD COLUMN expected_failures INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- 2. A gate that has never run is not a FAILING gate.
--
-- v_format_status used COALESCE(gl.ok, 0) > 0, so a NULL from a gate with no
-- runs read as a failure and x2t -- 31,102 files, decoding since 3 September --
-- was reported as failing because corpus_tex had not been run this session.
-- "Not measured" and "measured and bad" are different states and the digest has
-- to say which.
--
-- 3. Stale is a freshness problem, not a capability problem.
--
-- v_capability counted a stale format as not ready, so every capability whose
-- formats had been gated against a dirty working tree came out blocked. A stale
-- gate passed when it last ran; what is unknown is whether it still would. That
-- belongs in STALE GATES, where the digest already reports it, not in BLOCKED.
-- ---------------------------------------------------------------------------

DROP VIEW v_blocked;
DROP VIEW v_capability;
DROP VIEW v_format_status;

CREATE VIEW v_format_status AS
SELECT f.id, f.name, f.roadmap_anchor, t.name AS tree, b.name AS build,
       (SELECT COUNT(*) FROM gate g, json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name)                                   AS n_gates,
       (SELECT COUNT(*) FROM gate g
          JOIN v_gate_latest gl ON gl.gate_id = g.id,
               json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name AND gl.ok = 1)                     AS n_passing,
       (SELECT COUNT(*) FROM gate g
          JOIN v_gate_latest gl ON gl.gate_id = g.id,
               json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name AND gl.run_id IS NOT NULL AND gl.ok = 0) AS n_failing,
       (SELECT COUNT(*) FROM gate g
          JOIN v_gate_stale gs ON gs.gate_id = g.id,
               json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name AND gs.stale = 1)                  AS n_stale,
       CASE
         -- no gate names this format's tree at all
         WHEN (SELECT COUNT(*) FROM gate g, json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name) = 0                            THEN 'ungated'
         -- a gate RAN and came back bad
         WHEN (SELECT COUNT(*) FROM gate g
                 JOIN v_gate_latest gl ON gl.gate_id = g.id,
                      json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name AND gl.run_id IS NOT NULL AND gl.ok = 0) > 0
                                                                        THEN 'failing'
         -- every gate that names it exists but none has ever been run
         WHEN (SELECT COUNT(*) FROM gate g
                 JOIN v_gate_latest gl ON gl.gate_id = g.id,
                      json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name AND gl.run_id IS NOT NULL) = 0  THEN 'unmeasured'
         WHEN (SELECT COUNT(*) FROM gate g
                 JOIN v_gate_stale gs ON gs.gate_id = g.id,
                      json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name AND gs.stale = 1) > 0           THEN 'stale'
         ELSE 'verified'
       END AS status
FROM format f
LEFT JOIN tree  t ON t.id = f.tree_id
LEFT JOIN build b ON b.id = t.build_id;

-- A format counts as READY for a capability when it has been measured and was
-- good the last time it ran. 'stale' qualifies; 'ungated', 'unmeasured' and
-- 'failing' do not.
CREATE VIEW v_format_ready AS
SELECT id, name, status,
       CASE WHEN status IN ('verified','stale') THEN 1 ELSE 0 END AS ready
FROM v_format_status;

CREATE VIEW v_capability AS
SELECT c.id, c.name, c.description,
       (SELECT COUNT(*) FROM edge e
         WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
           AND e.review_state='confirmed')                                  AS n_gating,
       (SELECT COUNT(*) FROM edge e
          JOIN v_format_ready fr ON fr.id = e.src_id
         WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
           AND e.src_type='format' AND e.review_state='confirmed'
           AND fr.ready = 1)                                                AS n_ready,
       CASE
         WHEN (SELECT COUNT(*) FROM edge e
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.review_state='confirmed') = 0                       THEN 'ungated'
         WHEN (SELECT COUNT(*) FROM edge e
                 JOIN v_format_ready fr ON fr.id = e.src_id
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.src_type='format' AND e.review_state='confirmed'
                  AND fr.ready = 1)
              = (SELECT COUNT(*) FROM edge e
                  WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                    AND e.review_state='confirmed')                         THEN 'available'
         WHEN (SELECT COUNT(*) FROM edge e
                 JOIN v_format_ready fr ON fr.id = e.src_id
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.src_type='format' AND e.review_state='confirmed'
                  AND fr.ready = 1) = 0                                     THEN 'blocked'
         ELSE 'partial'
       END AS status
FROM capability c;

CREATE VIEW v_blocked AS
SELECT cap.name AS capability, cap.status AS capability_status,
       cap.n_ready, cap.n_gating,
       f.name AS blocking_format, fs.status AS format_status,
       f.roadmap_anchor, t.name AS tree, t.file_count
FROM v_capability cap
JOIN edge e         ON e.kind='gates' AND e.dst_type='capability' AND e.dst_id=cap.id
                   AND e.src_type='format' AND e.review_state='confirmed'
JOIN format f       ON f.id = e.src_id
JOIN v_format_ready fs ON fs.id = f.id
LEFT JOIN tree t    ON t.id = f.tree_id
WHERE cap.status IN ('blocked','partial')
  AND fs.ready = 0;
