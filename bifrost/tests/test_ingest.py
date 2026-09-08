"""Bifrost ingest tests.

The gate-stdout fixtures below are verbatim tails of real probe runs made on
4 September 2026. That matters: a parser tested against output somebody invented
is a parser tested against nothing, and these probes do not share a format.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, digest, ingest  # noqa: E402
from bifrost.core import BifrostError  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def fresh():
    conn = core.connect(":memory:")
    core.migrate(conn)
    ingest.ingest_builds(conn)
    ingest.register_gates(conn)
    return conn


# --- verbatim tails of real runs, 4 Sep 2026 -------------------------------

REAL = {
    "corpus_mesh": """\
FAIL  E:/BF3_360/assets/bf/ob_xb_v184\\characters\\beasts\\krayt\\krayt\\ob.rax model 0: part 'BTOP' decodes outside its AABB

1663 files, 3337 models: 3293 pass, 1 fail, 43 refused
14021613 vertices, 9777373 triangles
2253 skinned parts (2071 compressed), 881098 skinned vertices
  refused (1): model carries no geometry
  refused (42): ob has no vertex stream templates""",

    "corpus_wii_ob": """\
2624 ob.war, 2945 models: 2904 pass, 4 fail, 197 refused
  12976 parts, 14250777 vertices, 9967623 triangles, 522 models with vertex colour
  4372 skinned parts, 2559872 skinned vertices, 696 models binding matrices
  failures: 0 bone past the skeleton, 4 position outside its AABB, 0 index out of range, 0 non-unit normal
  37  refused: model carries no geometry
  160  refused: no rdata.war beside the ob.war""",

    "corpus_anim": """\
4705 files: 4703 pass, 0 fail, 2 refused
  157460 joints, 6092979 keys (2506470 with a quantised time, the rest f32), 5444091 quaternions
  5286631 key-to-key steps, mean 2.95096 deg, worst 180 deg, 26198 over 45 deg
  2  refused: narrow channel offsets cannot address this file""",

    "corpus_pose": """\
1663 ob.rax, 3337 models: 574 pass, 1 fail, 2719 with no skeleton, 43 refused
  575 skeletons, 34140 bones (575 roots); 471 carry an inverse bind (obdef_s.flags bit 2), 104 are hierarchy only
  failures: 0 hierarchy out of order, 0 palette not identity, 1 vertex moved
  failed: E:/BF3_360/assets/bf/ob_xb_v184\\characters\\beasts\\krayt\\krayt\\ob.rax [0] (bind skinning moved a vertex)""",

    "corpus_terrain": """\
19 files, 19 pass, 0 fail
65 tiles, 9862209 samples
178002 samples covered by two tiles, 2439 disagree
0 heights outside [hOffset, hOffset + hScale]
159 texture ids, 159 with a file on disk""",

    "corpus_vm": """\
[SUCCESS] VM context 0xc4360afd: 655 functions, 377 constants, 44 types
E:/BF3_R9_Wii/DATA/files/assets/bf/omv_v10: 596 scripts, 596 walk, 0 fail, 1 libraries
  168525 instructions, 18477 fixed and 2403 variadic native calls
  stepped with a stub bound to every function: 190069222 instructions, 501 ran to the end, 0 faulted""",

    "corpus_matattr": """\
    diff    (old slot 0)   858618  858881     263       0     263
    lmap    (old slot 6)   833154  823521    2514   12147    1296

[PASS] 868129 descriptors: 0 naming an unknown set, 0 with a texture id past their set's slot count""",
}

# corpus_level prints its summary ABOVE a ~50 line class-id histogram, which is
# exactly the case a tail-only parser gets wrong.
REAL["corpus_level"] = (
    "setups          : 213, 0 failed to parse\n"
    "declarations    : 24759 -> 24263 props, 339 backgrounds, 157 no template\n"
    "models          : 8112 named, 352 distinct, 7948 on disk, 164 missing\n"
    "lods            : 1907 promised by numLods, 1907 on disk\n"
    "transforms      : 14838 carry a rot, 0 not orthonormal\n"
    "-- class-id histogram --\n"
    + "\n".join(f"  {n}  some class id {n}" for n in range(60))
    + "\nPASS"
)


