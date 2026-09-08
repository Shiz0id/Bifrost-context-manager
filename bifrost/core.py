"""Bifrost core — the shared module behind both the CLI and the MCP server.

Nothing here decides project status. Status lives in the views (002_views.sql);
this module opens the database, applies migrations, resolves provenance against
the retail symbol databases, and enforces the two invariants that cannot be
expressed in SQL:

  * a claim without a citation is rejected  (AGENTS.md rule 6)
  * only mutable rows carry review_state; history is written freely

Both the CLI and the MCP server import this. There is no second implementation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"


def repo_root() -> Path:
    """The PROJECT's root, not Bifrost's. Bifrost lives in its own repository
    and is pointed at a project; see profile.py."""
    from . import profile
    return profile.load().root


def default_db() -> Path:
    from . import profile
    return profile.load().db_path

# The retail symbol databases. The 360's is prebuilt (22,714 types / 286,660
# fields / 42,533 methods / 37,317 map symbols); the Wii's is ingested from
# RABAZZ_full.map, which is why 83 of the roadmap's addresses currently resolve
# to nothing.
def symbol_db() -> Optional[Path]:
    """A prebuilt symbol database to ATTACH read-only, if the project has one."""
    from . import profile
    p = profile.load().SYMBOL_DB
    return Path(p) if p else None

# The dispositions an exception may carry, matching the CHECK in 001_initial.
# Named here so seed can report the legal set instead of letting sqlite report a
# constraint, and so a profile author has somewhere to look it up.
EXCEPTION_DISPOSITIONS = {"reported", "refused", "defect_in_shipped_data"}

CLAIM_STATUSES = {"assumed", "plausible", "verified"}

SOURCE_KINDS = {
    "disasm_fn", "data_addr", "pdb_type", "map_symbol", "build_manifest",
    "shipped_file", "shipped_shader", "design_doc", "measurement", "cross_build",
    # This project's own reasoning, at "src/SelotapeDataLoaders.cpp:344". NOT
    # the same thing as disasm_fn: that cites the retail function, this cites
    # what we concluded about it. Backing-wise it behaves like `measurement` --
    # our own note is not evidence for itself -- but a claim can finally point
    # at the code that embodies it, which nothing could express before.
    "source_comment",
    # A range in OUR code -- "src/SelotapeD3D11.cpp:3666-3698". Distinct from
    # source_comment (a comment block) and emphatically not shipped_file, which
    # is where one landed for want of anywhere better: it is neither shipped nor
    # theirs. ensure_source pins it to a commit, because a line range that
    # nothing anchors rots the next time somebody edits the function.
    "source_ref",
}

# Kinds whose locator names a path in this repository, and therefore decays
# unless it is pinned to a commit.
PATH_KINDS = {"source_comment", "source_ref"}

EDGE_KINDS = {
    "gates", "unlocks", "verified_by", "read_from", "implements",
    "diverges_from", "consumes", "pins", "derived_from",
}

# Addresses at or above this in the 360 image are text; below is data. Used only
# to pick a default source kind when the caller does not say.
XENON_TEXT_BASE = 0x82200000


class BifrostError(RuntimeError):
    """Raised when a write would violate an invariant the schema cannot express."""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# connection & migrations
# ---------------------------------------------------------------------------

def connect(path: os.PathLike | str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path) if path else default_db()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # uri=True on every connection, not just the read-only ones: ATTACH parses a
    # `file:` URI only when the connection itself was opened in URI mode, and
    # attach_symbols needs mode=ro. Without it sqlite reports the far less
    # helpful "unable to open database".
    if readonly and str(path) != ":memory:":
        # as_posix(): a Windows path with backslashes is not a valid file: URI
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly and str(path) != ":memory:":
        # Agents write concurrently; WAL keeps a reader from blocking the hook
        # that is ingesting a gate run.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migration_files() -> list[tuple[int, str, Path]]:
    out = []
    for p in sorted(MIGRATIONS.glob("*.sql")):
        m = re.match(r"^(\d+)_(.+)\.sql$", p.name)
        if m:
            out.append((int(m.group(1)), m.group(2), p))
    return out


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not row:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_version")}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Idempotent."""
    done = applied_versions(conn)
    applied = []
    for version, name, path in migration_files():
        if version in done:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (?,?,?)",
            (version, name, utcnow()),
        )
        conn.commit()
        applied.append(version)
    return applied


