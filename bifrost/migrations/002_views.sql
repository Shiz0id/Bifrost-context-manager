-- Bifrost 002 — derived state.
--
-- Everything an agent reads as "current status" is computed here. Nothing in
-- this file can be written to, which is the point: rule 7 says status reflects
-- verified behaviour rather than intent, and the only way to guarantee that is
-- to make intent structurally incapable of setting it.

-- ---------------------------------------------------------------------------
-- gate freshness
-- ---------------------------------------------------------------------------

CREATE VIEW v_gate_latest AS
SELECT g.id AS gate_id, g.name, g.readers, g.trees,
       r.id AS run_id, r.ts, r.commit_sha, r.tree_dirty,
       r.ok, r.pass, r.fail, r.refused, r.metrics
FROM gate g
LEFT JOIN gate_run r ON r.id = (
    SELECT id FROM gate_run WHERE gate_id = g.id ORDER BY ts DESC, id DESC LIMIT 1
);

-- A gate run is stale when a reader it exercises changed after the run, or when
-- it was taken against a dirty tree, or when it never ran at all.
--
-- git_ancestry.ordinal is 0 at HEAD and increases into the past, so a SMALLER
-- ordinal is a NEWER commit. The reader is newer than the run -> the run is stale.
CREATE VIEW v_gate_stale AS
SELECT l.gate_id,
       l.name,
       l.run_id,
       l.ts,
       CASE
         WHEN l.run_id IS NULL                          THEN 1
         WHEN l.tree_dirty = 1                          THEN 1
         WHEN EXISTS (
              SELECT 1
              FROM json_each(COALESCE(l.readers, '[]')) je
              JOIN git_state gs ON gs.path = je.value
              WHERE gs.dirty = 1
                 OR (
                      l.commit_sha IS NOT NULL
                      AND (SELECT ordinal FROM git_ancestry WHERE commit_sha = gs.last_commit)
                          <
                          (SELECT ordinal FROM git_ancestry WHERE commit_sha = l.commit_sha)
                    )
         )                                              THEN 1
         ELSE 0
       END AS stale,
       CASE
         WHEN l.run_id IS NULL      THEN 'never run'
         WHEN l.tree_dirty = 1      THEN 'run against a dirty working tree'
         ELSE (
            SELECT group_concat(je.value, ', ')
            FROM json_each(COALESCE(l.readers, '[]')) je
            JOIN git_state gs ON gs.path = je.value
            WHERE gs.dirty = 1
               OR (
                    l.commit_sha IS NOT NULL
                    AND (SELECT ordinal FROM git_ancestry WHERE commit_sha = gs.last_commit)
                        <
                        (SELECT ordinal FROM git_ancestry WHERE commit_sha = l.commit_sha)
                  )
         )
       END AS reason
FROM v_gate_latest l;

-- ---------------------------------------------------------------------------
-- claim status — the honest downgrade
-- ---------------------------------------------------------------------------

-- What actually backs a claim, as opposed to what an agent asserted about it.
CREATE VIEW v_claim_backing AS
SELECT c.id AS claim_id,
       (SELECT COUNT(*) FROM citation      WHERE claim_id = c.id)                       AS n_citations,
       (SELECT COUNT(*) FROM discriminator WHERE claim_a  = c.id OR claim_b = c.id)     AS n_discriminators,
       (SELECT COUNT(*) FROM gate_invariant gi
          JOIN v_gate_latest gl ON gl.gate_id = gi.gate_id
         WHERE gi.claim_id = c.id AND gl.ok = 1)                                        AS n_passing_gates,
       (SELECT COUNT(*) FROM gate_invariant WHERE claim_id = c.id)                      AS n_gates,
       (SELECT COUNT(*) FROM refutation    WHERE refuted_claim = c.id)                  AS n_refutations,
       (SELECT COUNT(*) FROM tautology     WHERE claim_id = c.id AND retired_at IS NULL) AS n_tautologies
FROM claim c;

