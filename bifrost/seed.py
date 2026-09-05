"""Bifrost seed — Wave 2, the mutable core.

Everything here is a judgement rather than a scan, which is why it is a separate
module from ingest.py and why every row carries a citation. Roughly fifty rows,
which was the estimate: the drift surface of this system is small precisely
because most of it is derived.

Sources are the roadmap as of commit 23340b0 plus the corpus runs and header
dumps made on 4 September 2026. Nothing below is inferred from a format that has
not been read -- where something is believed but untested it is recorded as
`assumed`, which is what puts it in v_claim_risk rather than in the status table.
"""

from __future__ import annotations

import sqlite3

from . import core, profile
from .core import utcnow

# ---------------------------------------------------------------------------
# formats — one per readable surface, anchored to its roadmap section
# ---------------------------------------------------------------------------

# FORMATS: project data, from the profile.


# ---------------------------------------------------------------------------
# capabilities — what the project is actually trying to be able to do
# ---------------------------------------------------------------------------

# CAPABILITIES: project data, from the profile.


# ---------------------------------------------------------------------------
# exceptions — named, justified, and counted rather than worked around (rule 4)
# ---------------------------------------------------------------------------

# EXCEPTIONS: project data, from the profile.


# ---------------------------------------------------------------------------
# claims — including the ones that are only ASSUMED, which is the point
# ---------------------------------------------------------------------------

# CLAIMS: project data, from the profile.


# TODOS: project data, from the profile.



def seed(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats: dict = {}

    # formats
    n = 0
    prof = profile.load()
    for name, tree, anchor, summary in prof.FORMATS:
        tid = None
        if tree:
            r = core.one(conn, "SELECT id FROM tree WHERE name=?", (tree,))
            tid = r["id"] if r else None
        conn.execute(
            """INSERT INTO format(name, tree_id, roadmap_anchor, summary) VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET tree_id=excluded.tree_id,
                 roadmap_anchor=excluded.roadmap_anchor, summary=excluded.summary""",
            (name, tid, anchor, summary))
        n += 1
    conn.commit()
    stats["formats"] = n
    log(f"[SUCCESS] formats: {n}")

    # capabilities and their gating edges
    caps = 0
    edges = 0
    for name, desc, gating in prof.CAPABILITIES:
        existing = core.get_id(conn, "capability", name)
        cap_id = existing or core.propose(conn, "capability", {"name": name, "description": desc})
        caps += 1
        for fmt in gating:
            fid = core.get_id(conn, "format", fmt)
            if fid is None:
                log(f"[ERROR] capability {name} gates unknown format {fmt}")
                continue
            conn.execute(
                """INSERT OR IGNORE INTO edge(src_type, src_id, kind, dst_type, dst_id,
                                              review_state, proposed_by, proposed_at, confirmed_at)
                   VALUES ('format',?,'gates','capability',?,'confirmed','seed',?,?)""",
                (fid, cap_id, utcnow(), utcnow()))
            edges += 1
    conn.commit()
    stats["capabilities"], stats["edges"] = caps, edges
    log(f"[SUCCESS] capabilities: {caps} ({edges} gating edges)")

    # exceptions
    exc = 0
    for name, disp, rationale, gates in prof.EXCEPTIONS:
        conn.execute(
            """INSERT INTO exception(name, disposition, rationale) VALUES (?,?,?)
               ON CONFLICT(name) DO UPDATE SET disposition=excluded.disposition,
                 rationale=excluded.rationale""",
            (name, disp, rationale))
        eid = core.get_id(conn, "exception", name)
        for g, expected in gates:
            gid = core.get_id(conn, "gate", g)
            if gid:
                conn.execute(
                    "INSERT OR REPLACE INTO exception_gate(exception_id, gate_id, "
                    "expected_failures) VALUES (?,?,?)", (eid, gid, expected))
        exc += 1
    conn.commit()
    stats["exceptions"] = exc
    log(f"[SUCCESS] exceptions: {exc}")

    # claims
    made = 0
    for c in prof.CLAIMS:
        fid = core.get_id(conn, "format", c["subject"])
        existing = core.one(conn, "SELECT id FROM claim WHERE statement=?", (c["statement"],))
        if existing:
            continue
        cid = core.record_claim(
            conn, subject_type="format", subject_id=fid, statement=c["statement"],
            asserted_status=c["status"], citations=c["citations"],
            created_by="seed", roadmap_anchor=c.get("anchor"))
        if "refute" in c:
            core.record_refutation(conn, refuted_claim=cid, **c["refute"])
        made += 1
    stats["claims"] = made
    log(f"[SUCCESS] claims: {made}")

    # todos, pre-confirmed: these came out of a reviewed status paper
    t = 0
    for prio, title, difficulty, rationale in prof.TODOS:
        if core.one(conn, "SELECT id FROM todo WHERE title=?", (title,)):
            continue
        tid = core.propose(conn, "todo", {"title": title, "rationale": rationale,
                                          "difficulty": difficulty, "priority": prio},
                           proposed_by="seed")
        core.review(conn, "todo", tid, "confirmed")
        t += 1
    stats["todos"] = t
    log(f"[SUCCESS] todos: {t}")
    return stats
