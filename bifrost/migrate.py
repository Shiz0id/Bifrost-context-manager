"""Bifrost Wave 3 — turning the roadmap into rows.

The roadmap is rewritten into claims and passages and the original file is then
deleted. That is a one-way door, so the safety net is built first and it is built
out of git: `migration_source` records the commit the file is discarded at, and
`git show <sha>:bf3_execution_roadmap.md` recovers it forever at no storage cost.
The coverage gate compares against that, not against a copy anyone has to keep.

Prose may be rewritten. Evidence may not. That is the whole rule, and it is
mechanically checkable because in this project the citations ARE the evidence:

    for every paragraph p,
        citations(p)  is a subset of  the union of citations of the claims
                                       derived from p

Any address, symbol, path or figure that fails to survive the rewrite is a gate
failure naming the paragraph it was lost from.

Three things here are mechanical and one is not. Splitting the document,
extracting citation tokens and validating struct layouts against the retail PDB
all run without judgement. Deciding what a paragraph ASSERTS does not, and that
is left to `bifrost_record_claim`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import core
from .core import BifrostError, utcnow

def document() -> Path | None:
    """The markdown this project renders out of the database."""
    from . import profile
    return profile.load().document

# Addresses at or above this in the 360 image are text; below is a constant
# pool, jump table or string blob, which is a different KIND of citation rather
# than a failed one. See README, "Provenance".
XENON_TEXT_BASE = 0x82200000
XENON_LO, XENON_HI = 0x82000000, 0x83000000
BROADWAY_LO, BROADWAY_HI = 0x80000000, 0x81000000


# ---------------------------------------------------------------------------
# document model
# ---------------------------------------------------------------------------

BLOCK_KINDS = ("heading", "code", "table", "list", "prose", "rule")


@dataclass
class Block:
    ordinal: int
    kind: str
    text: str
    anchor: Optional[str] = None      # the section number it sits under, e.g. "6.13"
    tokens: list = field(default_factory=list)


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# "### 6.13 Materials - solved" / "## 9. Level instantiation" / "### 5.1 `.res`"
_ANCHOR = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+[a-z]?)?)[.\s]")


def split_blocks(text: str) -> list[Block]:
    """Split the document into blocks, respecting fenced code.

    A naive split on blank lines tears every code block in the file apart --
    the roadmap's format layouts are full of them -- and then the citation
    extractor sees half a struct.
    """
    lines = text.split("\n")
    blocks: list[Block] = []
    buf: list[str] = []
    kind = "prose"
    anchor: Optional[str] = None
    in_code = False

    def flush():
        nonlocal buf, kind
        if buf and any(l.strip() for l in buf):
            blocks.append(Block(len(blocks), kind, "\n".join(buf).strip(), anchor))
        buf = []
        kind = "prose"

    for line in lines:
        fence = line.lstrip().startswith("```")
        if fence:
            if not in_code:
                flush()
                in_code = True
                kind = "code"
                buf.append(line)
            else:
                buf.append(line)
                in_code = False
                flush()
            continue

        if in_code:
            buf.append(line)
            continue

        if not line.strip():
            flush()
            continue

        if _HEADING.match(line):
            flush()
            m = _ANCHOR.match(line)
            if m:
                anchor = m.group(1)
            blocks.append(Block(len(blocks), "heading", line.strip(), anchor))
            continue

        if line.strip().startswith("|"):
            if kind != "table":
                flush()
                kind = "table"
            buf.append(line)
            continue

        if re.match(r"^\s*([-*]|\d+\.)\s+\S", line):
            if kind not in ("list",):
                flush()
                kind = "list"
            buf.append(line)
            continue

        if re.match(r"^\s*(-{3,}|={3,})\s*$", line):
            flush()
            blocks.append(Block(len(blocks), "rule", line.strip(), anchor))
            continue

        if kind == "table":            # a table ended without a blank line
            flush()
        buf.append(line)

    flush()
    return blocks


# ---------------------------------------------------------------------------
# citation extraction
# ---------------------------------------------------------------------------

_ADDR = re.compile(r"0x[0-9A-Fa-f]{6,8}")
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_PATH = re.compile(r"(?:assets|data|gendata|pak|build|tools|src|include|ref)/[\w./*-]+")
_FIGURE = re.compile(r"\*\*([\d][\d,]{2,})\*\*|\b(\d{1,3}\.\d{1,2})%")
_STRUCT = re.compile(r"^\w+_s$")
_FUNCLIKE = re.compile(r"^(?:[a-z]+[A-Z]\w*|C[A-Z]\w*(?:::\w+)?|\w+::\w+)$")


def classify_address(va: int) -> tuple[str, Optional[str]]:
    if BROADWAY_LO <= va < BROADWAY_HI:
        return ("disasm_fn", "bf3_wii")
    if XENON_LO <= va < XENON_HI:
        return ("disasm_fn" if va >= XENON_TEXT_BASE else "data_addr", "bf3_360")
    return ("data_addr", None)


def extract_tokens(text: str) -> list[dict]:
    """Every citation-like token in a block, typed.

    Deliberately high-recall: this set is what the preservation gate holds the
    rewrite to, so a token missed here is a piece of evidence the gate will not
    notice being dropped.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    def add(kind: str, locator: str, build: str | None = None):
        key = (kind, locator)
        if key not in seen:
            seen.add(key)
            out.append({"kind": kind, "locator": locator, **({"build": build} if build else {})})

    for m in _ADDR.finditer(text):
        va = int(m.group(0), 16)
        kind, build = classify_address(va)
        add(kind, m.group(0).lower(), build)

    for m in _BACKTICK.finditer(text):
        tok = m.group(1).strip()
        if _STRUCT.match(tok):
            add("pdb_type", tok)
        elif _FUNCLIKE.match(tok) and len(tok) > 3:
            add("map_symbol", tok)

    for m in _PATH.finditer(text):
        p = m.group(0).rstrip(".,;)")
        add("build_manifest" if "buildfiles" in p else
            "shipped_shader" if "shaders" in p else "shipped_file", p)

    for m in _FIGURE.finditer(text):
        add("measurement", (m.group(1) or m.group(2) + "%"))

    return out