def test_parse_real_gate_output():
    expect = {
        "corpus_mesh":     (3293, 1, 43, 0),   # 1 fail -> raw verdict is not ok
        "corpus_wii_ob":   (2904, 4, 197, 0),
        "corpus_anim":     (4703, 0, 2, 1),
        "corpus_pose":     (574, 1, 43, 0),    # the same krayt outlier
        "corpus_terrain":  (19, 0, None, 1),
        "corpus_vm":       (596, 0, None, 1),
        "corpus_matattr":  (None, None, None, 1),
        "corpus_level":    (None, 0, None, 1),
    }
    for name, (p, f, r, ok) in expect.items():
        got = ingest.parse_gate_stdout(name, REAL[name])
        check(got["pass"] == p, f"{name}: pass {got['pass']} != {p}")
        check(got["fail"] == f, f"{name}: fail {got['fail']} != {f}")
        check(got["refused"] == r, f"{name}: refused {got['refused']} != {r}")
        check(got["ok"] == ok, f"{name}: ok {got['ok']} != {ok}")
    return f"{len(expect)} real probe outputs, none sharing a format"


def test_failing_gate_is_not_ok():
    got = ingest.parse_gate_stdout(
        "corpus_mesh", "1663 files, 3337 models: 3200 pass, 94 fail, 43 refused")
    check(got["ok"] == 0, "a nonzero fail count must not report ok")


def test_unparseable_output_is_not_a_pass():
    """The failure mode that matters. Reporting green for output nobody
    understood is precisely what rule 7 forbids."""
    got = ingest.parse_gate_stdout("corpus_mesh", "Segmentation fault\nAborted\n")
    check(got["ok"] == 0, "unparseable output must not report ok")
    check("parse_note" in got["metrics"], "and it must say why")

    got = ingest.parse_gate_stdout("corpus_mesh", "")
    check(got["ok"] == 0, "empty output must not report ok")


def test_no_metrics_are_inferred_from_free_text():
    """This test used to assert the opposite, and that was the defect.

    Scraping "<number> <nearby word>" out of prose produced `and_all_eight: 90`
    and `confirmed_by_direct_stat: 360` (the 360 came from the path E:/BF3_360),
    stored as if they were measurements. Worst of all, a paragraph written to
    explain that an earlier run was a mis-parse was itself scraped, landing as
    `is_a_recording_artefact: 32`.
    """
    got = ingest.parse_gate_stdout("corpus_mesh", REAL["corpus_mesh"])
    check(got["metrics"] == {}, f"free text must yield no metrics, got {got['metrics']}")
    check(got["pass"] == 3293, "counts still come from the summary line")

    prose = ("corpus_wii_anim: bimodal by joint, 17063 by_joint and 1177 deg;\n"
             "and all eight land 97 degrees apart.\n"
             "4694 pass, 0 fail\n")
    got = ingest.parse_gate_stdout("corpus_wii_anim", prose)
    check(got["metrics"] == {}, f"prose is not data, got {got['metrics']}")
    check((got["pass"], got["fail"]) == (4694, 0), str(got))
    return "prose stays prose; counts still parse"


def test_declared_block_is_the_only_source_of_metrics():
    block = ('BIFROST-RESULT-BEGIN\n'
             '{"pass": 1387, "fail": 0, "metrics": {"files": 1387, "decoded": 1387}}\n'
             'BIFROST-RESULT-END\n')
    got = ingest.parse_gate_stdout("corpus_res", "noise 99 things\n" + block)
    check(got["declared"] is True, "a block must be recognised as declared")
    check((got["pass"], got["fail"], got["ok"]) == (1387, 0, 1), str(got))
    check(got["metrics"] == {"files": 1387, "decoded": 1387}, str(got["metrics"]))

    kv = ("BIFROST-RESULT-BEGIN\n"
          "pass=1387\nfail=0\nfiles=1387\n"
          "BIFROST-RESULT-END\n")
    got = ingest.parse_gate_stdout("corpus_res", kv)
    check((got["pass"], got["fail"]) == (1387, 0), str(got))
    check(got["metrics"] == {"files": 1387}, str(got["metrics"]))
    return "JSON and KEY=value both declare; nothing else does"


