-- Bifrost 011 — asking a question you do not already know the answer to.
--
-- SEARCH
--   There was no way to ask "which claims mention decals". `realm` needs a name
--   you already have and `query` needs a view, so the only route to a question
--   shaped like that was dumping all 276 claims to a file and grepping it. That
--   is 48,000 tokens spent to find three rows, and it was the single most
--   expensive gap a tester hit.
--
--   One index over everything with prose in it: claims, the code comments from
--   010, formats, todos, and citation locators. An external-content FTS table
--   would stay in step automatically but ties the index to one table; this is a
--   plain fts5 table rebuilt by search.reindex(), because the corpus is five
--   different shapes and a rebuild over ~1,500 rows costs milliseconds.
--
-- LAYER
--   Nothing separated "how the retail game works" from "how our reimplementation
--   differs from it". Claims 277 and 278 are filed under attribute_info and
--   shader_db but are not about those formats -- they are about where the viewer
--   diverges from them, and only their prose said so. For a clean-room project
--   that is a distinction with consequences: a reader asking what the retail
--   engine does and one asking where ours differs want different result sets,
--   and both got the same pile.
--
--   Defaulted to 'retail' and NOT backfilled by guessing. Inferring which
--   existing claims are divergences from words like "the viewer" is exactly the
--   inference this project forbids; the ones that need moving are found with
--   search and moved deliberately.
--
-- PINNED CITATIONS
--   A locator like "src/SelotapeD3D11.cpp:3666-3698" rots the next time anyone
--   edits that function, and there was no kind that fit it anyway -- it went in
--   as `shipped_file`, which is doubly wrong, since it is neither shipped nor
--   theirs. `source_ref` is our own code, and pinned_commit is what stops a line
--   range decaying into a lie.

ALTER TABLE claim ADD COLUMN layer TEXT NOT NULL DEFAULT 'retail'
    CHECK (layer IN ('retail', 'divergence'));

CREATE INDEX idx_claim_layer ON claim(layer);

-- The commit a path-and-line locator was true at. NULL for everything that is
-- not a path: an address in the retail image does not move.
ALTER TABLE source ADD COLUMN pinned_commit TEXT;

CREATE VIRTUAL TABLE search_index USING fts5(
    kind,            -- claim | comment | format | todo | source
    ref,             -- the row id, as text
    title,           -- short label for the hit
    body,            -- what is actually searched
    extra,           -- status, path, whatever the caller needs to triage
    tokenize = "unicode61 remove_diacritics 2"
);