# ---------------------------------------------------------------------------
# struct layouts out of code blocks
# ---------------------------------------------------------------------------

# "  +00 u8  flags   +0c f32 endTime"  /  "+0x0C float4 inv_qRot"
#
# The offset alternative must accept BARE HEX. An earlier version allowed only
# `0x...` or pure digits, so every field line carrying a hex letter -- "+0c f32
# endTime", "+3c ptr parts" -- matched nothing and was dropped without a word.
# Silently losing evidence is the one thing this migration must not do, and it
# also disabled the hex detection below, since the letters never got seen.
_FIELD = re.compile(
    r"\+(0x[0-9A-Fa-f]{1,4}|[0-9A-Fa-f]{1,4})\s+"
    r"((?:u8|s8|u16|s16|u32|s32|u64|f32|float\d?|char\[\d+\]|ptr|int|short|"
    r"unsigned\s+\w+|float3|float4|vec3|u16\[\d+\]|s16\[\d+\])\b)\s+"
    r"([A-Za-z_][\w\[\]]*)")

# "animdef_s  0x1c   +00 u8 flags"  -- a struct named at the head of the block
_STRUCT_DECL = re.compile(r"^\s*(\w+_s)\b", re.M)


def extract_fields(code: str) -> list[dict]:
    """Candidate field rows from a format-layout code block.

    The base is the trap. These blocks write offsets in HEX with no 0x prefix --
    section 5.7's animdef_s runs "+08 f32 startTime  +0c f32 endTime  +10 float3
    baseTranslation" -- so reading them as decimal puts baseTranslation at 10
    where the retail PDB has it at 16. Read as decimal the whole struct is wrong
    from the first field past +09 and every one of those errors looks plausible.

    Two signals, in order:
      1. any offset in the block carrying a hex letter settles it for the block
      2. otherwise decimal, provisionally -- validate_fields flips it if the PDB
         says hex is what agrees, and records that it did
    """
    body = re.sub(r"^```.*$", "", code, flags=re.M)
    sm = _STRUCT_DECL.search(body)
    struct = sm.group(1) if sm else None

    raws = [m.group(1) for m in _FIELD.finditer(body)]
    letters = any(re.search(r"[a-fA-F]", r) and not r.startswith("0x") for r in raws)

    out = []
    for m in _FIELD.finditer(body):
        raw = m.group(1)
        if raw.startswith("0x"):
            off, base = int(raw, 16), "0x"
        elif letters:
            off, base = int(raw, 16), "hex"
        else:
            off, base = int(raw), "dec?"
        out.append({
            "struct_name": struct, "offset": off, "offset_raw": raw, "base": base,
            "ctype": m.group(2).strip(), "name": m.group(3),
        })
    return out