def attach_symbols(conn: sqlite3.Connection, path: os.PathLike | str | None = None,
                   alias: str = "sym") -> bool:
    """ATTACH the retail symbol database read-only. 55 MB and regenerable, so it
    is referenced rather than copied. Returns False if it is not present."""
    p = Path(path) if path else symbol_db()
    if p is None:
        return False
    if not p.exists():
        return False
    already = {r["name"] for r in conn.execute("PRAGMA database_list")}
    if alias in already:
        return True
    # as_posix(): a Windows path with backslashes is not a valid file: URI, and
    # sqlite reports it as "unable to open database" rather than as a bad URI.
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{p.as_posix()}?mode=ro",))
    return True


def has_symbols(conn: sqlite3.Connection, alias: str = "sym") -> bool:
    return alias in {r["name"] for r in conn.execute("PRAGMA database_list")}


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    kind: str                  # exact | inside | unresolved
    symbol: Optional[str] = None
    containing: Optional[str] = None
    offset: Optional[int] = None


def resolve_address(conn: sqlite3.Connection, va: int, build: str = "bf3_360",
                    *, window: int = 0x800) -> Resolution:
    """Resolve a virtual address against the symbol tables.

    Three outcomes, and the third is not a failure. `map_symbols` holds functions
    and classes only, so a data address -- a constant pool, a jump table, a string
    blob -- has no symbol and is cited by meaning instead. Marking those
    'unresolved' would permanently flag 29 perfectly well-cited addresses in the
    roadmap as broken.
    """
    # Wii symbols live in our own wii_symbol table, so this branch must NOT be
    # gated on the 360's retail database being attached -- they are unrelated
    # sources, and gating them together silently returned 'unresolved' for every
    # Wii address whenever the 360 database was absent.
    if build == "bf3_wii":
        row = conn.execute(
            "SELECT symbol FROM wii_symbol WHERE addr = ?", (va,)
        ).fetchone()
        if row:
            return Resolution("exact", symbol=row["symbol"])
        row = conn.execute(
            "SELECT symbol, addr FROM wii_symbol WHERE addr <= ? ORDER BY addr DESC LIMIT 1",
            (va,),
        ).fetchone()
        if row and va - row["addr"] < window:
            return Resolution("inside", containing=row["symbol"], offset=va - row["addr"])
        return Resolution("unresolved")

    if not has_symbols(conn):
        return Resolution("unresolved")

    row = conn.execute("SELECT mangled FROM sym.map_symbols WHERE rva = ?", (va,)).fetchone()
    if row:
        return Resolution("exact", symbol=row["mangled"])
    row = conn.execute(
        "SELECT mangled, rva FROM sym.map_symbols WHERE rva <= ? ORDER BY rva DESC LIMIT 1",
        (va,),
    ).fetchone()
    if row and va - row["rva"] < window:
        return Resolution("inside", containing=row["mangled"], offset=va - row["rva"])
    return Resolution("unresolved")


def pdb_field(conn: sqlite3.Connection, struct: str, field: str) -> Optional[sqlite3.Row]:
    """Look up a struct field in the retail PDB. This is what makes a recorded
    offset checkable rather than assertable."""
    if not has_symbols(conn):
        return None
    return conn.execute(
        "SELECT name, type, offset, size FROM sym.fields WHERE class = ? AND name = ?",
        (struct, field),
    ).fetchone()


