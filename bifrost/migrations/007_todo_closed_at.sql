-- Bifrost 007 — when a todo was closed, not merely that it was.
--
-- `todo` recorded proposed_at and confirmed_at but nothing for the transition
-- that matters most in practice: open -> done. With two sessions working the
-- same database, "who closed this, and when" is a question that gets asked and
-- could not be answered -- the row simply read 'done', with the timestamps
-- still pointing at the day it was proposed.
--
-- NULL is the honest value for a todo closed before this migration: those
-- closes happened, and their time is not recoverable. Nothing backfills them.

ALTER TABLE todo ADD COLUMN closed_at TEXT;
