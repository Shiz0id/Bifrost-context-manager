"""Bifrost — full-text search across everything that holds prose.

There was no way to ask "which claims mention decals". `realm` needs a name you
already have; `query` needs a view. The only route to a question of that shape
was dumping all 276 claims and grepping the file -- 48,000 tokens to find three
rows, and the most expensive gap a tester hit in a whole session.

What is searched, and why each earns its place:

    claim     the statements themselves, plus their citation locators, so
              "0x825ae690" and "decals" both find the claim
    comment   the code comments indexed by 010 -- often the only place a thing
              is explained at all
    format    name and summary, for when you half-remember what it was called
    todo      title and rationale
    source    locators and resolved symbol names

Results are deliberately COMPACT. A search that returns whole rows reproduces
the problem it exists to solve, so a hit is an id, a one-line label and a
snippet; `realm`, `query` or `comments` gets the rest once you know which row
you want.
"""

from __future__ import annotations

import re
import sqlite3

from . import core
from .core import BifrostError

# fts5 treats these as syntax. A caller typing `obdef_s.numParts` or
# `0x825ae690` means them literally, and a bare quote or paren is a hard error
# rather than a no-hit, so the query is quoted unless it is deliberate syntax.
_FTS_SYNTAX = re.compile(r'^\s*(?:NOT|\()|["*:^]| (?:AND|OR|NOT) ')


def _prepare(q: str) -> str:
    q = q.strip()
    if not q:
        raise BifrostError("nothing to search for")
    if _FTS_SYNTAX.search(q):
        return q
    # Quote each bare term: "obdef_s.numParts" is one token to a human and three
    # to fts5, and the dotted form is what people actually type.
    return " ".join(f'"{t}"' for t in q.split())


def reindex(conn: sqlite3.Connection) -> dict:
    """Rebuild the whole index. Cheap: ~1,500 rows, milliseconds."""
    conn.execute("DELETE FROM search_index")
    n: dict = {}

    rows = core.rows(conn, """
        SELECT c.id, c.statement, c.layer, v.effective_status,
               COALESCE(f.name, '') AS subject,
               (SELECT group_concat(s.locator, ' ') FROM citation ci
                  JOIN source s ON s.id = ci.source_id WHERE ci.claim_id = c.id) AS locators,
               (SELECT group_concat(s.symbol, ' ') FROM citation ci
                  JOIN source s ON s.id = ci.source_id WHERE ci.claim_id = c.id) AS symbols
        FROM claim c
        JOIN v_claim_status v ON v.id = c.id
        LEFT JOIN format f ON f.id = c.subject_id AND c.subject_type = 'format'""")
    for r in rows:
        conn.execute(
            "INSERT INTO search_index(kind, ref, title, body, extra) VALUES ('claim',?,?,?,?)",
            (str(r["id"]), (r["subject"] or "claim"),
             " ".join(filter(None, (r["statement"], r["locators"], r["symbols"]))),
             f"{r['effective_status']} | {r['layer']}"))
    n["claim"] = len(rows)

    rows = core.rows(conn, "SELECT id, path, line, prose FROM code_comment")
    for r in rows:
        conn.execute(
            "INSERT INTO search_index(kind, ref, title, body, extra) VALUES ('comment',?,?,?,?)",
            (str(r["id"]), f"{r['path']}:{r['line']}", r["prose"], r["path"]))
    n["comment"] = len(rows)

    # status is derived, so it comes from the view rather than the table.
    rows = core.rows(conn, """
        SELECT f.id, f.name, COALESCE(f.summary,'') summary,
               COALESCE(v.status,'') status
        FROM format f LEFT JOIN v_format_status v ON v.name = f.name""")
    for r in rows:
        conn.execute(
            "INSERT INTO search_index(kind, ref, title, body, extra) VALUES ('format',?,?,?,?)",
            (str(r["id"]), r["name"], f"{r['name']} {r['summary']}", r["status"]))
    n["format"] = len(rows)

    rows = core.rows(conn, "SELECT id, title, COALESCE(rationale,'') rationale, status FROM todo")
    for r in rows:
        conn.execute(
            "INSERT INTO search_index(kind, ref, title, body, extra) VALUES ('todo',?,?,?,?)",
            (str(r["id"]), r["title"], f"{r['title']} {r['rationale']}", r["status"]))
    n["todo"] = len(rows)

    rows = core.rows(conn, """SELECT id, kind, locator, COALESCE(symbol,'') symbol,
                                     COALESCE(note,'') note FROM source""")
    for r in rows:
        conn.execute(
            "INSERT INTO search_index(kind, ref, title, body, extra) VALUES ('source',?,?,?,?)",
            (str(r["id"]), r["locator"], f"{r['locator']} {r['symbol']} {r['note']}", r["kind"]))
    n["source"] = len(rows)

    conn.commit()
    n["total"] = sum(v for k, v in n.items() if k != "total")
    return n


def search(conn: sqlite3.Connection, query: str, *, kind: str | None = None,
           limit: int = 20, snippet_chars: int = 160) -> list[dict]:
    """Compact hits, best first. Never whole rows -- that is the thing this
    exists to avoid."""
    sql = ["SELECT kind, ref, title, extra,",
           "       snippet(search_index, 3, '', '', ' ... ', 12) AS snippet",
           "FROM search_index WHERE search_index MATCH ?"]
    params: list = [_prepare(query)]
    if kind:
        sql.append("AND kind = ?")
        params.append(kind)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(limit)

    try:
        hits = core.rows(conn, "\n".join(sql), params)
    except sqlite3.OperationalError as e:
        raise BifrostError(f"bad search query: {e}")
    for h in hits:
        if h["snippet"] and len(h["snippet"]) > snippet_chars:
            h["snippet"] = h["snippet"][:snippet_chars - 1] + "…"
    return hits
