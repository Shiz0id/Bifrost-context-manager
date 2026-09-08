-- Bifrost 010 — the evidence written in the code.
--
-- This project's convention is that comments explain WHY and cite provenance,
-- so a large part of what is known lives in them. Bifrost ingested the retail
-- binary, the asset trees, the gate runs and one markdown document, and none of
-- that. Measured over the BF3 tree: 235 distinct retail addresses are cited in
-- source comments and 107 of them -- 46% -- appeared nowhere in the knowledge
-- layer. 39 of those resolve to an exact retail symbol. An agent that resolved
-- one of those addresses was told "unresolved by Bifrost" while a header three
-- directories away already explained it, and disassembled it again.
--
-- INDEXED, NOT MIGRATED. The roadmap could be rewritten into rows and deleted,
-- so it could not drift; a comment has to stay in the code where people read
-- it, which means anything stored here is a SECOND copy of a fact. Two records
-- of one fact with nothing to tell them apart is the failure this project keeps
-- hitting, so the file stays authoritative and every row carries what it takes
-- to prove it is still current: the commit it was read at and a hash of the
-- block. v_code_comment marks a row stale rather than letting it quietly rot.
--
-- The prose IS stored, deliberately, even though that is the copy. The whole
-- value is answering "has anyone already worked this out" without a grep, and a
-- token list cannot answer it. Storing it as a cache with a staleness check is
-- honest; storing tokens alone would be safe and useless.

CREATE TABLE code_comment (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,          -- repo-relative, forward slashes
    line        INTEGER NOT NULL,       -- first line of the block, 1-based
    end_line    INTEGER NOT NULL,
    prose       TEXT NOT NULL,          -- the block, comment markers stripped
    hash        TEXT NOT NULL,          -- sha1 of prose; how drift is detected
    commit_sha  TEXT,                   -- HEAD when it was read
    scanned_at  TEXT NOT NULL,
    UNIQUE (path, line)
);

CREATE INDEX idx_code_comment_path ON code_comment(path);

-- Which citations a block carries. The source rows are the SAME rows a claim
-- cites, which is the point: a comment and a claim that cite one address are
-- now visibly about the same thing.
CREATE TABLE code_comment_source (
    comment_id INTEGER NOT NULL REFERENCES code_comment(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES source(id),
    PRIMARY KEY (comment_id, source_id)
);

CREATE INDEX idx_ccs_source ON code_comment_source(source_id);

-- A row is stale when the file it was read from has moved on, or is dirty. The
-- comment itself may be untouched -- this says "not proven current", not
-- "wrong", and re-scanning settles it in a second.
CREATE VIEW v_code_comment AS
SELECT c.id, c.path, c.line, c.end_line, c.prose, c.commit_sha, c.scanned_at,
       (SELECT COUNT(*) FROM code_comment_source WHERE comment_id = c.id) AS n_citations,
       CASE
         WHEN gs.path IS NULL                        THEN 1
         WHEN gs.dirty = 1                           THEN 1
         WHEN c.commit_sha IS NOT NULL
              AND gs.last_commit IS NOT NULL
              AND gs.last_commit <> c.commit_sha     THEN 1
         ELSE 0
       END AS stale
FROM code_comment c
LEFT JOIN git_state gs ON gs.path = c.path;
