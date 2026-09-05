"""Dump and restore the knowledge layer, as committable text.

The database is a build product: scans, symbol tables and gate runs all rebuild
from the profile in seconds. The KNOWLEDGE layer does not. Claims, their
citations, the discriminators and refutations, and the migrated passages are
hand-written judgements — recreating them means reading the source document
again and rewriting several hundred statements.

Since `build/bifrost.db` is gitignored (binary, and a merge conflict in it is
unfixable), that work would live in exactly one file on one machine. This writes
it back out as JSONL the project can commit, and reads it back.

Deterministic on purpose: rows ordered by a stable key, keys sorted, no
timestamps of its own. A dump that reorders itself produces a meaningless diff
and nobody reviews it twice.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from . import core

# The append-only knowledge layer, in dependency order so a restore can insert
# without deferring foreign keys. Everything absent from this list is derived,
# scanned or ingested, and rebuilds from the profile.
TABLES = [
    ("source", "kind, locator, COALESCE(build_id,-1)"),
    ("format_field", "format_id, offset, name"),
    ("constant", "id"),
    ("decoded_enum", "id"),
    ("migration_source", "path, commit_sha"),
    ("migration_para", "migration_source_id, ordinal"),
    ("claim", "id"),
    ("citation", "claim_id, source_id"),
    ("discriminator", "id"),
    ("refutation", "id"),
    ("tautology", "id"),
    ("passage", "id"),
    ("gate_invariant", "id"),
]

DERIVED_NOTE = """\
# Bifrost knowledge dump. Committed so the hand-written layer is not confined to
# one gitignored database file.
#
# Rebuild the rest with:  bifrost bootstrap && bifrost seed && bifrost link
# Restore this with:      bifrost restore
#
# One JSON object per line, keys sorted, rows in a stable order. Do not edit by
# hand -- change the row and dump again.
"""


def dump(conn: sqlite3.Connection, out_dir: Path | None = None,
         verbose: bool = True) -> dict:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    d = Path(out_dir) if out_dir else (core.repo_root() / ".bifrost" / "dump")
    d.mkdir(parents=True, exist_ok=True)

    stats: dict[str, int] = {}
    for table, order in TABLES:
        rows = core.rows(conn, f"SELECT * FROM {table} ORDER BY {order}")
        lines = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows]
        (d / f"{table}.jsonl").write_text(
            DERIVED_NOTE + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        stats[table] = len(rows)
    log(f"[SUCCESS] dumped {sum(stats.values()):,} rows to {d}")
    for t, n in stats.items():
        if n:
            log(f"    {t:<18}{n:>7,}")
    return stats


def restore(conn: sqlite3.Connection, in_dir: Path | None = None,
            verbose: bool = True) -> dict:
    """Load a dump into a database that already has its scaffolding.

    Run `bootstrap` and `seed` first: this restores the knowledge layer, not the
    trees, gates or builds it refers to.
    """
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    d = Path(in_dir) if in_dir else (core.repo_root() / ".bifrost" / "dump")
    if not d.is_dir():
        raise core.BifrostError(f"no dump at {d}")

    stats: dict[str, int] = {}
    for table, _ in TABLES:
        f = d / f"{table}.jsonl"
        if not f.exists():
            continue
        n = 0
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            row = json.loads(line)
            cols = ",".join(row)
            marks = ",".join("?" * len(row))
            conn.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({marks})",
                         list(row.values()))
            n += 1
        stats[table] = n
    conn.commit()
    log(f"[SUCCESS] restored {sum(stats.values()):,} rows from {d}")
    return stats


def verify(conn: sqlite3.Connection, out_dir: Path | None = None) -> bool:
    """Dump twice and confirm the bytes match — the determinism the committed
    artefact depends on."""
    import tempfile
    a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    dump(conn, a, verbose=False)
    dump(conn, b, verbose=False)
    for table, _ in TABLES:
        fa, fb = a / f"{table}.jsonl", b / f"{table}.jsonl"
        if fa.exists() != fb.exists() or (fa.exists() and fa.read_bytes() != fb.read_bytes()):
            return False
    return True
