"""Bifrost Wave 3 tests — document splitting, citation extraction, the
preservation gate, and deterministic rendering.

The two that carry the weight are test_citation_preservation_catches_a_drop and
test_render_is_deterministic. The first is the only thing standing between a
rewrite and quietly losing evidence; the second is the only thing keeping a
committed generated file reviewable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, migrate, render  # noqa: E402
from bifrost.core import BifrostError  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def fresh():
    conn = core.connect(":memory:")
    core.migrate(conn)
    return conn


DOC = """# Title

## 1. Method

Some narrative with no evidence in it at all.

Positions dequantise by 1/1024, from `obGetVertexPosHW` (0x825ae690).

```
animdef_s  0x1c   +00 u8  flags          +01 u8  numTags
                  +08 f32 startTime      +0c f32 endTime
                  +10 float3 baseTranslation
```

| tree | files |
| :--- | ---: |
| `assets/bf/ob_xb_v184` | 5,314 |

- a list item citing 0x8263EF70
"""


def _load(conn, text=DOC):
    p = core.repo_root() / "build" / "_test_doc.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    try:
        return migrate.ingest_document(conn, p)
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------

def test_split_respects_fenced_code():
    blocks = migrate.split_blocks(DOC)
    kinds = [b.kind for b in blocks]
    check("code" in kinds, f"no code block found: {kinds}")
    code = next(b for b in blocks if b.kind == "code")
    check("animdef_s" in code.text and "baseTranslation" in code.text,
          "the code block was torn apart by the blank-line split")
    check(code.text.count("```") == 2, "the fence must be kept whole")
    table = next(b for b in blocks if b.kind == "table")
    check(table.text.count("|") > 4, table.text)
    return f"{len(blocks)} blocks: {', '.join(sorted(set(kinds)))}"


def test_anchor_tracking():
    blocks = migrate.split_blocks(DOC)
    cited = next(b for b in blocks if "obGetVertexPosHW" in b.text)
    check(cited.anchor == "1", f"expected section 1, got {cited.anchor!r}")


def test_address_classification():
    # text range -> a function; below it -> a constant pool, which is a DIFFERENT
    # kind of citation rather than a failed one
    check(migrate.classify_address(0x825AE690) == ("disasm_fn", "bf3_360"), "text")
    check(migrate.classify_address(0x82068EE4) == ("data_addr", "bf3_360"), "data")
    check(migrate.classify_address(0x8044E160) == ("disasm_fn", "bf3_wii"), "wii")


def test_token_extraction():
    toks = migrate.extract_tokens(
        "`obGetVertexPosHW` (0x825ae690) reads `animdef_s` from "
        "`assets/bf/ob_xb_v184/x.rax`, over **14,021,613** vertices, 99.51% of them.")
    by = {(t["kind"], t["locator"]) for t in toks}
    check(("disasm_fn", "0x825ae690") in by, str(by))
    check(("pdb_type", "animdef_s") in by, str(by))
    check(("map_symbol", "obGetVertexPosHW") in by, str(by))
    check(("shipped_file", "assets/bf/ob_xb_v184/x.rax") in by, str(by))
    check(("measurement", "14,021,613") in by, str(by))
    check(("measurement", "99.51%") in by, str(by))
    return f"{len(by)} typed tokens from one sentence"


def test_hex_offsets_are_not_read_as_decimal():
    """The trap. These blocks write offsets in hex with NO 0x prefix, so reading
    them as decimal puts baseTranslation at 10 where the PDB has it at 16 -- and
    every field past +09 is wrong in a way that looks entirely plausible."""
    fields = migrate.extract_fields(DOC)
    by = {f["name"]: f for f in fields}
    check("endTime" in by, "a field line with a hex letter (+0c) was dropped entirely")
    check(by["endTime"]["offset"] == 0x0C, str(by["endTime"]))
    check(by["baseTranslation"]["offset"] == 0x10,
          f"+10 must be hex 16, got {by['baseTranslation']['offset']}")
    check(all(f["base"] == "hex" for f in fields), [f["base"] for f in fields])
    return "hex base detected from +0c; 8 fields including the one bare hex hid"


def test_pdb_validation_and_conflict():
    conn = fresh()
    if not core.attach_symbols(conn):
        return "skipped: no symbol database"
    good = migrate.validate_fields(conn, migrate.extract_fields(DOC))
    named = [f for f in good if f["pdb_validated"] is not None]
    check(named and all(f["pdb_validated"] == 1 for f in named),
          [f for f in named if f["pdb_validated"] != 1])

    bad = migrate.extract_fields(DOC)
    for f in bad:
        if f["name"] == "baseTranslation":
            f["offset"] = 0x99
            f["base"] = "hex"
    checked = migrate.validate_fields(conn, bad)
    conflict = next(f for f in checked if f["name"] == "baseTranslation")
    check(conflict["pdb_validated"] == 0, str(conflict))
    check("PDB says +16" in conflict["pdb_note"], conflict["pdb_note"])
    return f"{len(named)} fields agree with the retail PDB; a wrong offset conflicts"


def test_ingest_and_mechanical_pass():
    conn = fresh()
    st = _load(conn)
    check(st["blocks"] >= 7, str(st))
    check(st["tokens"] > 0, str(st))

    migrate.mechanical_pass(conn, verbose=False)
    left = core.rows(conn, "SELECT id, text FROM migration_para WHERE accounted=0")
    # Code, tables and uncited prose are retained as passages. What is left is
    # every CITED block -- prose or bullet -- because a cited bullet is an
    # assertion too: section 2.1's devkit-key rule is one.
    check(len(left) == 2, f"expected 2 assertions left, got {len(left)}: "
                          f"{[r['text'][:40] for r in left]}")
    texts = " ".join(r["text"] for r in left)
    check("obGetVertexPosHW" in texts, texts)
    check("0x8263EF70" in texts, "a CITED list item is an assertion, not decoration")
    return "code, tables and uncited prose retained; 2 cited blocks left"


def test_claim_inherits_its_paragraph_citations():
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")
    cid = migrate.claim_from_para(conn, para["id"],
                                  "Positions dequantise by 1/1024.", "verified")
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["n_citations"] >= 2, str(got))
    check(got["effective_status"] == "verified",
          f"a disassembly citation is rule-1 evidence, got {got['effective_status']}")

    # a paragraph with no evidence cannot become a claim at all
    conn2 = fresh()
    _load(conn2, "# T\n\nNarrative with nothing in it.\n")
    p2 = core.one(conn2, "SELECT id FROM migration_para WHERE text LIKE 'Narrative%'")
    try:
        migrate.claim_from_para(conn2, p2["id"], "x")
        raise AssertionError("a citation-free paragraph was allowed to become a claim")
    except BifrostError as e:
        check("no citation token" in str(e), str(e))
    return "citations inherited; an uncited paragraph is refused"


def test_citation_preservation_catches_a_drop():
    """The gate the whole rewrite rests on."""
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")

    cid = migrate.claim_from_para(conn, para["id"], "Positions dequantise by 1/1024.")
    pres = migrate.citation_preservation(conn)
    check(pres["ok"], f"a faithful rewrite must pass: {pres['lost']}")
    check(pres["tokens_kept"] == pres["tokens_checked"], str(pres))

    # now drop one citation, exactly as a careless rewrite would
    sid = core.one(conn, """SELECT s.id FROM citation ct JOIN source s ON s.id=ct.source_id
                            WHERE ct.claim_id=? AND s.kind='disasm_fn'""", (cid,))
    conn.execute("DELETE FROM citation WHERE claim_id=? AND source_id=?", (cid, sid["id"]))
    conn.commit()

    pres = migrate.citation_preservation(conn)
    check(not pres["ok"], "dropping an address must fail the gate")
    lost = pres["lost"][0]
    check(lost["kind"] == "disasm_fn" and lost["locator"] == "0x825ae690", str(lost))
    check(lost["ordinal"] is not None and lost["head"], "the gate must name the paragraph")
    return "a dropped address fails the gate and names its paragraph"


def test_passages_preserve_by_construction():
    conn = fresh()
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    pres = migrate.citation_preservation(conn)
    check(pres["tokens_checked"] > 0, "the code block and table carry tokens")
    check(pres["ok"], str(pres["lost"]))
    return "verbatim retention keeps evidence without auditing it"


def test_render_is_deterministic():
    """A non-deterministic renderer produces a thousand-line diff on every
    regeneration, which makes committing the file pointless."""
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")
    migrate.claim_from_para(conn, para["id"], "Positions dequantise by 1/1024.", "verified")

    a, b = render.render(conn), render.render(conn)
    check(a == b, "render is not deterministic")
    check(render.render_twice_identical(conn), "the helper disagrees with the test")
    for forbidden in ("generated at", "20260", "run id"):
        check(forbidden.lower() not in a.lower(),
              f"the render leaks {forbidden!r}, which churns the diff every run")
    return "byte-identical across runs, no timestamps"


def test_render_keeps_structure_and_marks_status():
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")
    cid = migrate.claim_from_para(conn, para["id"], "Positions dequantise by 1/1024.",
                                  "verified")
    out = render.render(conn)
    check("# Title" in out and "## 1. Method" in out, "headings must survive")
    check("animdef_s  0x1c" in out, "the code block must survive verbatim")
    check("| tree | files |" in out, "the table must survive verbatim")
    check("Positions dequantise by 1/1024." in out, "the claim statement must appear")
    check("obGetVertexPosHW" in out, "citations must be rendered")
    check("[UNBACKED]" not in out, "a disassembly-backed claim must not be marked")

    core.record_refutation(conn, refuted_claim=cid, what_killed_it="the bytes say otherwise",
                           why_plausible="every neighbouring format does it that way")
    out = render.render(conn)
    check("**[REFUTED]**" in out, "a refuted claim must be marked in the margin")
    check("It was plausible because" in out, "why it was plausible must survive")
    return "structure verbatim, status marked, refutation rendered"


def test_render_deduplicates_citations():
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")
    cid = migrate.claim_from_para(conn, para["id"], "Positions dequantise by 1/1024.")
    line = render._citation_line(conn, cid)
    check(line.count("obGetVertexPosHW") == 1,
          f"a function cited by both address and name printed twice: {line}")
    n = core.one(conn, "SELECT COUNT(*) n FROM citation WHERE claim_id=?", (cid,))["n"]
    check(n >= 2, "the underlying citation rows must NOT be deduplicated -- only the render")
    return "rendered once, stored twice"


def test_source_commit_is_recorded_for_recovery():
    """Discarding the original is only safe because git can still produce it."""
    conn = fresh()
    st = _load(conn)
    src = core.one(conn, "SELECT * FROM migration_source")
    check(src["commit_sha"] and len(src["commit_sha"]) >= 7, str(src))
    check("git show" in (src["note"] or ""), f"the recovery command must be recorded: {src}")
    return f"recoverable at {src['commit_sha'][:7]}"



# --- orphaned assertions and promotion -------------------------------------

ORPHAN_DOC = """# Title