def test_a_malformed_block_refuses_rather_than_falling_back():
    """The dangerous case: output somebody believed was structured."""
    for body, why in (
        ('{"pass": 1, "fail":}', "invalid JSON"),
        ('{"pass": "many", "fail": 0}', "a non-integer count"),
        ('the run went fine', "neither JSON nor KEY=value"),
        ('{"metrics": {"files": 3}}', "no pass and no fail"),
    ):
        text = f"BIFROST-RESULT-BEGIN\n{body}\nBIFROST-RESULT-END\n0 fail\n12 pass\n"
        try:
            ingest.parse_gate_stdout("corpus_res", text)
            raise AssertionError(f"{why} was accepted, and would fall back to guessing")
        except BifrostError:
            pass

    two = ("BIFROST-RESULT-BEGIN\npass=1\nfail=0\nBIFROST-RESULT-END\n"
           "BIFROST-RESULT-BEGIN\npass=2\nfail=0\nBIFROST-RESULT-END\n")
    try:
        ingest.parse_gate_stdout("corpus_res", two)
        raise AssertionError("two blocks in one log was accepted")
    except BifrostError:
        pass
    return "a broken declaration is refused, never re-read by the guesser"


def test_corpus_res_summary_is_not_read_backwards():
    """`PARSED OK: 1387   FAILED: 0` bound 1387 to fail and flipped the gate red.

    A cold session reading the digest then saw a failing gate that had passed.
    """
    got = ingest.parse_gate_stdout("corpus_res", "Scanning...\nPARSED OK: 1387   FAILED: 0\n")
    check(got["fail"] == 0, f"fail should be 0, got {got['fail']}")
    check(got["ok"] == 1, "a clean run must not report red")

    # ...while a colon that introduces a BREAKDOWN must not beat the real total.
    got = ingest.parse_gate_stdout("corpus_wii_ob", REAL["corpus_wii_ob"])
    check(got["fail"] == 4, f"the summary line's 4 wins over 'failures: 0', got {got['fail']}")
    return "adjacency no longer decides which number is the failure count"


def test_ingest_run_records_staleness_inputs():
    conn = fresh()
    rid = ingest.ingest_gate_run(conn, "corpus_mesh", REAL["corpus_mesh"],
                                 commit_sha="abc123", tree_dirty=False)
    row = core.one(conn, "SELECT * FROM gate_run WHERE id=?", (rid,))
    check(row["pass"] == 3293 and row["fail"] == 1 and row["refused"] == 43, str(row))
    check(row["commit_sha"] == "abc123", str(row))
    check(row["stdout_head"] and "refused (42)" in row["stdout_head"],
          "the head must keep the tail of the output, which is where summaries live")
    # Only the two the database COMPUTES. Nothing is scraped out of the text:
    # a probe declares its metrics in a BIFROST-RESULT block or has none.
    check(set(json.loads(row["metrics"])) == {"accepted_exceptions", "unexplained_failures"},
          row["metrics"])

    # unknown gate is refused rather than silently created
    try:
        ingest.ingest_gate_run(conn, "corpus_nonexistent", "x")
        raise AssertionError("an unknown gate was accepted")
    except BifrostError as e:
        check("unknown gate" in str(e), str(e))


