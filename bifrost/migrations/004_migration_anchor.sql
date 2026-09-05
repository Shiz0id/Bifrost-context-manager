-- Bifrost 004 — carry the section anchor on each migrated paragraph.
--
-- A code block holding a struct layout has to be attached to the format it
-- describes, and the only thing that says which is the section it sits under.
-- Recomputing it from the document at every use would mean the document is
-- still the source of truth, which is the thing this migration ends.

ALTER TABLE migration_para ADD COLUMN anchor TEXT;

CREATE INDEX idx_para_anchor ON migration_para(anchor);