def validate_fields(conn: sqlite3.Connection, fields: Iterable[dict]) -> list[dict]:
    """Check each recorded offset against the retail PDB where it names one.

    This is the check with teeth on a transcription error: the PDB is
    independent of the roadmap, so a field the document recorded at the wrong
    offset surfaces here rather than in a renderer six months later.
    """
    out = []
    for f in fields:
        f = dict(f)
        f["pdb_validated"] = None
        f["pdb_note"] = None
        struct = f.get("struct_name")
        if struct and core.has_symbols(conn):
            rows = core.rows(
                conn, "SELECT name, offset, size FROM sym.fields WHERE class = ?", (struct,))
            if rows:
                base = f["name"].split("[")[0].lower()
                match = next((r for r in rows
                              if r["name"].split("[")[0].lower() == base), None)
                if match:
                    f["pdb_struct"], f["pdb_field"] = struct, match["name"]
                    if match["offset"] == f["offset"]:
                        f["pdb_validated"] = 1
                    elif (f.get("base") == "dec?"
                          and int(f["offset_raw"], 16) == match["offset"]):
                        # An unprefixed block with no hex letters in it, whose
                        # offsets only line up when read as hex. The PDB is the
                        # arbiter; record that the base was inferred, not read.
                        f["offset"] = match["offset"]
                        f["base"] = "hex (inferred from the PDB)"
                        f["pdb_validated"] = 1
                        f["pdb_note"] = (f"offset base was ambiguous; +{f['offset_raw']} "
                                         f"read as hex to agree with the PDB")
                    else:
                        f["pdb_validated"] = 0
                        f["pdb_note"] = (f"document says +{f['offset']} "
                                         f"(from '+{f['offset_raw']}', base {f['base']}), "
                                         f"PDB says +{match['offset']}")
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def _blob_commit(path: Path) -> str:
    """The commit whose blob of this file the gate will compare against.

    HEAD, not the last commit touching the file: after the file is deleted, the
    recoverable content is whatever HEAD held when it was ingested.
    """
    return core.head_commit()


def ingest_document(conn: sqlite3.Connection, path: Path | None = None,
                    note: str | None = None) -> dict:
    """Load a document into migration_source / migration_para."""
    p = Path(path) if path else document()
    if p is None:
        raise BifrostError('this project declares no DOCUMENT in its profile')
    if not p.exists():
        raise BifrostError(f"no document at {p}")
    text = p.read_text(encoding="utf-8")
    rel = str(p.relative_to(core.repo_root())).replace("\\", "/")
    sha = _blob_commit(p)

    conn.execute(
        """INSERT INTO migration_source(path, commit_sha, note, ingested_at)
           VALUES (?,?,?,?)
           ON CONFLICT(path, commit_sha) DO UPDATE SET note=excluded.note""",
        (rel, sha, note or "recover with: git show %s:%s" % (sha[:12], rel), utcnow()))
    src = core.one(conn, "SELECT id FROM migration_source WHERE path=? AND commit_sha=?",
                   (rel, sha))["id"]

    blocks = split_blocks(text)
    conn.execute("DELETE FROM migration_para WHERE migration_source_id=?", (src,))
    for b in blocks:
        b.tokens = extract_tokens(b.text)
        # Headings and horizontal rules carry no evidence and need no claim.
        pre_accounted = b.kind in ("heading", "rule") and not b.tokens
        conn.execute(
            """INSERT INTO migration_para(migration_source_id, ordinal, text,
                                          citation_tokens, accounted, accounted_kind,
                                          anchor)
               VALUES (?,?,?,?,?,?,?)""",
            (src, b.ordinal, b.text, json.dumps(b.tokens),
             1 if pre_accounted else 0, b.kind if pre_accounted else None, b.anchor))
    conn.commit()

    kinds: dict[str, int] = {}
    for b in blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    return {
        "source_id": src, "path": rel, "commit": sha,
        "blocks": len(blocks), "kinds": kinds,
        "with_tokens": sum(1 for b in blocks if b.tokens),
        "tokens": sum(len(b.tokens) for b in blocks),
        "distinct_tokens": len({(t["kind"], t["locator"]) for b in blocks for t in b.tokens}),
    }