def test_supersede_retracts_a_misparse_without_unsaying_it():
    """Append-only is right for evidence, wrong for a transcription error.

    Run 32 of the BF3 database was `PARSED OK: 1387  FAILED: 0` read backwards.
    It could not be retracted, so the correction became run 33 and the digest
    listed both -- a failing gate in front of every session that read it cold.
    """
    conn = fresh()
    bad = ingest.ingest_gate_run(conn, "corpus_mesh", "3200 pass, 94 fail",
                                 commit_sha="a", tree_dirty=False)
    check(core.one(conn, "SELECT ok FROM v_gate_latest WHERE name='corpus_mesh'")["ok"] == 0,
          "the mis-parse is this gate's state until something supersedes it")

    good = ingest.ingest_gate_run(conn, "corpus_mesh", "3294 pass, 0 fail",
                                  commit_sha="a", tree_dirty=False, supersedes=bad,
                                  note="run was PARSED OK read backwards, not a result")
    latest = core.one(conn, "SELECT * FROM v_gate_latest WHERE name='corpus_mesh'")
    check(latest["run_id"] == good and latest["ok"] == 1, str(latest))
    check("read backwards" in (latest["note"] or ""), str(latest))

    # The bad row is still there. Nothing was unsaid, only reclassified.
    check(core.one(conn, "SELECT * FROM gate_run WHERE id=?", (bad,)) is not None,
          "a superseded run must survive; it is the record of what the parser did")
    check(digest.digest_data(conn)["recent"] == [] or
          all(r["ts"] for r in digest.digest_data(conn)["recent"]), "digest still renders")
    names = [(r["name"], r["ok"]) for r in digest.digest_data(conn)["recent"]]
    check(names == [("corpus_mesh", 1)], f"the digest lists one run, not both: {names}")

    for wrong, why in ((bad, "superseding an already-superseded run"),
                       (9999, "superseding a run that does not exist")):
        try:
            ingest.ingest_gate_run(conn, "corpus_mesh", "1 pass, 0 fail", supersedes=wrong,
                                   commit_sha="a", tree_dirty=False)
            raise AssertionError(f"{why} was accepted")
        except BifrostError:
            pass

    # ...and never across gates: a run can only supersede one of its own.
    try:
        ingest.ingest_gate_run(conn, "corpus_anim", "1 pass, 0 fail", supersedes=good,
                               commit_sha="a", tree_dirty=False)
        raise AssertionError("a run superseded another gate's run")
    except BifrostError:
        pass
    return "the row stays, the views stop counting it"


