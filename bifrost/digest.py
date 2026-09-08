"""Bifrost digest — whole-project state, small enough to inject at session start.

This is the primary interface, and the schema was designed backwards from it.

The most expensive thing about arriving on this project is not answering a
question, it is not knowing there was a question. On 4 September 2026 an agent
spent a session discovering that embed_wi_v4 blocks every Wii level, noticed by
luck that a container assumption behind 175 nav files had never been tested, and
re-ran nine corpus gates because it could not prove the recorded results were
current. All three are rows below.

Hard budget: this must render in a few thousand tokens. If it cannot, the schema
is wrong, and `bifrost digest --check-budget` fails rather than quietly growing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import core

# Roughly four characters per token. Deliberately conservative: the digest is
# prepended to every session, so overrunning costs on every single turn.
CHARS_PER_TOKEN = 4
BUDGET_TOKENS = 3000


def digest_data(conn: sqlite3.Connection, *, limit: int = 12) -> dict[str, Any]:
    """The structured form. bifrost_digest over MCP returns this; render() below
    turns it into the text a SessionStart hook injects."""
    head = core.head_commit()
    dirty = core.tree_dirty()

    return {
        "commit": head[:7] if head else None,
        "dirty": dirty,
        "blocked": core.rows(conn, """
            SELECT capability, capability_status, blocking_format, format_status,
                   roadmap_anchor, tree, file_count
            FROM v_blocked ORDER BY capability, blocking_format"""),
        "stale": core.rows(conn, """
            SELECT name, reason, ts FROM v_gate_stale WHERE stale = 1
            ORDER BY (ts IS NULL) DESC, name LIMIT ?""", (limit,)),
        "risk": core.rows(conn, """
            SELECT statement, risk, severity, roadmap_anchor
            FROM v_claim_risk
            ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     id LIMIT ?""", (limit,)),
        "review": core.rows(conn, "SELECT kind, id, label, proposed_by FROM v_review_queue LIMIT ?",
                            (limit,)),
        "conflicts": core.rows(conn, """
            SELECT format, struct_name, name, offset, pdb_struct, pdb_field
            FROM v_field_pdb_conflict LIMIT ?""", (limit,)),
        "unsourced": core.one(conn, "SELECT COUNT(*) AS n FROM v_constant_unsourced")["n"],
        # A superseded run is a recording error that was corrected, not a result.
        # Listing both put a failing corpus_res in front of every session that
        # read this digest cold, when the gate had passed.
        "recent": core.rows(conn, """
            SELECT g.name, r.ts, r.ok, r.pass, r.fail, r.refused
            FROM gate_run r JOIN gate g ON g.id = r.gate_id
            WHERE r.id NOT IN (SELECT supersedes FROM gate_run
                                WHERE supersedes IS NOT NULL)
            ORDER BY r.ts DESC, r.id DESC LIMIT 6"""),
        "todos": core.rows(conn, """
            SELECT title, phase, difficulty, status FROM todo
            WHERE review_state='confirmed' AND status IN ('open','in_progress')
            ORDER BY COALESCE(priority, 999), id LIMIT ?""", (limit,)),
        "counts": {
            "trees": core.one(conn, "SELECT COUNT(*) n FROM tree")["n"],
            "files": core.one(conn, "SELECT COALESCE(SUM(file_count),0) n FROM tree")["n"],
            "formats": core.one(conn, "SELECT COUNT(*) n FROM format")["n"],
            "claims": core.one(conn, "SELECT COUNT(*) n FROM claim")["n"],
            "gates": core.one(conn, "SELECT COUNT(*) n FROM gate")["n"],
            "capabilities": core.one(conn, "SELECT COUNT(*) n FROM capability")["n"],
        },
    }


def _section(lines: list[str], title: str, rows: list, render_row, empty: str | None = None):
    if not rows:
        if empty:
            lines.append(f"{title} - {empty}")
            lines.append("")
        return
    lines.append(f"{title} ({len(rows)})")
    for r in rows:
        lines.append("  " + render_row(r))
    lines.append("")


def render(conn: sqlite3.Connection, *, limit: int = 12) -> str:
    d = digest_data(conn, limit=limit)
    c = d["counts"]
    L: list[str] = []

    L.append(f"BF3 CORPUS | Bifrost | {d['commit'] or 'no commit'}"
             + ("  (working tree dirty)" if d["dirty"] else ""))
    L.append(f"  {c['trees']} asset trees, {c['files']:,} files | {c['formats']} formats | "
             f"{c['claims']} claims | {c['gates']} gates | {c['capabilities']} capabilities")
    L.append("")

    _section(L, "BLOCKED", d["blocked"],
             lambda r: (f"{r['capability']:<26} <- {r['blocking_format']} "
                        f"[{r['format_status']}]"
                        + (f"  sec {r['roadmap_anchor']}" if r["roadmap_anchor"] else "")
                        + (f"  {r['file_count']:,} files" if r["file_count"] else "")),
             empty=("nothing blocked" if c["capabilities"]
                    else "no capabilities defined yet"))

    _section(L, "STALE GATES", d["stale"],
             lambda r: f"{r['name']:<20} {r['reason'] or ''}"[:110],
             empty="every gate current")

    _section(L, "CLAIMS AT RISK", d["risk"],
             lambda r: (f"[{r['severity']}] {r['statement'][:74]}"
                        + (f"\n        {r['risk']}" if r["risk"] else "")),
             empty=("no claims recorded yet" if not c["claims"] else "none"))

    _section(L, "PDB CONFLICTS", d["conflicts"],
             lambda r: f"{r['format']}.{r['name']} at +{r['offset']} disagrees with "
                       f"{r['pdb_struct']}.{r['pdb_field']}")

    _section(L, "AWAITING REVIEW", d["review"],
             lambda r: f"{r['kind']} #{r['id']}  {r['label'][:80]}")

    _section(L, "OPEN WORK", d["todos"],
             lambda r: f"[{r['status']}] {r['title'][:80]}"
                       + (f"  ({r['difficulty']})" if r["difficulty"] else ""))

    _section(L, "RECENT GATE RUNS", d["recent"],
             lambda r: (f"{'ok ' if r['ok'] else 'FAIL'} {r['name']:<20} "
                        f"{(r['pass'] if r['pass'] is not None else '-')!s:>7} pass  "
                        f"{(r['fail'] if r['fail'] is not None else '-')!s:>3} fail  {r['ts'][:16]}"),
             empty="no runs recorded")

    if d["unsourced"]:
        L.append(f"RULE 6: {d['unsourced']} constant(s) with no source. "
                 f"`bifrost query constant_unsourced`")
        L.append("")

    L.append("Query with `bifrost realm <name>` / `bifrost trace <node>` rather than "
             "reading generated docs.")
    return "\n".join(L)


def budget_report(text: str) -> dict:
    est = len(text) / CHARS_PER_TOKEN
    return {"chars": len(text), "est_tokens": round(est),
            "budget": BUDGET_TOKENS, "within": est <= BUDGET_TOKENS}
