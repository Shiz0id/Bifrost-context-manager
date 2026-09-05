-- Bifrost 005 — what counts as backing a claim.
--
-- 002 accepted only a passing gate invariant or a discriminator, which made the
-- rendered roadmap read [UNBACKED] on almost every line. That is not a display
-- problem, it is a wrong model: it treats corpus verification as the ONLY form
-- of evidence, when AGENTS.md rule 1 makes reading the retail code the primary
-- one and rule 4 makes corpus coverage a separate requirement.
--
--   rule 1  never infer a format, read the code that reads it
--   rule 4  a format is done when a full-corpus run passes
--
-- Those are two different things and the schema now says so:
--
--   READING evidence   disasm_fn, data_addr, pdb_type, map_symbol,
--                      build_manifest, shipped_shader
--                      -- the code, or the build manifest, was read
--   COVERAGE evidence  a passing gate invariant, or a discriminator
--                      -- the reading was checked across the corpus, or against
--                         a rival reading
--
-- A claim asserted 'verified' now sticks when it has either. It downgrades to
-- 'asserted-unbacked' only when its whole case is `measurement` or
-- `shipped_file` -- somebody looked at the bytes and inferred, which is exactly
-- the failure mode rule 1 exists to prevent, and exactly what the marker should
-- be reserved for.
--
-- Missing corpus coverage has not stopped being a problem; it is reported at LOW
-- severity in v_claim_risk as "no gate invariant", where it belongs.

DROP VIEW v_claim_risk;
DROP VIEW v_claim_status;
DROP VIEW v_claim_backing;

CREATE VIEW v_claim_backing AS
SELECT c.id AS claim_id,
       (SELECT COUNT(*) FROM citation      WHERE claim_id = c.id)                       AS n_citations,
       (SELECT COUNT(*) FROM discriminator WHERE claim_a  = c.id OR claim_b = c.id)     AS n_discriminators,
       (SELECT COUNT(*) FROM gate_invariant gi
          JOIN v_gate_latest gl ON gl.gate_id = gi.gate_id
         WHERE gi.claim_id = c.id AND gl.ok = 1)                                        AS n_passing_gates,
       (SELECT COUNT(*) FROM gate_invariant WHERE claim_id = c.id)                      AS n_gates,
       (SELECT COUNT(*) FROM refutation    WHERE refuted_claim = c.id)                  AS n_refutations,
       (SELECT COUNT(*) FROM tautology     WHERE claim_id = c.id AND retired_at IS NULL) AS n_tautologies,
       -- rule 1: the retail code, the PDB or a build manifest was actually read
       (SELECT COUNT(*) FROM citation ct JOIN source s ON s.id = ct.source_id
         WHERE ct.claim_id = c.id
           AND s.kind IN ('disasm_fn','data_addr','pdb_type','map_symbol',
                          'build_manifest','shipped_shader'))                           AS n_read
FROM claim c;

CREATE VIEW v_claim_status AS
SELECT c.id,
       c.subject_type, c.subject_id, c.statement, c.asserted_status,
       c.roadmap_anchor, c.created_at, c.created_by,
       b.n_citations, b.n_discriminators, b.n_passing_gates, b.n_gates,
       b.n_refutations, b.n_tautologies, b.n_read,
       CASE
         WHEN b.n_refutations > 0                                      THEN 'refuted'
         WHEN c.asserted_status = 'verified'
              AND (b.n_passing_gates > 0 OR b.n_discriminators > 0
                   OR b.n_read > 0)                                    THEN 'verified'
         WHEN c.asserted_status = 'verified'                           THEN 'asserted-unbacked'
         ELSE c.asserted_status
       END AS effective_status
FROM claim c
JOIN v_claim_backing b ON b.claim_id = c.id;

CREATE VIEW v_claim_risk AS
SELECT s.id, s.subject_type, s.subject_id, s.statement, s.effective_status,
       s.roadmap_anchor,
       CASE
         WHEN s.n_refutations > 0                       THEN 'refuted'
         WHEN s.n_tautologies > 0                       THEN 'backed only by a tautology'
         WHEN s.effective_status = 'asserted-unbacked'  THEN 'asserted verified on inference alone -- no code read, no gate, no discriminator'
         WHEN s.n_citations = 0                         THEN 'no citation'
         WHEN s.effective_status = 'assumed'            THEN 'assumed, never discriminated'
         WHEN s.n_read = 0                              THEN 'no reading of the retail code behind it (rule 1)'
         WHEN s.n_gates = 0                             THEN 'no gate invariant (rule 4)'
         ELSE NULL
       END AS risk,
       CASE
         WHEN s.n_refutations > 0                       THEN 'high'
         WHEN s.n_tautologies > 0                       THEN 'high'
         WHEN s.effective_status = 'asserted-unbacked'  THEN 'high'
         WHEN s.n_citations = 0                         THEN 'high'
         WHEN s.effective_status = 'assumed'            THEN 'medium'
         WHEN s.n_read = 0                              THEN 'medium'
         WHEN s.n_gates = 0                             THEN 'low'
         ELSE NULL
       END AS severity
FROM v_claim_status s
WHERE risk IS NOT NULL;
