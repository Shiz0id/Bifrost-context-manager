"""Bifrost — indexing the evidence written in the code.

This project's convention is that a comment explains WHY and cites provenance,
which makes the comments part of the knowledge layer. They were not in it. Over
the BF3 tree, 235 distinct retail addresses are cited in source comments and 107
of them appeared nowhere else in the database; 39 of those resolve to an exact
retail symbol. The cost of that is specific and repeated: an agent resolves an
address, is told nothing is known about it, and disassembles a function a header
three directories away already explains.

Indexed, not migrated. The roadmap was rewritten into rows and deleted, so it
could not drift. A comment stays in the code, so every row here is a SECOND copy
of a fact -- and two records of one fact with nothing to separate them is the
failure this project keeps hitting. So the file stays authoritative and each row
carries the commit it was read at and a hash of the block, and v_code_comment
reports staleness rather than letting the copy quietly rot.

Nothing here decides what a comment ASSERTS. Extraction is mechanical; turning
prose into a claim is not, and stays with bifrost_record_claim.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from pathlib import Path

from . import core
from .core import BifrostError, utcnow

# The languages whose comments carry provenance in this project.
SUFFIXES = (".cpp", ".h", ".hpp", ".c", ".cc", ".py")

_LINE_COMMENT = {
    ".py": re.compile(r"^\s*#\s?(.*)$"),
    None:  re.compile(r"^\s*(?://+\s?|/\*+\s?|\*(?!/)\s?)(.*?)\s*(?:\*/)?$"),
}

# A block is worth a row only if it cites something. Prose with no provenance in
# it is a comment doing its ordinary job, and indexing it would bury the rows
# that matter under the ones that do not.
_CITES = re.compile(r"0x[0-9A-Fa-f]{6,8}|\b\w+_s\.\w+|\b[A-Za-z_]\w*\.(?:rax|war|res|x2t|tpf)\b")


def tracked_sources(root: Path) -> list[str]:
    """Repo-relative paths git knows about, in the languages that carry comments."""
    out = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True,
                         text=True, check=False).stdout.split()
    return sorted(p for p in out if p.endswith(SUFFIXES))


def comment_blocks(text: str, suffix: str) -> list[tuple[int, int, str]]:
    """Consecutive comment lines, as (first_line, last_line, prose).

    Blocks rather than lines because the unit of reasoning is the paragraph: the
    address is on one line and what it means is on the next three.
    """
    rx = _LINE_COMMENT.get(suffix, _LINE_COMMENT[None])
    blocks: list[tuple[int, int, str]] = []
    start: int | None = None
    body: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        m = rx.match(line)
        if m:
            if start is None:
                start = i
                body = []
            body.append(m.group(1).rstrip())
        elif start is not None:
            blocks.append((start, i - 1, "\n".join(body).strip()))
            start = None
    if start is not None:
        blocks.append((start, start + len(body) - 1, "\n".join(body).strip()))
    return [b for b in blocks if b[2]]


def scan(conn: sqlite3.Connection, root: Path | None = None,
         verbose: bool = False) -> dict:
    """Index every citing comment block in the project's tracked sources.

    Idempotent: a block whose hash is unchanged is left alone, a changed one is
    updated in place, and a block that no longer exists is deleted. Re-running
    after an edit is how a stale row stops being stale.
    """
    from . import migrate

    root = Path(root) if root else core.repo_root()
    files = tracked_sources(root)
    if not files:
        raise BifrostError(f"no tracked source files under {root}")

    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats = {"files": 0, "files_citing": 0, "blocks": 0, "citations": 0,
             "new": 0, "updated": 0, "removed": 0}

    # Staleness needs a git_state row per file, and before this only the eleven
    # paths declared as gate readers had one.
    core.refresh_git_state(conn, files, cwd=root)

    # The commit that last touched THIS FILE, not HEAD. Storing HEAD marked every
    # block stale the moment it was indexed, because a file's last commit is
    # almost never the current one -- a staleness signal that fires on
    # everything says nothing, which is worse than not having it.
    last_commit = {r["path"]: r["last_commit"]
                   for r in core.rows(conn, "SELECT path, last_commit FROM git_state")}

    now = utcnow()
    for rel in files:
        stats["files"] += 1
        head = last_commit.get(rel)
        p = root / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        seen_lines: set[int] = set()
        for first, last, prose in comment_blocks(text, p.suffix):
            if not _CITES.search(prose):
                continue
            tokens = migrate.extract_tokens(prose)
            if not tokens:
                continue

            seen_lines.add(first)
            digest = hashlib.sha1(prose.encode("utf-8")).hexdigest()
            row = core.one(conn, "SELECT id, hash FROM code_comment WHERE path=? AND line=?",
                           (rel, first))
            if row is None:
                cur = conn.execute(
                    """INSERT INTO code_comment(path, line, end_line, prose, hash,
                                                commit_sha, scanned_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (rel, first, last, prose, digest, head, now))
                cid = int(cur.lastrowid)
                stats["new"] += 1
            else:
                cid = row["id"]
                if row["hash"] != digest:
                    stats["updated"] += 1
                conn.execute(
                    """UPDATE code_comment SET end_line=?, prose=?, hash=?, commit_sha=?,
                                               scanned_at=? WHERE id=?""",
                    (last, prose, digest, head, now, cid))
                conn.execute("DELETE FROM code_comment_source WHERE comment_id=?", (cid,))

            for t in tokens:
                sid = core.ensure_source(conn, t["kind"], t["locator"], t.get("build"),
                                         note=f"cited in {rel}:{first}")
                conn.execute(
                    "INSERT OR IGNORE INTO code_comment_source(comment_id, source_id) "
                    "VALUES (?,?)", (cid, sid))
                stats["citations"] += 1
            stats["blocks"] += 1

        if seen_lines:
            stats["files_citing"] += 1
        # A block that stopped citing anything, or was deleted, must not linger.
        gone = [r["id"] for r in core.rows(
            conn, "SELECT id, line FROM code_comment WHERE path=?", (rel,))
            if r["line"] not in seen_lines]
        for cid in gone:
            conn.execute("DELETE FROM code_comment WHERE id=?", (cid,))
            stats["removed"] += 1

    conn.commit()
    log(f"[SUCCESS] {stats['blocks']} citing comment blocks in "
        f"{stats['files_citing']} of {stats['files']} files")
    return stats


def explaining(conn: sqlite3.Connection, locator: str, limit: int = 5) -> list[dict]:
    """Comment blocks that cite this locator.

    The answer to "has anyone already worked this out", which before this was a
    grep an agent had no reason to run.
    """
    return core.rows(conn, """
        SELECT v.id, v.path, v.line, v.end_line, v.prose, v.stale
        FROM v_code_comment v
        JOIN code_comment_source ccs ON ccs.comment_id = v.id
        JOIN source s ON s.id = ccs.source_id
        WHERE s.locator = ? COLLATE NOCASE
        ORDER BY v.stale, v.id
        LIMIT ?""", (locator, limit))