def _tiny_repo(files: dict) -> Path:
    """A throwaway git repo, because the scanner reads `git ls-files`."""
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    run = lambda *a: subprocess.run(a, cwd=str(d), capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    run("git", "add", "-A"); run("git", "commit", "-qm", "in")
    return d


def test_code_comments_are_indexed_with_their_citations():
    """The evidence written in the code was not in the knowledge layer.

    Over the BF3 tree, 235 retail addresses are cited in source comments and 107
    of them appeared nowhere else -- so an agent resolving one was told nothing
    was known while a header already explained it.
    """
    from bifrost import comments as cm
    conn = fresh()
    d = _tiny_repo({
        "src/a.cpp": (
            "// obGetPart, 0x825b1678. The part array is ob+0x18 and the count\n"
            "// sits at ob+0x14, which obGetNumParts (0x825b1638) bounds against.\n"
            "int f() { return 1; }\n"
            "// an ordinary comment carrying no provenance at all\n"),
        "tools/b.py": "# m0vTick at 0x8266b628 drives one frame\nx = 1\n",
        "README.md": "# not a source file\n",
    })
    try:
        st = cm.scan(conn, root=d)
        check(st["blocks"] == 2, f"two citing blocks, got {st['blocks']}: {st}")
        check(st["files"] == 2, f"only the source files are read, got {st['files']}")

        rows = core.rows(conn, "SELECT * FROM v_code_comment ORDER BY path")
        check(len(rows) == 2, str(rows))
        check(rows[0]["path"] == "src/a.cpp" and rows[0]["n_citations"] == 2, str(rows[0]))
        check(all(r["stale"] == 0 for r in rows), "a fresh scan is not stale")

        # The prose with no provenance must NOT be indexed: burying the rows that
        # matter under the ones that do not is how an index stops being read.
        check("ordinary comment" not in rows[0]["prose"], rows[0]["prose"])

        # The payoff: the question "has anyone worked this out already".
        hits = cm.explaining(conn, "0x825b1678")
        check(len(hits) == 1 and "part array" in hits[0]["prose"], str(hits))
        check(cm.explaining(conn, "0x8266b628"), "python comments count too")
        check(cm.explaining(conn, "0xdeadbeef") == [], "and nothing is invented")

        # Re-scanning is idempotent, and an edited block updates in place.
        st2 = cm.scan(conn, root=d)
        check((st2["new"], st2["updated"], st2["removed"]) == (0, 0, 0), str(st2))
        (d / "src/a.cpp").write_text(
            "// obGetPart, 0x825b1678. Rewritten, same address.\nint f(){return 1;}\n",
            encoding="utf-8")
        st3 = cm.scan(conn, root=d)
        check(st3["updated"] == 1, f"an edited block updates in place: {st3}")
        check(core.one(conn, "SELECT COUNT(*) n FROM code_comment WHERE path='src/a.cpp'")["n"] == 1,
              "and does not duplicate")
        check(cm.explaining(conn, "0x825b1638") == [],
              "an address the rewrite dropped must stop being reachable")
        return "248 blocks over the real tree; here, the whole lifecycle"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_comment_is_not_evidence_for_itself():
    """source_comment is a citable kind, but it must not BACK a claim.

    Citing our own note as proof of our own note is the circularity the backing
    rules exist to stop -- it behaves like `measurement`, not like `disasm_fn`.
    """
    conn = fresh()
    cid = core.record_claim(
        conn, subject_type="format", subject_id=None,
        statement="the part array is at ob+0x18", asserted_status="verified",
        citations=[{"kind": "source_comment",
                    "locator": "src/SelotapeDataLoaders.cpp:344"}])
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "asserted-unbacked",
          f"a comment must not back its own claim, got {got['effective_status']}")
    return "citable, and correctly not evidence for itself"


def test_a_run_can_pass_and_still_have_found_something():
    """`ok` is binary; the interesting state is a third thing.

    corpus_wii_anim reads `ok 4694 pass 0 fail` while 16.6% of its re-encoded
    rotation channels disagree with the same animation in the 360 tree. The gate
    passed on its own terms and found something anyway, and the digest -- the
    one surface a cold session is guaranteed to read -- showed only the pass.
    """
    conn = fresh()
    tid = core.propose(conn, "todo", {"title": "Wii animation: the disagreeing channels"})
    core.review(conn, "todo", tid, "confirmed")

    rid = ingest.ingest_gate_run(
        conn, "corpus_wii_anim", "4694 pass, 0 fail", commit_sha="a", tree_dirty=False,
        finding="16.6% of re-encoded rotation channels disagree with the 360 tree",
        todo_id=tid)
    row = core.one(conn, "SELECT * FROM gate_run WHERE id=?", (rid,))
    check(row["ok"] == 1, "it passed structurally")
    check("16.6%" in row["finding"], str(row["finding"]))
    check(row["todo_id"] == tid, "and it is attached to the work it bears on")

    # The point of the column: the digest must not read as an unqualified pass.
    text = digest.render(conn)
    check("ok?" in text, "a qualified pass must be marked in the digest")
    check("16.6%" in text, "and the finding itself must be on the digest")

    try:
        ingest.ingest_gate_run(conn, "corpus_wii_anim", "1 pass, 0 fail", todo_id=9999,
                               commit_sha="a", tree_dirty=False)
        raise AssertionError("a run attached to a todo that does not exist was accepted")
    except BifrostError:
        pass
    return "passed, and found something -- visible where people actually look"


