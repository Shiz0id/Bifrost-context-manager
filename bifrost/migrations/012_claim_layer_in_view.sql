-- Bifrost 012 — surface `layer` on the view everything actually reads.
--
-- 011 added claim.layer, and this belongs with it. It is a separate migration
-- for one reason: 011 had already been applied to a live database by the time
-- the omission was noticed, and editing an applied migration is precisely what
-- broke that database during 009 -- the file changes, the version row does not,
-- and the two disagree forever after. A migration that has run anywhere is
-- append-only. This is the append.
--
-- Body is unchanged from 006 apart from c.layer.

DROP VIEW IF EXISTS v_claim_status;
CREATE VIEW v_claim_status AS
SELECT c.id,
       c.subject_type, c.subject_id, c.statement, c.asserted_status, c.layer,
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