def account(conn: sqlite3.Connection, para_id: int, kind: str,
            claim_ids: Iterable[int] = ()) -> None:
    """Mark a paragraph as handled, by claims or as a retained passage."""
    if kind not in ("claim", "passage", "table", "code", "heading", "skipped"):
        raise BifrostError(f"bad accounted_kind {kind!r}")
    conn.execute("UPDATE migration_para SET accounted=1, accounted_kind=? WHERE id=?",
                 (kind, para_id))
    for cid in claim_ids:
        conn.execute("UPDATE claim SET para_id=? WHERE id=?", (para_id, cid))
    conn.commit()


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------

def coverage(conn: sqlite3.Connection) -> list[dict]:
    return core.rows(conn, "SELECT * FROM v_migration_coverage")


def unaccounted(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return core.rows(conn, """
        SELECT id, ordinal, accounted_kind, substr(text,1,110) AS head,
               json_array_length(citation_tokens) AS n_tokens
        FROM migration_para WHERE accounted = 0
        ORDER BY n_tokens DESC, ordinal LIMIT ?""", (limit,))


def citation_preservation(conn: sqlite3.Connection) -> dict:
    """The gate with teeth on the rewrite.

    Every citation token a paragraph carried must survive into some claim or
    passage derived from it. Prose is free to change; evidence is not. A
    paragraph rewritten into a claim that quietly drops the address it rested on
    is exactly the drift this migration could otherwise introduce, and nothing
    else would catch it.
    """
    lost: list[dict] = []
    checked = kept = 0

    for para in core.rows(conn, """
            SELECT id, ordinal, citation_tokens, accounted, accounted_kind,
                   substr(text,1,90) AS head
            FROM migration_para WHERE accounted = 1"""):
        want = {(t["kind"], t["locator"]) for t in json.loads(para["citation_tokens"])}
        if not want:
            continue

        # A retained passage keeps its prose verbatim, so it keeps its evidence
        # by construction; only rewrites into claims can drop anything.
        if para["accounted_kind"] == "passage":
            checked += len(want)
            kept += len(want)
            continue

        have = {(r["kind"], r["locator"]) for r in core.rows(conn, """
            SELECT s.kind, s.locator FROM claim c
            JOIN citation ct ON ct.claim_id = c.id
            JOIN source s ON s.id = ct.source_id
            WHERE c.para_id = ?""", (para["id"],))}

        checked += len(want)
        kept += len(want & have)
        for k, loc in sorted(want - have):
            lost.append({"para_id": para["id"], "ordinal": para["ordinal"],
                         "kind": k, "locator": loc, "head": para["head"]})

    return {"tokens_checked": checked, "tokens_kept": kept,
            "lost": lost, "ok": not lost}


def report(conn: sqlite3.Connection) -> str:
    L = []
    for c in coverage(conn):
        L.append(f"{c['path']} @ {c['commit_sha'][:7]}")
        L.append(f"  paragraphs {c['paragraphs']}, accounted {c['accounted']}, "
                 f"unaccounted {c['unaccounted']}  ({c['pct']}%)")
    kinds = core.rows(conn, """
        SELECT accounted_kind AS k, COUNT(*) n FROM migration_para
        WHERE accounted=1 GROUP BY accounted_kind ORDER BY n DESC""")
    if kinds:
        L.append("  by kind: " + ", ".join(f"{r['k']} {r['n']}" for r in kinds))

    pres = citation_preservation(conn)
    L.append(f"  citation preservation: {pres['tokens_kept']}/{pres['tokens_checked']} kept"
             + ("" if pres["ok"] else f", {len(pres['lost'])} LOST"))
    for l in pres["lost"][:8]:
        L.append(f"    para {l['ordinal']}: dropped {l['kind']} {l['locator']}  -- {l['head']}")

    left = unaccounted(conn, 6)
    if left:
        L.append(f"  next unaccounted (by evidence density):")
        for u in left:
            L.append(f"    #{u['ordinal']:<4} {u['n_tokens']:>2} tokens  {u['head']}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# the mechanical pass
# ---------------------------------------------------------------------------

def persist_fields(conn: sqlite3.Connection, para_id: int) -> int:
    """Turn a layout code block into format_field rows, validated against the PDB.

    The format is resolved from the paragraph's own section anchor, which is why
    migration 004 carries it: the document has to stop being the thing that knows.
    """
    para = core.one(conn, "SELECT text, anchor FROM migration_para WHERE id=?", (para_id,))
    if not para or not para["text"].startswith("```"):
        return 0
    fmt = core.one(conn, "SELECT id FROM format WHERE roadmap_anchor = ?", (para["anchor"],))
    if not fmt:
        return 0

    n = 0
    for f in validate_fields(conn, extract_fields(para["text"])):
        if core.one(conn, """SELECT id FROM format_field WHERE format_id=? AND name=?
                             AND offset=? AND COALESCE(struct_name,'')=COALESCE(?,'')""",
                    (fmt["id"], f["name"], f["offset"], f.get("struct_name"))):
            continue
        conn.execute(
            """INSERT INTO format_field(format_id, struct_name, offset, ctype, name,
                                        pdb_struct, pdb_field, pdb_validated, pdb_note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (fmt["id"], f.get("struct_name"), f["offset"], f["ctype"], f["name"],
             f.get("pdb_struct"), f.get("pdb_field"), f.get("pdb_validated"),
             f.get("pdb_note")))
        n += 1
    conn.commit()
    return n


def make_passage(conn: sqlite3.Connection, para_id: int) -> int:
    """Retain a block verbatim and mark it accounted.

    Code blocks, tables and narrative prose land here. Verbatim retention keeps
    their evidence by construction, which is why the preservation gate trusts a
    passage and audits a claim: over-preserving is safe, under-preserving is the
    failure this migration actually risks.
    """
    para = core.one(conn, "SELECT id, text, anchor FROM migration_para WHERE id=?", (para_id,))
    if not para:
        raise BifrostError(f"no paragraph #{para_id}")
    fmt = core.one(conn, "SELECT id FROM format WHERE roadmap_anchor = ?", (para["anchor"],))
    fid = fmt["id"] if fmt else None
    ordinal = core.one(conn, """SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM passage
                                WHERE subject_type=? AND subject_id IS ?""",
                       ("format" if fid else "corpus", fid))["n"]
    cur = conn.execute(
        """INSERT INTO passage(subject_type, subject_id, ordinal, prose,
                               roadmap_anchor, para_id)
           VALUES (?,?,?,?,?,?)""",
        ("format" if fid else "corpus", fid, ordinal, para["text"],
         para["anchor"], para_id))
    account(conn, para_id, "passage")
    return int(cur.lastrowid)


def claim_from_para(conn: sqlite3.Connection, para_id: int, statement: str,
                    asserted_status: str = "plausible",
                    extra_citations: Iterable[dict] = ()) -> int:
    """Record a claim that INHERITS its paragraph's citation tokens.

    This is what makes per-assertion granularity affordable. The evidence was
    already extracted mechanically and is already right; only the statement needs
    judgement. Inheriting rather than retyping also satisfies the preservation
    gate by construction for the ordinary case, so a citation can only be dropped
    deliberately.
    """
    para = core.one(conn, """SELECT id, anchor, citation_tokens
                             FROM migration_para WHERE id=?""", (para_id,))
    if not para:
        raise BifrostError(f"no paragraph #{para_id}")
    cites = json.loads(para["citation_tokens"]) + list(extra_citations)
    if not cites:
        raise BifrostError(
            f"paragraph #{para_id} carries no citation token, so it cannot become a "
            f"claim. Retain it with make_passage() instead.")

    fmt = core.one(conn, "SELECT id FROM format WHERE roadmap_anchor = ?", (para["anchor"],))
    cid = core.record_claim(
        conn, subject_type="format" if fmt else "corpus",
        subject_id=fmt["id"] if fmt else None,
        statement=statement, citations=cites,
        asserted_status=asserted_status, created_by="migration",
        roadmap_anchor=para["anchor"], para_id=para_id)
    account(conn, para_id, "claim", [cid])
    return cid


def orphan_assertions(conn: sqlite3.Connection, min_len: int = 90) -> list[dict]:
    """Uncited prose that directly follows a CITED block in the same section.

    The blind spot of paragraph-level extraction. The roadmap routinely states a
    reading in one paragraph and its evidence in the one before -- section 6.4
    puts `obGetVertexPosHW` and its layout in two blocks and then asserts "there
    is no per-model scale factor" in a third, which carries no citable token of
    its own and so falls out as narrative.

    These are not errors, they are the cases a human has to look at: some really
    are narrative and some are among the most important readings in the document.
    Reporting them is the point -- an invisible gap is the one that stays open.
    """
    rows = core.rows(conn, """SELECT id, ordinal, anchor, accounted_kind, text,
                                     citation_tokens
                              FROM migration_para ORDER BY ordinal""")
    out, prev = [], None
    for r in rows:
        prose = not r["text"].startswith(("```", "|", "#", "---", "- ", "* "))
        if (prose and r["accounted_kind"] == "passage"
                and not json.loads(r["citation_tokens"])
                and len(r["text"]) > min_len
                and prev and json.loads(prev["citation_tokens"])
                and prev["anchor"] == r["anchor"]):
            out.append({"id": r["id"], "ordinal": r["ordinal"], "anchor": r["anchor"],
                        "text": r["text"], "inherits_from": prev["id"]})
        prev = r
    return out


def promote_passage(conn: sqlite3.Connection, para_id: int, statement: str,
                    asserted_status: str = "plausible",
                    inherit_from: int | None = None) -> int:
    """Turn a retained passage into a claim, inheriting a neighbour's citations.

    An orphaned assertion has no citation token of its own, so it must borrow the
    evidence of the block it continues from -- which is exactly how the document
    reads it. `inherit_from` defaults to the nearest preceding cited paragraph in
    the same section.
    """
    para = core.one(conn, """SELECT id, ordinal, anchor, accounted_kind
                             FROM migration_para WHERE id=?""", (para_id,))
    if not para:
        raise BifrostError(f"no paragraph #{para_id}")

    if inherit_from is None:
        # The nearest THREE cited blocks in the same section, not just one.
        # Section 6.4 is the shape this has to handle: prose names
        # obGetVertexPosHW, a code block gives the layout and its 1/1024
        # constant, and only then does a third paragraph assert "there is no
        # per-model scale factor". Inheriting one block back would cite the
        # constant and lose the function the reading actually came from.
        #
        # Three rather than the whole section: a long section like 6.13 would
        # otherwise hang twenty citations off every assertion in it, which makes
        # the evidence unreadable and the preservation gate meaningless.
        prevs = core.rows(conn, """SELECT id, citation_tokens FROM migration_para
                                   WHERE ordinal < ? AND anchor IS ?
                                     AND json_array_length(citation_tokens) > 0
                                   ORDER BY ordinal DESC LIMIT 3""",
                          (para["ordinal"], para["anchor"]))
        if not prevs:
            raise BifrostError(
                f"paragraph #{para_id} has no cited neighbour in section "
                f"{para['anchor']} to inherit from; cite it explicitly instead")
        sources = prevs
    else:
        sources = core.rows(conn, "SELECT id, citation_tokens FROM migration_para "
                                  "WHERE id=?", (inherit_from,))

    seen: set[tuple] = set()
    cites: list[dict] = []
    for src in sources:
        for t in json.loads(src["citation_tokens"]):
            key = (t["kind"], t["locator"])
            if key not in seen:
                seen.add(key)
                cites.append(t)
    if not cites:
        raise BifrostError(f"nothing to inherit for paragraph #{para_id}")

    fmt = core.one(conn, "SELECT id FROM format WHERE roadmap_anchor = ?", (para["anchor"],))
    cid = core.record_claim(
        conn, subject_type="format" if fmt else "corpus",
        subject_id=fmt["id"] if fmt else None,
        statement=statement, citations=cites, asserted_status=asserted_status,
        created_by="migration", roadmap_anchor=para["anchor"], para_id=para_id)

    # the passage it was is retired: the paragraph is now stated as a claim
    conn.execute("DELETE FROM passage WHERE para_id = ?", (para_id,))
    account(conn, para_id, "claim", [cid])
    return cid


def topup_inherited_citations(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    """Give every promoted claim the full local evidence of its section.

    A claim whose own paragraph carries no citation token inherited its evidence
    from a neighbour. When that inheritance rule widens -- as it did from one
    preceding block to the nearest three -- the claims already promoted keep the
    narrower set, and nothing else would ever revisit them.

    Idempotent: only missing citations are added, and a claim that already has
    everything its section offers is left alone.
    """
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    added = touched = 0

    for c in core.rows(conn, """
            SELECT c.id, p.id AS para_id, p.ordinal, p.anchor
            FROM claim c JOIN migration_para p ON p.id = c.para_id
            WHERE json_array_length(p.citation_tokens) = 0"""):
        prevs = core.rows(conn, """SELECT citation_tokens FROM migration_para
                                   WHERE ordinal < ? AND anchor IS ?
                                     AND json_array_length(citation_tokens) > 0
                                   ORDER BY ordinal DESC LIMIT 3""",
                          (c["ordinal"], c["anchor"]))
        have = {(r["kind"], r["locator"]) for r in core.rows(conn, """
            SELECT s.kind, s.locator FROM citation ct JOIN source s ON s.id = ct.source_id
            WHERE ct.claim_id = ?""", (c["id"],))}

        before = added
        for p in prevs:
            for t in json.loads(p["citation_tokens"]):
                if (t["kind"], t["locator"]) in have:
                    continue
                have.add((t["kind"], t["locator"]))
                sid = core.ensure_source(conn, t["kind"], t["locator"], t.get("build"))
                conn.execute(
                    "INSERT OR IGNORE INTO citation(claim_id, source_id, note) VALUES (?,?,?)",
                    (c["id"], sid, "inherited from the section's preceding evidence"))
                added += 1
        if added > before:
            touched += 1
    conn.commit()
    log(f"[SUCCESS] {added} citations added across {touched} promoted claims")
    return {"added": added, "claims": touched}


def mechanical_pass(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    """Everything that needs no judgement.

    Code blocks and tables become passages, and code blocks additionally yield
    validated field rows. Prose carrying no citation token becomes a passage too:
    with no evidence in it there is nothing for a claim to rest on.

    A LIST is deliberately not swept up. An uncited bullet has no evidence and
    falls out as narrative anyway, but a cited one is an assertion like any other
    -- section 2.1's "the devkit key (16 zero bytes) decrypts the session key" is
    a bullet, and it is exactly the kind of thing that must become a claim rather
    than be retained as decoration.
    What is left afterwards is exactly the set of assertions somebody has to read
    and state, which is the only genuinely expensive part of the migration.
    """
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats = {"passages": 0, "fields": 0, "left": 0}

    for para in core.rows(conn, """
            SELECT id, kind_guess FROM (
                SELECT id,
                       CASE WHEN text LIKE '```%' THEN 'code'
                            WHEN text LIKE '|%'   THEN 'table'
                            WHEN json_array_length(citation_tokens) = 0 THEN 'narrative'
                            ELSE 'assertion' END AS kind_guess
                FROM migration_para WHERE accounted = 0)
            WHERE kind_guess != 'assertion'"""):
        if para["kind_guess"] == "code":
            stats["fields"] += persist_fields(conn, para["id"])
        make_passage(conn, para["id"])
        stats["passages"] += 1

    stats["left"] = core.one(
        conn, "SELECT COUNT(*) n FROM migration_para WHERE accounted=0")["n"]
    log(f"[SUCCESS] mechanical pass: {stats['passages']} passages retained, "
        f"{stats['fields']} validated field rows")
    log(f"[INFO] {stats['left']} paragraphs carry evidence and need a statement")
    return stats
