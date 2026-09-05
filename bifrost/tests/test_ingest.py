"""Bifrost ingest tests.

The gate-stdout fixtures below are verbatim tails of real probe runs made on
4 September 2026. That matters: a parser tested against output somebody invented
is a parser tested against nothing, and these probes do not share a format.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, ingest  # noqa: E402
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


def test_metrics_extracted():
    got = ingest.parse_gate_stdout("corpus_mesh", REAL["corpus_mesh"])
    m = got["metrics"]
    check(m.get("vertices") == 14021613, f"vertices: {m.get('vertices')}")
    check(m.get("triangles") == 9777373, f"triangles: {m.get('triangles')}")
    return "vertices and triangles pulled out of the free text"


def test_ingest_run_records_staleness_inputs():
    conn = fresh()
    rid = ingest.ingest_gate_run(conn, "corpus_mesh", REAL["corpus_mesh"],
                                 commit_sha="abc123", tree_dirty=False)
    row = core.one(conn, "SELECT * FROM gate_run WHERE id=?", (rid,))
    check(row["pass"] == 3293 and row["fail"] == 1 and row["refused"] == 43, str(row))
    check(row["commit_sha"] == "abc123", str(row))
    check(row["stdout_head"] and "refused (42)" in row["stdout_head"],
          "the head must keep the tail of the output, which is where summaries live")
    check(json.loads(row["metrics"]).get("vertices") == 14021613, row["metrics"])

    # unknown gate is refused rather than silently created
    try:
        ingest.ingest_gate_run(conn, "corpus_nonexistent", "x")
        raise AssertionError("an unknown gate was accepted")
    except BifrostError as e:
        check("unknown gate" in str(e), str(e))


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