def test_note_keeps_interpretation_out_of_stdout():
    conn = fresh()
    rid = ingest.ingest_gate_run(
        conn, "corpus_anim", REAL["corpus_anim"], commit_sha="a", tree_dirty=False,
        note="bimodal by joint; the second mode is all eight of the cloth rigs")
    row = core.one(conn, "SELECT * FROM gate_run WHERE id=?", (rid,))
    check("bimodal" in row["note"], str(row["note"]))
    check("bimodal" not in (row["stdout_head"] or ""),
          "stdout stays the probe's own bytes; the conclusion is a person's")
    return "measured and concluded are separable after the fact"


def test_stdout_goes_to_disk():
    conn = fresh()
    d = Path(tempfile.mkdtemp())
    try:
        rid = ingest.ingest_gate_run(conn, "corpus_anim", REAL["corpus_anim"],
                                     commit_sha="c", tree_dirty=False, stdout_dir=d)
        row = core.one(conn, "SELECT stdout_path, stdout_head FROM gate_run WHERE id=?", (rid,))
        check(row["stdout_path"] and Path(row["stdout_path"]).exists(), str(row))
        check(Path(row["stdout_path"]).read_text(encoding="utf-8") == REAL["corpus_anim"],
              "the full output must survive verbatim on disk")
        check(len(row["stdout_head"]) < len(REAL["corpus_anim"]) + 1,
              "the in-row head stays small")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_tree_scan():
    conn = fresh()
    root = Path(tempfile.mkdtemp())
    try:
        tree = root / "assets" / "bf" / "ob_xb_v184" / "characters" / "clone"
        tree.mkdir(parents=True)
        (tree / "ob.rax").write_bytes(b"x" * 100)
        (tree / "havok.rax").write_bytes(b"y" * 50)
        (root / "assets" / "bf" / "ob_xb_v184" / "tex.tpf").write_bytes(b"z" * 7)
        (root / "assets" / "bf" / "cloud_xb_v1").mkdir()
        (root / "assets" / "bf" / "cloud_xb_v1" / "noise3d.cld").write_bytes(b"c" * 9)

        conn.execute("UPDATE build SET root_path=? WHERE name='bf3_360'", (str(root),))
        conn.commit()
        got = ingest.scan_trees(conn, "bf3_360")
        by = {t["name"]: t for t in got}

        check(by["ob_xb_v184"]["files"] == 3, str(by["ob_xb_v184"]))
        check(by["ob_xb_v184"]["bytes"] == 157, str(by["ob_xb_v184"]))
        check(by["ob_xb_v184"]["version"] == 184, "version comes off the directory name")
        check(by["cloud_xb_v1"]["version"] == 1, str(by["cloud_xb_v1"]))

        row = core.one(conn, "SELECT * FROM tree WHERE name='ob_xb_v184'")
        hist = json.loads(row["ext_histogram"])
        check(hist == {".rax": 2, ".tpf": 1}, str(hist))

        # rescanning updates in place rather than duplicating
        (tree / "novodex.rax").write_bytes(b"w" * 10)
        ingest.scan_trees(conn, "bf3_360")
        n = conn.execute("SELECT COUNT(*) FROM tree WHERE name='ob_xb_v184'").fetchone()[0]
        check(n == 1, f"rescan must update in place, found {n} rows")
        row = core.one(conn, "SELECT file_count FROM tree WHERE name='ob_xb_v184'")
        check(row["file_count"] == 4, str(row))
        return "scan, histogram, version and idempotent rescan"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wii_symbols_and_backfill():
    conn = fresh()
    wm = ingest.wii_map()
    if not wm or not wm.exists():
        return f"skipped: no Wii map at {wm}"

    # a citation recorded BEFORE the map is loaded resolves to nothing...
    sid = core.ensure_source(conn, "disasm_fn", "0x8044E160", "bf3_wii")
    row = core.one(conn, "SELECT symbol FROM source WHERE id=?", (sid,))
    check(row["symbol"] is None, "should be unresolved before the map is ingested")

    n = ingest.ingest_wii_symbols(conn)
    check(n > 3000, f"expected ~3,100 symbols, got {n}")

    # A symbol the map actually carries resolves exactly.
    row = conn.execute("SELECT addr, symbol FROM wii_symbol ORDER BY addr LIMIT 1").fetchone()
    r = core.resolve_address(conn, row["addr"], "bf3_wii")
    check(r.kind == "exact" and r.symbol == row["symbol"], f"{r} for {row['symbol']}")

    # And one it does NOT must stay unresolved rather than being attributed to a
    # distant neighbour. RABAZZ_full.map holds 3,113 symbols over a 5.3 MB text
    # range with gaps up to 0x2d330 (185 KB), so vmExecute at 0x8044E160 -- which
    # was found by disassembly, not by the map -- has its nearest preceding
    # symbol 185 KB away. Attributing it there would be a fabricated citation.
    r = core.resolve_address(conn, 0x8044E160, "bf3_wii")
    check(r.kind == "unresolved",
          f"a 185 KB gap must not resolve; got {r.kind} -> {r.containing}")

    # ...and backfill is what rescues it, since nothing else revisits old rows
    known = core.ensure_source(conn, "disasm_fn", hex(row["addr"]), "bf3_wii")
    conn.execute("UPDATE source SET symbol=NULL WHERE id=?", (known,))
    conn.commit()
    stats = ingest.backfill_source_symbols(conn)
    got = core.one(conn, "SELECT symbol FROM source WHERE id=?", (known,))
    check(got["symbol"] == row["symbol"],
          "backfill must resolve citations recorded before the map existed")
    return (f"{n} Wii symbols; backfill: {stats['exact']} exact, "
            f"{stats['inside']} inside, {stats['unresolved']} unresolved (sparse map)")