-- effective_status is what agents should read. An agent may assert 'verified';
-- only a passing gate invariant or a discriminator can make it stick.
CREATE VIEW v_claim_status AS
SELECT c.id,
       c.subject_type, c.subject_id, c.statement, c.asserted_status,
       c.roadmap_anchor, c.created_at, c.created_by,
       b.n_citations, b.n_discriminators, b.n_passing_gates, b.n_gates,
       b.n_refutations, b.n_tautologies,
       CASE
         WHEN b.n_refutations > 0                                      THEN 'refuted'
         WHEN c.asserted_status = 'verified'
              AND (b.n_passing_gates > 0 OR b.n_discriminators > 0)    THEN 'verified'
         WHEN c.asserted_status = 'verified'                           THEN 'asserted-unbacked'
         ELSE c.asserted_status
       END AS effective_status
FROM claim c
JOIN v_claim_backing b ON b.claim_id = c.id;

-- The standing audit. Every row here is a place the project believes something
-- that nothing has tested -- which is where four of the five defects corrected
-- in the week of 2-4 Sep 2026 were living.
--
-- Severity matters because two very different things land here. "Asserted
-- verified with nothing behind it" is an epistemic failure. "Verified by a
-- discriminator but never run over the whole corpus" is rule 4 exposure -- real,
-- but a different order of problem, and the digest triages on this column.
CREATE VIEW v_claim_risk AS
SELECT s.id, s.subject_type, s.subject_id, s.statement, s.effective_status,
       s.roadmap_anchor,
       CASE
         WHEN s.n_refutations > 0                       THEN 'refuted'
         WHEN s.n_tautologies > 0                       THEN 'backed only by a tautology'
         WHEN s.effective_status = 'asserted-unbacked'  THEN 'asserted verified with no gate and no discriminator'
         WHEN s.n_citations = 0                         THEN 'no citation'
         WHEN s.effective_status = 'assumed'            THEN 'assumed, never discriminated'
         WHEN s.n_gates = 0                             THEN 'no gate invariant'
         ELSE NULL
       END AS risk,
       CASE
         WHEN s.n_refutations > 0                       THEN 'high'
         WHEN s.n_tautologies > 0                       THEN 'high'
         WHEN s.effective_status = 'asserted-unbacked'  THEN 'high'
         WHEN s.n_citations = 0                         THEN 'high'
         WHEN s.effective_status = 'assumed'            THEN 'medium'
         WHEN s.n_gates = 0                             THEN 'low'
         ELSE NULL
       END AS severity
FROM v_claim_status s
WHERE risk IS NOT NULL;

-- ---------------------------------------------------------------------------
-- format and capability status — both purely derived
-- ---------------------------------------------------------------------------

CREATE VIEW v_format_status AS
SELECT f.id, f.name, f.roadmap_anchor, t.name AS tree, b.name AS build,
       (SELECT COUNT(*) FROM gate g, json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name)                                   AS n_gates,
       (SELECT COUNT(*) FROM gate g
          JOIN v_gate_latest gl ON gl.gate_id = g.id,
               json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name AND gl.ok = 1)                     AS n_passing,
       (SELECT COUNT(*) FROM gate g
          JOIN v_gate_stale gs ON gs.gate_id = g.id,
               json_each(COALESCE(g.trees,'[]')) je
         WHERE je.value = t.name AND gs.stale = 1)                  AS n_stale,
       CASE
         WHEN (SELECT COUNT(*) FROM gate g, json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name) = 0                        THEN 'ungated'
         WHEN (SELECT COUNT(*) FROM gate g
                 JOIN v_gate_latest gl ON gl.gate_id = g.id,
                      json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name AND COALESCE(gl.ok,0) = 0) > 0 THEN 'failing'
         WHEN (SELECT COUNT(*) FROM gate g
                 JOIN v_gate_stale gs ON gs.gate_id = g.id,
                      json_each(COALESCE(g.trees,'[]')) je
                WHERE je.value = t.name AND gs.stale = 1) > 0       THEN 'stale'
         ELSE 'verified'
       END AS status
FROM format f
LEFT JOIN tree  t ON t.id = f.tree_id
LEFT JOIN build b ON b.id = t.build_id;

