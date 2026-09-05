-- Bifrost 006 — examining a shipped artefact counts as evidence.
--
-- 005 accepted only the retail code, the PDB, a build manifest or a shipped
-- shader as "reading" evidence. That left 23 claims flagged, and inspecting them
-- showed the bar was in the wrong place for 15 of them: "the Wii paks are
-- 16-byte PCK2 headers with 2,048 zero bytes of payload" cites the pak files
-- themselves, and there is no code to disassemble for it -- the artefact IS the
-- evidence, and a stronger one than a disassembly would be.
--
-- Adding `shipped_file` leaves exactly 8, and every one of those is a bare
-- statistic with no artefact and no code behind it. That is the weakest position
-- this project recognises and precisely what the marker should be reserved for:
-- somebody measured something and asserted a format fact from the measurement
-- alone, which is the shape of the failure rule 1 exists to prevent.
--
-- Three of those 8 do state a control in their own prose. They are not weak
-- claims, they are claims whose DISCRIMINATOR was never recorded -- so the flag
-- is doing its job, and the fix is a discriminator row rather than a looser rule.

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
       -- the retail code, the PDB, a build manifest, a shipped shader, or a
       -- shipped artefact was actually examined
       (SELECT COUNT(*) FROM citation ct JOIN source s ON s.id = ct.source_id
         WHERE ct.claim_id = c.id
           AND s.kind IN ('disasm_fn','data_addr','pdb_type','map_symbol',
                          'build_manifest','shipped_shader','shipped_file'))            AS n_read,
       -- the narrower rule-1 sense: code or type information, not an artefact
       (SELECT COUNT(*) FROM citation ct JOIN source s ON s.id = ct.source_id
         WHERE ct.claim_id = c.id
           AND s.kind IN ('disasm_fn','data_addr','pdb_type','map_symbol',
                          'build_manifest','shipped_shader'))                           AS n_code_read
FROM claim c;

CREATE VIEW v_claim_status AS
SELECT c.id,
       c.subject_type, c.subject_id, c.statement, c.asserted_status,
       c.roadmap_anchor, c.created_at, c.created_by,
       b.n_citations, b.n_discriminators, b.n_passing_gates, b.n_gates,
       b.n_refutations, b.n_tautologies, b.n_read, b.n_code_read,
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
         WHEN s.effective_status = 'asserted-unbacked'  THEN 'asserted verified from a measurement alone -- no artefact, no code, no discriminator'
         WHEN s.n_citations = 0                         THEN 'no citation'
         WHEN s.effective_status = 'assumed'            THEN 'assumed, never discriminated'
         WHEN s.n_gates = 0                             THEN 'no gate invariant (rule 4)'
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