def test_gate_registry_matches_probes():
    """Every registered gate names a probe that exists, so the registry cannot
    quietly describe a gate nobody can run."""
    conn = fresh()
    missing = []
    from bifrost import profile
    for g in profile.load().GATES:
        p = core.repo_root() / g["probe_path"]
        if g["probe_path"] and not p.exists():
            missing.append(g["probe_path"])
    check(not missing, f"registered gates with no probe on disk: {missing}")
    n = conn.execute("SELECT COUNT(*) FROM gate").fetchone()[0]
    want = len(profile.load().GATES)
    check(n == want, f"{n} gates registered, expected {want}")
    return f"{n} gates, every probe_path present"



def test_accepted_exception_keeps_a_gate_green():
    """Rule 4 asks for failures JUSTIFIED, not for zero failures.

    corpus_mesh has reported "1 fail" since the krayt outlier was found. That
    file is a named, accepted exception, and a gate that stayed red because of it
    would train everyone to ignore red. An UNEXPLAINED failure still fails.
    """
    conn = fresh()

    rid = ingest.ingest_gate_run(conn, "corpus_mesh", REAL["corpus_mesh"],
                                 commit_sha="c", tree_dirty=False)
    row = core.one(conn, "SELECT ok, metrics FROM gate_run WHERE id=?", (rid,))
    check(row["ok"] == 0, "an unjustified failure must fail the gate")
    check(json.loads(row["metrics"])["unexplained_failures"] == 1, row["metrics"])

    gid = core.get_id(conn, "gate", "corpus_mesh")
    conn.execute("""INSERT INTO exception(id, name, disposition, rationale)
                    VALUES (1,'krayt declared stride contradicts its own data',
                            'reported',
                            '0x25E0 + 32*3720 is exactly the next stream data pointer')""")
    conn.execute("INSERT INTO exception_gate(exception_id, gate_id) VALUES (1,?)", (gid,))
    conn.commit()

    rid = ingest.ingest_gate_run(conn, "corpus_mesh", REAL["corpus_mesh"],
                                 commit_sha="c", tree_dirty=False)
    row = core.one(conn, "SELECT ok, metrics FROM gate_run WHERE id=?", (rid,))
    m = json.loads(row["metrics"])
    check(row["ok"] == 1, "a justified failure must not keep the gate red")
    check(m["accepted_exceptions"] == 1 and m["unexplained_failures"] == 0, str(m))

    # a SECOND, unregistered failure must turn it red again
    worse = REAL["corpus_mesh"].replace("3293 pass, 1 fail", "3292 pass, 2 fail")
    rid = ingest.ingest_gate_run(conn, "corpus_mesh", worse,
                                 commit_sha="c", tree_dirty=False)
    row = core.one(conn, "SELECT ok, metrics FROM gate_run WHERE id=?", (rid,))
    check(row["ok"] == 0, "a new, unexplained failure must fail the gate")
    check(json.loads(row["metrics"])["unexplained_failures"] == 1, row["metrics"])
    return "justified failures stay green, new ones do not"