def ensure_source(conn: sqlite3.Connection, kind: str, locator: str,
                  build: str | None = None, note: str | None = None,
                  pinned_commit: str | None = None) -> int:
    """Get or create a source row, resolving addresses on the way in.

    A locator naming a path in this repository is pinned to a commit, defaulting
    to HEAD. An address in the retail image is fixed forever; "Foo.cpp:3666-3698"
    is true only of one revision, and unpinned it quietly becomes a lie the next
    time somebody edits that function.
    """
    if kind not in SOURCE_KINDS:
        raise BifrostError(f"unknown source kind {kind!r}; expected one of {sorted(SOURCE_KINDS)}")
    if kind in PATH_KINDS and pinned_commit is None:
        pinned_commit = head_commit()

    build_id = None
    if build:
        r = conn.execute("SELECT id FROM build WHERE name = ?", (build,)).fetchone()
        if r:
            build_id = r["id"]

    existing = conn.execute(
        "SELECT id FROM source WHERE kind=? AND locator=? AND build_id IS ?",
        (kind, locator, build_id),
    ).fetchone()
    if existing:
        return existing["id"]

    symbol = containing = None
    offset = None
    if kind in ("disasm_fn", "data_addr", "map_symbol") and re.fullmatch(r"0x[0-9A-Fa-f]+", locator):
        res = resolve_address(conn, int(locator, 16), build or "bf3_360")
        symbol, containing, offset = res.symbol, res.containing, res.offset

    cur = conn.execute(
        """INSERT INTO source(kind, locator, build_id, symbol, containing_symbol,
                              offset_in_symbol, note, pinned_commit)
           VALUES (?,?,?,?,?,?,?,?)""",
        (kind, locator, build_id, symbol, containing, offset, note, pinned_commit),
    )
    return int(cur.lastrowid)


def classify_address(va: int) -> str:
    """Default source kind for a bare 360 address. Text is a function; below the
    text base it is a constant pool or table, which is a different kind of
    citation, not a failed one."""
    return "disasm_fn" if va >= XENON_TEXT_BASE else "data_addr"


# ---------------------------------------------------------------------------
# the write path — history is append-only, state is proposed
# ---------------------------------------------------------------------------