## 6.4 Position scale

From `obGetVertexPosHW` (0x825ae690):

```
type 4  : three floats, verbatim
type 13 : three s16, each * 1/1024      __real@3a800000 at 0x821BF9FC
```

There is no per-model scale factor. The capital ships that made a global 1/1024
look impossible simply store float3 positions, and the Venator then reproduces
its stored AABB to the bit.

## 6.5 Something else

Narrative that follows no cited block at all, and so is not an orphan.
"""


def test_orphan_assertions_are_found():
    """The blind spot of paragraph-level extraction.

    The roadmap routinely states a reading in one paragraph and its evidence in
    the one before. Section 6.4 is the real case: two blocks give
    obGetVertexPosHW and its layout, and the third asserts "there is no
    per-model scale factor" carrying no citable token of its own.
    """
    conn = fresh()
    _load(conn, ORPHAN_DOC)
    migrate.mechanical_pass(conn, verbose=False)

    orphans = migrate.orphan_assertions(conn, min_len=40)
    check(len(orphans) == 1, f"expected 1 orphan, got {len(orphans)}: "
                             f"{[o['text'][:40] for o in orphans]}")
    o = orphans[0]
    check("no per-model scale factor" in o["text"], o["text"][:80])
    check(o["anchor"] == "6.4", o["anchor"])
    check(o["inherits_from"] is not None, "an orphan must name the block it follows")
    return "an assertion whose evidence is in the neighbour is reported, not lost"


def test_promote_passage_inherits_the_neighbour():
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn, ORPHAN_DOC)
    migrate.mechanical_pass(conn, verbose=False)
    o = migrate.orphan_assertions(conn, min_len=40)[0]

    before = core.one(conn, "SELECT COUNT(*) n FROM passage WHERE para_id=?", (o["id"],))
    check(before["n"] == 1, "it starts life as a retained passage")

    cid = migrate.promote_passage(
        conn, o["id"], "There is no per-model scale factor.", "verified")
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["n_citations"] >= 1, str(got))
    check(got["effective_status"] == "verified",
          f"the inherited disassembly citation backs it, got {got['effective_status']}")

    cites = {r["locator"] for r in core.rows(conn, """
        SELECT s.locator FROM citation ct JOIN source s ON s.id=ct.source_id
        WHERE ct.claim_id=?""", (cid,))}
    check("0x825ae690" in cites, f"must inherit the neighbour's address: {cites}")

    after = core.one(conn, "SELECT COUNT(*) n FROM passage WHERE para_id=?", (o["id"],))
    check(after["n"] == 0, "the passage is retired -- the paragraph is a claim now")
    check(not migrate.orphan_assertions(conn, min_len=40), "it should no longer be an orphan")
    return "citations inherited from the block it continues; passage retired"


def test_promote_refuses_without_a_cited_neighbour():
    conn = fresh()
    doc = """# T