-- A capability is blocked when any format gating it is not verified.
CREATE VIEW v_capability AS
SELECT c.id, c.name, c.description,
       (SELECT COUNT(*) FROM edge e
         WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
           AND e.review_state='confirmed')                                  AS n_gating,
       (SELECT COUNT(*) FROM edge e
          JOIN v_format_status fs ON fs.id = e.src_id
         WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
           AND e.src_type='format' AND e.review_state='confirmed'
           AND fs.status = 'verified')                                      AS n_ready,
       CASE
         WHEN (SELECT COUNT(*) FROM edge e
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.review_state='confirmed') = 0                       THEN 'ungated'
         WHEN (SELECT COUNT(*) FROM edge e
                 JOIN v_format_status fs ON fs.id = e.src_id
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.src_type='format' AND e.review_state='confirmed'
                  AND fs.status = 'verified')
              = (SELECT COUNT(*) FROM edge e
                  WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                    AND e.review_state='confirmed')                         THEN 'available'
         WHEN (SELECT COUNT(*) FROM edge e
                 JOIN v_format_status fs ON fs.id = e.src_id
                WHERE e.kind='gates' AND e.dst_type='capability' AND e.dst_id=c.id
                  AND e.src_type='format' AND e.review_state='confirmed'
                  AND fs.status = 'verified') = 0                           THEN 'blocked'
         ELSE 'partial'
       END AS status
FROM capability c;

-- What blocks what, with the blocking format named. This is the query that cost
-- a whole session to answer by hand on 4 Sep 2026.
CREATE VIEW v_blocked AS
SELECT cap.name AS capability, cap.status AS capability_status,
       f.name AS blocking_format, fs.status AS format_status,
       f.roadmap_anchor,
       t.name AS tree, t.file_count
FROM v_capability cap
JOIN edge e         ON e.kind='gates' AND e.dst_type='capability' AND e.dst_id=cap.id
                   AND e.src_type='format' AND e.review_state='confirmed'
JOIN format f       ON f.id = e.src_id
JOIN v_format_status fs ON fs.id = f.id
LEFT JOIN tree t    ON t.id = f.tree_id
WHERE cap.status IN ('blocked','partial')
  AND fs.status <> 'verified';

-- ---------------------------------------------------------------------------
-- standing audits
-- ---------------------------------------------------------------------------

-- Rule 6: every non-obvious constant names where it came from.
CREATE VIEW v_constant_unsourced AS
SELECT k.id, k.value, k.meaning, f.name AS format
FROM constant k LEFT JOIN format f ON f.id = k.format_id
WHERE k.source_id IS NULL;

-- A recorded field offset that disagrees with the retail PDB. Build-breaking.
CREATE VIEW v_field_pdb_conflict AS
SELECT ff.id, f.name AS format, ff.struct_name, ff.name, ff.offset,
       ff.pdb_struct, ff.pdb_field, ff.pdb_note
FROM format_field ff JOIN format f ON f.id = ff.format_id
WHERE ff.pdb_validated = 0;

-- Rule 1 exposure: a claim resting only on measurement, with no code read.
CREATE VIEW v_claim_measurement_only AS
SELECT s.id, s.statement, s.effective_status, s.roadmap_anchor
FROM v_claim_status s
WHERE s.n_citations > 0
  AND NOT EXISTS (
        SELECT 1 FROM citation ct JOIN source src ON src.id = ct.source_id
        WHERE ct.claim_id = s.id
          AND src.kind IN ('disasm_fn','data_addr','pdb_type','map_symbol',
                           'build_manifest','shipped_shader'));

-- Everything awaiting human confirmation. Agents propose; only the CLI confirms.
CREATE VIEW v_review_queue AS
SELECT 'todo' AS kind, id, title AS label, proposed_by, proposed_at FROM todo
 WHERE review_state='proposed'
UNION ALL
SELECT 'edge', id,
       src_type || ' #' || src_id || ' --' || kind || '--> ' || dst_type || ' #' || dst_id,
       proposed_by, proposed_at
FROM edge WHERE review_state='proposed';

-- ---------------------------------------------------------------------------
-- migration coverage — the gate on the rewrite
-- ---------------------------------------------------------------------------

CREATE VIEW v_migration_coverage AS
SELECT ms.path, ms.commit_sha,
       COUNT(*)                                        AS paragraphs,
       SUM(mp.accounted)                               AS accounted,
       COUNT(*) - SUM(mp.accounted)                    AS unaccounted,
       ROUND(100.0 * SUM(mp.accounted) / COUNT(*), 1)  AS pct
FROM migration_source ms
JOIN migration_para mp ON mp.migration_source_id = ms.id
GROUP BY ms.id;