def record_claim(conn: sqlite3.Connection, *, subject_type: str, subject_id: int | None,
                 statement: str, citations: Sequence[dict],
                 asserted_status: str = "plausible", created_by: str = "agent",
                 roadmap_anchor: str | None = None, para_id: int | None = None,
                 layer: str = "retail") -> int:
    """Append a claim.

    Rejects an empty citation set. That is AGENTS.md rule 6 enforced where it can
    actually be enforced -- at the boundary -- rather than left to an agent's
    discipline. `asserted_status` is only what the caller claims; the effective
    status comes from v_claim_status, which downgrades an unbacked 'verified'.
    """
    if not statement or not statement.strip():
        raise BifrostError("a claim needs a statement")
    if not citations:
        raise BifrostError(
            "a claim needs at least one citation (rule 6). Cite the function you "
            "disassembled, the PDB struct, the build manifest, or the measurement."
        )
    if asserted_status not in ("assumed", "plausible", "verified"):
        raise BifrostError(f"bad asserted_status {asserted_status!r}")
    if layer not in ("retail", "divergence"):
        raise BifrostError(
            "layer is 'retail' (how the shipped game works) or 'divergence' (where our "
            f"reimplementation differs from it); got {layer!r}")

    cur = conn.execute(
        """INSERT INTO claim(subject_type, subject_id, statement, asserted_status,
                             created_at, created_by, roadmap_anchor, para_id, layer)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (subject_type, subject_id, statement.strip(), asserted_status,
         utcnow(), created_by, roadmap_anchor, para_id, layer),
    )
    claim_id = int(cur.lastrowid)

    for c in citations:
        sid = ensure_source(conn, c["kind"], c["locator"], c.get("build"), c.get("source_note"))
        conn.execute(
            "INSERT OR IGNORE INTO citation(claim_id, source_id, note) VALUES (?,?,?)",
            (claim_id, sid, c.get("note")),
        )
    conn.commit()
    return claim_id


def record_discriminator(conn: sqlite3.Connection, *, claim_a: int, claim_b: int | None,
                         check_desc: str, result_a: str, result_b: str,
                         separation: str | None = None,
                         gate_run_id: int | None = None) -> int:
    """The check that separates two candidate readings.

    This is the row that would have caught four of the five defects corrected in
    the week of 2-4 Sep 2026, because each of those survived a gate whose
    invariant the wrong reading also satisfied.
    """
    if not check_desc.strip():
        raise BifrostError("a discriminator needs to say what was checked")
    cur = conn.execute(
        """INSERT INTO discriminator(claim_a, claim_b, check_desc, result_a, result_b,
                                     separation, gate_run_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (claim_a, claim_b, check_desc.strip(), result_a, result_b, separation,
         gate_run_id, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def propose_discriminator(conn: sqlite3.Connection, *, claim_a: int,
                          claim_b: int | None, check_desc: str,
                          predicted_a: str, predicted_b: str,
                          why_decisive: str | None = None,
                          gate_run_id: int | None = None,
                          proposed_by: str = "agent") -> int:
    """The check that WOULD separate two readings, recorded before it is run.

    record_discriminator() needs both results, which means it can only be called
    once the question is already settled. But the moment you most want a
    discriminator captured is the moment you realise one is needed: you have the
    observation, you have two readings that would explain it, and you have a
    cheap check that tells them apart -- and no results, because not having run
    it is the entire point. Without this verb that moment goes into prose, and a
    specific decisive next check is the most valuable thing a session produces.

    The predictions are what make this a discriminator rather than a to-do: a
    check whose two readings predict the SAME outcome separates nothing, and
    writing both down is what forces that question before the work is spent.

    Fill it in later with settle_discriminator().
    """
    if not check_desc.strip():
        raise BifrostError("a proposed discriminator needs to say what to check")
    if not predicted_a.strip() or not predicted_b.strip():
        raise BifrostError(
            "a proposed discriminator needs both predictions -- what each reading says "
            "the check will give. If they are the same, the check separates nothing")
    cur = conn.execute(
        """INSERT INTO discriminator(claim_a, claim_b, check_desc, predicted_a, predicted_b,
                                     why_decisive, gate_run_id, proposed_by, proposed_at,
                                     created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (claim_a, claim_b, check_desc.strip(), predicted_a.strip(), predicted_b.strip(),
         why_decisive, gate_run_id, proposed_by, utcnow(), utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def settle_discriminator(conn: sqlite3.Connection, disc_id: int, *,
                         result_a: str, result_b: str,
                         separation: str | None = None,
                         gate_run_id: int | None = None) -> dict:
    """Run a proposed discriminator: same row, predictions now beside results.

    Keeping it in one row is the point. What a reading predicted before the
    check, next to what the check gave, is the record of whether the reasoning
    was any good -- and that is lost the moment these become two rows.
    """
    row = one(conn, "SELECT * FROM discriminator WHERE id=?", (disc_id,))
    if row is None:
        raise BifrostError(f"no discriminator #{disc_id}")
    if row["result_a"] is not None:
        raise BifrostError(f"discriminator #{disc_id} was already run")
    conn.execute(
        """UPDATE discriminator SET result_a=?, result_b=?, separation=?,
                                    gate_run_id=COALESCE(?, gate_run_id)
           WHERE id=?""",
        (result_a, result_b, separation, gate_run_id, disc_id))
    conn.commit()
    return one(conn, "SELECT * FROM discriminator WHERE id=?", (disc_id,))


def record_refutation(conn: sqlite3.Connection, *, refuted_claim: int,
                      what_killed_it: str, why_plausible: str | None = None,
                      by_claim: int | None = None,
                      source: dict | None = None) -> int:
    """Retire a reading, keeping why it was plausible.

    Why the wrong reading looked right is the expensive knowledge; deleting it
    means re-deriving it. v_claim_status reads this and downgrades to 'refuted'
    with no further action.
    """
    sid = None
    if source:
        sid = ensure_source(conn, source["kind"], source["locator"],
                            source.get("build"), source.get("note"))
    cur = conn.execute(
        """INSERT INTO refutation(refuted_claim, by_claim, why_plausible,
                                  what_killed_it, source_id, created_at)
           VALUES (?,?,?,?,?,?)""",
        (refuted_claim, by_claim, why_plausible, what_killed_it, sid, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_tautology(conn: sqlite3.Connection, *, claim_id: int,
                     why_it_could_not_fail: str) -> int:
    """Flag a check that could not have failed, so it is never counted again."""
    cur = conn.execute(
        """INSERT INTO tautology(claim_id, why_it_could_not_fail, created_at)
           VALUES (?,?,?)""",
        (claim_id, why_it_could_not_fail, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def propose(conn: sqlite3.Connection, kind: str, payload: dict,
            proposed_by: str = "agent") -> int:
    """Propose a change to mutable state. Lands unconfirmed.

    This is the only write path that touches state rather than history, which is
    why it is the only one carrying review_state -- the review boundary is one
    function, not a convention spread through the codebase.
    """
    now = utcnow()
    if kind == "todo":
        cur = conn.execute(
            """INSERT INTO todo(title, rationale, phase, difficulty, priority,
                                review_state, proposed_by, proposed_at, roadmap_anchor)
               VALUES (?,?,?,?,?,'proposed',?,?,?)""",
            (payload["title"], payload.get("rationale"), payload.get("phase"),
             payload.get("difficulty"), payload.get("priority"),
             proposed_by, now, payload.get("roadmap_anchor")),
        )
    elif kind == "capability":
        cur = conn.execute(
            "INSERT INTO capability(name, description) VALUES (?,?)",
            (payload["name"], payload.get("description")),
        )
    elif kind == "edge":
        if payload["kind"] not in EDGE_KINDS:
            raise BifrostError(
                f"unknown edge kind {payload['kind']!r}; expected one of {sorted(EDGE_KINDS)}"
            )
        cur = conn.execute(
            """INSERT INTO edge(src_type, src_id, kind, dst_type, dst_id, note,
                                review_state, proposed_by, proposed_at)
               VALUES (?,?,?,?,?,?,'proposed',?,?)""",
            (payload["src_type"], payload["src_id"], payload["kind"],
             payload["dst_type"], payload["dst_id"], payload.get("note"),
             proposed_by, now),
        )
    else:
        raise BifrostError(f"cannot propose {kind!r}; expected todo, capability or edge")
    conn.commit()
    return int(cur.lastrowid)


def review(conn: sqlite3.Connection, kind: str, row_id: int, decision: str) -> None:
    """Confirm or reject a proposal.

    Deliberately NOT exposed over MCP. Agents propose; only a human at the CLI
    confirms, so an agent is structurally incapable of approving its own work.
    """
    if decision not in ("confirmed", "rejected"):
        raise BifrostError("decision must be 'confirmed' or 'rejected'")
    if kind not in ("todo", "edge"):
        raise BifrostError(f"nothing to review on {kind!r}")
    conn.execute(
        f"UPDATE {kind} SET review_state=?, confirmed_at=? WHERE id=?",
        (decision, utcnow() if decision == "confirmed" else None, row_id),
    )
    conn.commit()


CLOSED_STATUSES = ("done", "abandoned")


def close_todo(conn: sqlite3.Connection, todo_id: int,
               status: str = "done") -> dict:
    """Close a todo: 'done' when the work landed, 'abandoned' when it will not.

    Deliberately NOT exposed over MCP, for the same reason as review(). An agent
    that can mark its own work done can report a project finished without any of
    it being true, and the digest's OPEN WORK section is exactly the list that
    would go quiet. Closing is a judgement about the world, not something
    derivable from the database.

    Only a confirmed todo can be closed. A proposal that should not happen is
    rejected at review, not closed -- keeping the two verbs from overlapping is
    what makes 'done' mean the work actually happened.
    """
    if status not in CLOSED_STATUSES:
        raise BifrostError(f"status must be one of {CLOSED_STATUSES}")
    row = one(conn, "SELECT * FROM todo WHERE id=?", (todo_id,))
    if row is None:
        raise BifrostError(f"no todo #{todo_id}")
    if row["review_state"] != "confirmed":
        raise BifrostError(
            f"todo #{todo_id} is {row['review_state']}, not confirmed; "
            f"use `bifrost review --reject todo:{todo_id}` to turn down a proposal")
    if row["status"] in CLOSED_STATUSES:
        raise BifrostError(f"todo #{todo_id} is already {row['status']}")
    conn.execute("UPDATE todo SET status=?, closed_at=? WHERE id=?",
                 (status, utcnow(), todo_id))
    conn.commit()
    return one(conn, "SELECT * FROM todo WHERE id=?", (todo_id,))


# ---------------------------------------------------------------------------
# git state — what makes staleness computable rather than declared
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path | None = None) -> str:
    cwd = cwd or repo_root()
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    ).stdout.strip()


def head_commit(cwd: Path | None = None) -> str:
    return _git("rev-parse", "HEAD", cwd=cwd)


def tree_dirty(paths: Iterable[str] | None = None, cwd: Path | None = None) -> bool:
    args = ["status", "--porcelain", "--"]
    args += list(paths) if paths else ["src", "include", "tools"]
    return bool(_git(*args, cwd=cwd))


def refresh_git_state(conn: sqlite3.Connection, paths: Iterable[str],
                      cwd: Path | None = None) -> int:
    """Record, per source file, the last commit that touched it and whether it is
    dirty. v_gate_stale joins this against gate_run.commit_sha."""
    dirty_paths = set()
    for line in _git("status", "--porcelain", cwd=cwd).splitlines():
        if len(line) > 3:
            dirty_paths.add(line[3:].strip().replace("\\", "/"))

    now = utcnow()
    n = 0
    for path in paths:
        norm = path.replace("\\", "/")
        info = _git("log", "-1", "--format=%H|%cI", "--", norm, cwd=cwd)
        sha, ts = (info.split("|", 1) + [None])[:2] if info else (None, None)
        conn.execute(
            """INSERT INTO git_state(path, last_commit, last_commit_ts, dirty, scanned_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 last_commit=excluded.last_commit,
                 last_commit_ts=excluded.last_commit_ts,
                 dirty=excluded.dirty,
                 scanned_at=excluded.scanned_at""",
            (norm, sha, ts, 1 if norm in dirty_paths else 0, now),
        )
        n += 1
    conn.commit()
    return n


def refresh_git_ancestry(conn: sqlite3.Connection, limit: int = 2000,
                         cwd: Path | None = None) -> int:
    """Ordinal 0 is HEAD, increasing into the past, so a SMALLER ordinal is a
    NEWER commit. v_gate_stale compares two ordinals rather than shelling out to
    `git merge-base --is-ancestor` per row."""
    out = _git("log", f"-{limit}", "--format=%H", cwd=cwd)
    rows = [(sha, i) for i, sha in enumerate(out.splitlines()) if sha]
    conn.execute("DELETE FROM git_ancestry")
    conn.executemany("INSERT INTO git_ancestry(commit_sha, ordinal) VALUES (?,?)", rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# small helpers used by the CLI, the MCP server and the tests
# ---------------------------------------------------------------------------

def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_id(conn: sqlite3.Connection, table: str, name: str) -> Optional[int]:
    r = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    return r["id"] if r else None