def test_output_with_no_verdict_is_refused_not_recorded():
    """Rows 11-19 of the BF3 database, and why they must not be possible.

    `git log --oneline -1` was ingested as a run of corpus_matattr: recorded red,
    and mined for the metric `read_the_material_sets: 7592` out of the commit
    subject `3dc7592 Read the material sets, ...`. Neither half is true, and a
    red row costs someone an afternoon looking for the defect.
    """
    conn = fresh()
    junk = {
        "a commit subject": "3dc7592 Read the material sets, the combination rule",
        "an ls -l":         "-rwxr-xr-x 1 jwall 197609 236032 2026-09-04 build/x.exe",
        "a traceback":      "Segmentation fault\nAborted\n",
        "nothing at all":   "",
    }
    for what, text in junk.items():
        parsed = ingest.parse_gate_stdout("corpus_mesh", text)
        check(parsed["recognised"] is False, f"{what}: must not be recognised")
        check(parsed["ok"] == 0, f"{what}: must never report ok")
        try:
            ingest.ingest_gate_run(conn, "corpus_mesh", text, commit_sha="abc")
            raise AssertionError(f"{what}: was recorded, and must not have been")
        except BifrostError:
            pass
    n = conn.execute("SELECT COUNT(*) FROM gate_run").fetchone()[0]
    check(n == 0, f"nothing may reach gate_run, got {n} rows")

    # and no metric may be mined out of text nobody could parse
    got = ingest.parse_gate_stdout("corpus_mesh", junk["a commit subject"])
    check(list(got["metrics"]) == ["parse_note"], str(got["metrics"]))
    return f"{len(junk)} kinds of non-output, all refused, no metrics invented"


def test_a_pass_literal_in_source_is_not_a_verdict():
    """`"[PASS]" in text` matched the probe's own source. A sed of
    corpus_matattr.cpp was recorded as a green run of corpus_matattr."""
    source = (
        '    const bool pass = (chainBreaks == 0) && (all.setUnknown == 0);\n'
        '    std::cout << "\\n" << (pass ? "[PASS]" : "[FAIL]") << " "\n'
        '              << all.descriptors << " descriptors" << std::endl;\n'
    )
    got = ingest.parse_gate_stdout("corpus_matattr", source)
    check(got["ok"] == 0, "a source listing must never report ok")
    check(got["recognised"] is False, "a source listing is not a gate run")

    # while the verdict the probe actually PRINTS, counts and all, still reads
    real = "[PASS] 868129 descriptors: 0 naming an unknown set, 0 with a texture id past"
    check(ingest.parse_gate_stdout("corpus_matattr", real)["ok"] == 1, real)
    check(ingest.parse_gate_stdout("corpus_matattr",
                                   real.replace("[PASS]", "[FAIL]"))["ok"] == 0,
          "and so does the failing branch, which carries counts after the verdict")
    return "the literal in the source is inert; the printed verdict still reads"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = skipped = 0
    print("=" * 60)
    print("   Bifrost — ingest, parsers and scanners")
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