## 1. A

Nothing cited anywhere in this section at all.
"""
    _load(conn, doc)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE text LIKE 'Nothing cited%'")
    try:
        migrate.promote_passage(conn, para["id"], "some statement")
        raise AssertionError("promotion with nothing to inherit was allowed")
    except BifrostError as e:
        check("no cited neighbour" in str(e), str(e))
    return "refuses rather than inventing evidence"


def test_coverage_survives_promotion():
    """Promotion must not silently drop a paragraph out of the accounting."""
    conn = fresh()
    core.attach_symbols(conn)
    _load(conn, ORPHAN_DOC)
    migrate.mechanical_pass(conn, verbose=False)
    para = core.one(conn, "SELECT id FROM migration_para WHERE accounted=0")
    if para:
        migrate.claim_from_para(conn, para["id"], "positions dequantise by 1/1024")

    o = migrate.orphan_assertions(conn, min_len=40)[0]
    migrate.promote_passage(conn, o["id"], "There is no per-model scale factor.")

    cov = core.one(conn, "SELECT * FROM v_migration_coverage")
    check(cov["unaccounted"] == 0, f"coverage broke: {cov}")
    check(migrate.citation_preservation(conn)["ok"], "preservation broke after promotion")
    return "100% coverage and preservation both hold"


# --- the linking modules ---------------------------------------------------

def test_invariant_patterns_all_match():
    """A pattern that matches no claim is a silently missing link, not a
    harmless one, so the module reports them and this pins that it reports none."""
    from bifrost import invariants
    conn = core.connect()
    stats = invariants.run(conn, verbose=False)
    check(not stats["unmatched"],
          f"{len(stats['unmatched'])} invariant patterns match no claim: "
          f"{stats['unmatched'][:5]}")
    return f"{stats['invariants']} invariants, every pattern matched"


def test_linking_is_idempotent():
    from bifrost import reattach, invariants, discriminators
    conn = core.connect()
    for mod in (reattach, invariants, discriminators):
        first = mod.run(conn, verbose=False)
        second = mod.run(conn, verbose=False)
        changed = {k: v for k, v in second.items()
                   if isinstance(v, int) and v and k not in ("invariants", "skipped")}
        check(not changed, f"{mod.__name__} is not idempotent: {changed}")
    return "reattach, invariants and discriminators all converge"



# --- the committable knowledge dump ----------------------------------------

def test_dump_is_deterministic_and_round_trips():
    """The database is gitignored, so the hand-written layer would otherwise live
    in exactly one file on one machine. A backup nobody has restored is not a
    backup, so this restores it and compares the DERIVED views, not just counts."""
    import tempfile
    from bifrost import dump as dump_mod

    conn = fresh()
    core.attach_symbols(conn)
    _load(conn, ORPHAN_DOC)
    migrate.mechanical_pass(conn, verbose=False)
    o = migrate.orphan_assertions(conn, min_len=40)[0]
    cid = migrate.promote_passage(conn, o["id"], "There is no per-model scale factor.",
                                  "verified")
    core.record_refutation(conn, refuted_claim=cid, what_killed_it="x", why_plausible="y")

    d = Path(tempfile.mkdtemp())
    dump_mod.dump(conn, d, verbose=False)
    first = (d / "claim.jsonl").read_bytes()
    dump_mod.dump(conn, d, verbose=False)
    check(first == (d / "claim.jsonl").read_bytes(),
          "the dump is not byte-stable, so its diff is meaningless")
    check(dump_mod.verify(conn, d), "verify() disagrees with the test")

    # a dump must carry no absolute paths -- it is committed and shared
    text = (d / "claim.jsonl").read_text(encoding="utf-8")
    check("E:/" not in text and "E:\\" not in text,
          "the dump leaks an absolute path, which will not survive another machine")

    target = fresh()
    core.attach_symbols(target)
    dump_mod.restore(target, d, verbose=False)
    for table, _ in dump_mod.TABLES:
        a = core.one(conn, f"SELECT COUNT(*) n FROM {table}")["n"]
        b = core.one(target, f"SELECT COUNT(*) n FROM {table}")["n"]
        check(a == b, f"{table}: {a} dumped, {b} restored")

    q = "SELECT effective_status s, COUNT(*) n FROM v_claim_status GROUP BY s"
    ra = {r["s"]: r["n"] for r in core.rows(conn, q)}
    rb = {r["s"]: r["n"] for r in core.rows(target, q)}
    check(ra == rb, f"derived status diverged after restore: {ra} vs {rb}")
    return "byte-stable, no absolute paths, derived views identical after restore"


def test_dump_covers_the_hand_written_tables():
    """A table that holds judgement and is not in the dump is silently
    unbacked-up, which is the failure this exists to prevent."""
    from bifrost import dump as dump_mod
    dumped = {t for t, _ in dump_mod.TABLES}
    for must in ("claim", "citation", "source", "discriminator", "refutation",
                 "tautology", "passage", "gate_invariant", "format_field",
                 "migration_source", "migration_para"):
        check(must in dumped, f"{must} holds hand-written work and is not dumped")
    # and the derived / scanned tables must NOT be, or the dump stops being a
    # knowledge layer and becomes a stale copy of the whole database
    for must_not in ("tree", "file", "gate_run", "git_state", "wii_symbol", "build"):
        check(must_not not in dumped,
              f"{must_not} rebuilds from the profile and should not be dumped")
    return f"{len(dumped)} tables dumped, scanned and derived ones excluded"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = skipped = 0
    print("=" * 60)
    print("   Bifrost — migration and rendering")
    print("=" * 60)
    for fn in TESTS:
        print(f"[RUNNING] {fn.__name__}...")
        try:
            note = fn()
            if isinstance(note, str) and note.startswith("skipped"):
                print(f"  -> SKIPPED ({note[9:].strip()})")
                skipped += 1
            else:
                print(f"  -> PASSED{'  ' + note if note else ''}")
                passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  -> FAILED: {type(e).__name__}: {e}")
            failed += 1
    print("=" * 60)
    print(f"   TEST RESULTS: {passed} / {passed + failed} passed"
          + (f", {skipped} skipped" if skipped else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
