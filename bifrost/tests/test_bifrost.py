"""Bifrost test harness.

Same shape as the C++ suites in tests/ -- a named runner per concern, one line
per result, a count at the end -- so `bifrost test` reads like the rest of the
project's gates.

Everything below runs against a temporary database. The three tests that need the
retail symbol database skip themselves when it is absent, and say so, rather than
passing vacuously.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, digest  # noqa: E402
from bifrost.core import BifrostError  # noqa: E402

RESULTS = []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def fresh():
    conn = core.connect(":memory:")
    core.migrate(conn)
    return conn


def seed_claim(conn, statement="a claim", status="plausible", subject_id=None):
    return core.record_claim(
        conn, subject_type="format", subject_id=subject_id, statement=statement,
        asserted_status=status,
        citations=[{"kind": "disasm_fn", "locator": "0x825ae690"}],
    )


# ---------------------------------------------------------------------------

def test_migrations_apply():
    conn = core.connect(":memory:")
    applied = core.migrate(conn)
    # Derived, not hardcoded: adding a migration must not break this test.
    expected = [v for v, _, _ in core.migration_files()]
    check(applied == expected, f"expected {expected}, got {applied}")

    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("build", "tree", "format", "format_field", "claim", "citation",
              "discriminator", "refutation", "tautology", "passage", "gate",
              "gate_run", "capability", "todo", "edge", "git_state",
              "migration_para", "wii_symbol"):
        check(t in names, f"missing table {t}")

    views = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}
    for v in ("v_gate_latest", "v_gate_stale", "v_claim_status", "v_claim_risk",
              "v_format_status", "v_capability", "v_blocked", "v_review_queue",
              "v_constant_unsourced", "v_field_pdb_conflict",
              "v_migration_coverage", "v_claim_measurement_only", "v_format_ready"):
        check(v in views, f"missing view {v}")

    # every view must actually execute, not merely parse
    for v in sorted(views):
        conn.execute(f"SELECT * FROM {v} LIMIT 1").fetchall()


def test_migrations_idempotent():
    path = Path(tempfile.mkdtemp()) / "b.db"
    try:
        expected = [v for v, _, _ in core.migration_files()]
        c1 = core.connect(path); first = core.migrate(c1); c1.close()
        c2 = core.connect(path); second = core.migrate(c2)
        check(first == expected, f"first run {first}")
        check(second == [], f"second run should be a no-op, got {second}")
        n = c2.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        check(n == len(expected), f"schema_version should hold {len(expected)} rows, holds {n}")
        c2.close()
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def test_claim_requires_citation():
    conn = fresh()
    try:
        core.record_claim(conn, subject_type="format", subject_id=None,
                          statement="positions are three s16 at 1/1024", citations=[])
        raise AssertionError("rule 6 not enforced: a claim with no citation was accepted")
    except BifrostError as e:
        check("citation" in str(e), f"wrong error: {e}")

    try:
        core.record_claim(conn, subject_type="format", subject_id=None,
                          statement="   ", citations=[{"kind": "measurement", "locator": "x"}])
        raise AssertionError("empty statement was accepted")
    except BifrostError:
        pass


def test_source_kind_validated():
    conn = fresh()
    try:
        core.ensure_source(conn, "vibes", "somewhere")
        raise AssertionError("an unknown source kind was accepted")
    except BifrostError as e:
        check("unknown source kind" in str(e), str(e))


def test_claim_roundtrip():
    conn = fresh()
    cid = core.record_claim(
        conn, subject_type="format", subject_id=None,
        statement="obGetVertexPosHW dequantises type 13 positions by 1/1024",
        citations=[
            {"kind": "disasm_fn", "locator": "0x825ae690", "build": "bf3_360"},
            {"kind": "data_addr", "locator": "0x821BF9FC",
             "note": "__real@3a800000, the 1/1024 constant"},
        ],
    )
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["n_citations"] == 2, f"expected 2 citations, got {got['n_citations']}")
    check(got["effective_status"] == "plausible", got["effective_status"])

    # source rows are deduplicated, not duplicated per claim
    cid2 = core.record_claim(conn, subject_type="format", subject_id=None,
                             statement="another claim on the same function",
                             citations=[{"kind": "disasm_fn", "locator": "0x825ae690",
                                         "build": "bf3_360"}])
    n = conn.execute("SELECT COUNT(*) FROM source WHERE locator='0x825ae690'").fetchone()[0]
    check(n == 1, f"source should be deduplicated, found {n} rows")


def measured_claim(conn, statement, status="plausible"):
    """A claim whose only evidence is a MEASUREMENT.

    Since migration 006, citing the retail code, the PDB, a build manifest or a
    shipped artefact is itself sufficient backing -- that is AGENTS.md rule 1,
    read the code that reads it. What stays unbacked is a bare statistic with no
    artefact and no code behind it, which is the shape rule 1 exists to prevent.
    """
    return core.record_claim(
        conn, subject_type="format", subject_id=None, statement=statement,
        asserted_status=status,
        citations=[{"kind": "measurement", "locator": "26,991 key-to-key steps"}])


def test_reading_the_code_is_itself_backing():
    """Rule 1 evidence stands on its own; it does not need a corpus gate too."""
    conn = fresh()
    cid = seed_claim(conn, "positions dequantise by 1/1024", status="verified")
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["n_code_read"] == 1, str(got))
    check(got["effective_status"] == "verified",
          f"a disassembly citation backs a claim, got {got['effective_status']}")
    risk = core.one(conn, "SELECT * FROM v_claim_risk WHERE id=?", (cid,))
    check(risk and risk["severity"] == "low",
          f"missing corpus coverage is rule 4 exposure, low severity: {risk}")
    return "a disassembly citation backs it; only the missing gate is flagged"


def test_verified_requires_backing():
    """The core promise: an agent cannot declare something verified on a bare
    measurement."""
    conn = fresh()
    cid = measured_claim(conn, "the permutation table is the natural one", status="verified")

    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "asserted-unbacked",
          f"unbacked 'verified' should downgrade, got {got['effective_status']}")

    risk = core.one(conn, "SELECT * FROM v_claim_risk WHERE id=?", (cid,))
    check(risk is not None, "an unbacked verified claim should appear in v_claim_risk")
    check(risk["severity"] == "high", f"unbacked 'verified' is high severity, got {risk}")

    # a discriminator is what makes it stick
    other = measured_claim(conn, "the permutation table is cyclic")
    core.record_discriminator(
        conn, claim_a=cid, claim_b=other,
        check_desc="mean key-to-key angular step over the clone tree, 26,991 steps",
        result_a="9.50 deg, 0.89% over 45",
        result_b="15.45 deg, 6.0% over 45",
        separation="7x on the fraction over 45 deg",
    )
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "verified",
          f"a discriminator should back it, got {got['effective_status']}")

    # A discriminator settles WHICH reading is right; it does not run the reading
    # over the corpus. So high severity clears and rule 4 exposure remains --
    # exactly the distinction AGENTS.md draws between the two.
    risk = core.one(conn, "SELECT * FROM v_claim_risk WHERE id=?", (cid,))
    check(risk is not None and risk["severity"] == "low",
          f"a discriminated but ungated claim is low risk, got {risk}")
    check(risk["risk"].startswith("no gate invariant"), str(risk))


def test_passing_gate_also_backs():
    conn = fresh()
    cid = measured_claim(conn, "every index lies inside its part's vertex range",
                         status="verified")
    conn.execute("INSERT INTO gate(id,name,trees,readers) VALUES (1,'corpus_mesh','[]','[]')")
    conn.execute("INSERT INTO gate_invariant(gate_id, claim_id, assertion) VALUES (1,?,?)",
                 (cid, "no index outside its part's range"))
    conn.execute("""INSERT INTO gate_run(gate_id, ts, commit_sha, ok, pass, fail, refused)
                    VALUES (1,'2026-09-04T00:00:00+00:00','deadbeef',1,3293,1,43)""")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "verified", got["effective_status"])
    check(got["n_passing_gates"] == 1, str(got["n_passing_gates"]))

    # a FAILING gate must not back it
    conn.execute("UPDATE gate_run SET ok=0")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "asserted-unbacked",
          f"a failing gate must not back a claim, got {got['effective_status']}")


def test_refutation_downgrades():
    conn = fresh()
    cid = seed_claim(conn, "nav .rax is the section 6.1 container", status="verified")
    core.record_refutation(
        conn, refuted_claim=cid,
        why_plausible="every other .rax tree is that container, and nav shares the extension",
        what_killed_it=("word 2 is the tree version on atlas/dgeom/animstream (8, 7, 5) "
                        "and is 0x6240 on nav; no root pointer at 0x10"),
        source={"kind": "shipped_file", "locator": "nav_xb_v5/.../bespin_-_nav.rax"},
    )
    got = core.one(conn, "SELECT * FROM v_claim_status WHERE id=?", (cid,))
    check(got["effective_status"] == "refuted", got["effective_status"])
    r = core.one(conn, "SELECT * FROM refutation WHERE refuted_claim=?", (cid,))
    check(r["why_plausible"] and "shares the extension" in r["why_plausible"],
          "why_plausible must be kept verbatim -- it is the expensive knowledge")
    check("0x6240" in r["what_killed_it"], "the killing evidence must be kept")


def test_tautology_is_flagged():
    conn = fresh()
    cid = measured_claim(conn, "the tangent frame is right-handed everywhere")
    core.record_tautology(
        conn, claim_id=cid,
        why_it_could_not_fail=("N, S and T were taken from three rows of one rotation "
                               "matrix, whose determinant is +1 by construction"))
    risk = core.one(conn, "SELECT * FROM v_claim_risk WHERE id=?", (cid,))
    check(risk is not None and "tautology" in risk["risk"], str(risk))


def test_capability_status_is_derived():
    conn = fresh()
    conn.execute("INSERT INTO build(id,name,platform,root_path) VALUES (1,'bf3_wii','broadway','/w')")
    conn.execute("INSERT INTO tree(id,build_id,name,path) VALUES (1,1,'embed_wi_v4','/w/e')")
    conn.execute("INSERT INTO tree(id,build_id,name,path) VALUES (2,1,'ob_wi_v194','/w/o')")
    conn.execute("INSERT INTO format(id,name,tree_id) VALUES (1,'embed_wi_v4',1)")
    conn.execute("INSERT INTO format(id,name,tree_id) VALUES (2,'ob_wi_v194',2)")
    cap = core.propose(conn, "capability", {"name": "wii_level_instantiation"})
    for fid in (1, 2):
        eid = core.propose(conn, "edge", {"src_type": "format", "src_id": fid,
                                          "kind": "gates", "dst_type": "capability",
                                          "dst_id": cap})
        core.review(conn, "edge", eid, "confirmed")

    got = core.one(conn, "SELECT * FROM v_capability WHERE id=?", (cap,))
    check(got["n_gating"] == 2, str(got))
    check(got["status"] == "blocked", f"no gates anywhere -> blocked, got {got['status']}")

    # give ob_wi_v194 a passing, fresh gate; embed stays ungated
    conn.execute("INSERT INTO gate(id,name,trees,readers) VALUES (1,'corpus_wii_ob','[\"ob_wi_v194\"]','[]')")
    conn.execute("""INSERT INTO gate_run(gate_id, ts, commit_sha, ok, pass)
                    VALUES (1,'2026-09-04T00:00:00+00:00','deadbeef',1,2904)""")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_capability WHERE id=?", (cap,))
    check(got["status"] == "partial", f"one of two ready -> partial, got {got['status']}")

    blocked = core.rows(conn, "SELECT * FROM v_blocked WHERE capability='wii_level_instantiation'")
    names = {b["blocking_format"] for b in blocked}
    check(names == {"embed_wi_v4"}, f"v_blocked should name only the unready format, got {names}")


def test_gate_staleness():
    conn = fresh()
    conn.execute("""INSERT INTO gate(id,name,trees,readers)
                    VALUES (1,'corpus_mesh','[]','["src/SelotapeDataLoaders.cpp"]')""")
    conn.commit()

    # never run
    got = core.one(conn, "SELECT * FROM v_gate_stale WHERE gate_id=1")
    check(got["stale"] == 1 and got["reason"] == "never run", str(got))

    # ancestry: c2 is HEAD (ordinal 0), c1 is older (ordinal 1)
    conn.executemany("INSERT INTO git_ancestry(commit_sha, ordinal) VALUES (?,?)",
                     [("c2", 0), ("c1", 1)])
    conn.execute("""INSERT INTO git_state(path,last_commit,dirty,scanned_at)
                    VALUES ('src/SelotapeDataLoaders.cpp','c1',0,'t')""")
    conn.execute("""INSERT INTO gate_run(id,gate_id,ts,commit_sha,tree_dirty,ok)
                    VALUES (1,1,'2026-09-04T00:00:00+00:00','c2',0,1)""")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_gate_stale WHERE gate_id=1")
    check(got["stale"] == 0, f"reader older than the run -> fresh, got {got}")

    # the reader now changes AFTER the run
    conn.execute("UPDATE git_state SET last_commit='c2'")
    conn.execute("UPDATE gate_run SET commit_sha='c1'")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_gate_stale WHERE gate_id=1")
    check(got["stale"] == 1, f"reader newer than the run -> stale, got {got}")
    check("SelotapeDataLoaders" in (got["reason"] or ""), str(got["reason"]))

    # a dirty reader is stale whatever the commits say
    conn.execute("UPDATE gate_run SET commit_sha='c2'")
    conn.execute("UPDATE git_state SET last_commit='c1', dirty=1")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_gate_stale WHERE gate_id=1")
    check(got["stale"] == 1, f"dirty reader -> stale, got {got}")

    # and a run taken against a dirty tree is stale on its own
    conn.execute("UPDATE git_state SET dirty=0")
    conn.execute("UPDATE gate_run SET tree_dirty=1")
    conn.commit()
    got = core.one(conn, "SELECT * FROM v_gate_stale WHERE gate_id=1")
    check(got["stale"] == 1 and "dirty working tree" in got["reason"], str(got))


def test_review_boundary():
    conn = fresh()
    tid = core.propose(conn, "todo", {"title": "Decode the Wii .dsp tree",
                                      "rationale": "35,017 files, decoder already in the repo"})
    q = core.rows(conn, "SELECT * FROM v_review_queue")
    check(len(q) == 1 and q[0]["kind"] == "todo", str(q))

    core.review(conn, "todo", tid, "confirmed")
    check(core.rows(conn, "SELECT * FROM v_review_queue") == [], "queue should drain on confirm")
    row = core.one(conn, "SELECT * FROM todo WHERE id=?", (tid,))
    check(row["review_state"] == "confirmed" and row["confirmed_at"], str(row))

    try:
        core.review(conn, "todo", tid, "maybe")
        raise AssertionError("an invalid decision was accepted")
    except BifrostError:
        pass


def test_close_todo_requires_confirmation_and_drops_out_of_the_digest():
    conn = fresh()
    tid = core.propose(conn, "todo", {"title": "Read the .wft font format",
                                      "rationale": "a version delta on a solved format"})

    # A proposal is rejected at review, never closed: 'done' has to mean the
    # work happened, so it cannot be reachable from something never agreed to.
    try:
        core.close_todo(conn, tid)
        raise AssertionError("an unconfirmed todo was closed")
    except BifrostError as e:
        check("not confirmed" in str(e), str(e))

    core.review(conn, "todo", tid, "confirmed")
    open_now = [t["title"] for t in digest.digest_data(conn)["todos"]]
    check(open_now == ["Read the .wft font format"], str(open_now))

    before = core.one(conn, "SELECT * FROM todo WHERE id=?", (tid,))
    check(before["closed_at"] is None, "an open todo has no closed_at")

    row = core.close_todo(conn, tid)
    check(row["status"] == "done", str(row))
    # The question this column exists to answer: when, not merely whether. A
    # todo closed by hand in SQL leaves it NULL, which is how the two are told
    # apart after the fact.
    # utcnow() is whole seconds, so a test that proposes and closes in the same
    # second gets equal stamps; ordering is what the column has to preserve.
    check(row["closed_at"] and row["closed_at"] >= row["confirmed_at"], str(row))
    check(digest.digest_data(conn)["todos"] == [], "a closed todo must leave OPEN WORK")

    for bad, why in ((tid, "closing twice"), (9999, "a todo that does not exist")):
        try:
            core.close_todo(conn, bad)
            raise AssertionError(f"{why} was accepted")
        except BifrostError:
            pass

    tid2 = core.propose(conn, "todo", {"title": "XMA1 decode"})
    core.review(conn, "todo", tid2, "confirmed")
    check(core.close_todo(conn, tid2, "abandoned")["status"] == "abandoned", "abandon")
    try:
        core.close_todo(conn, tid2, "in_progress")
        raise AssertionError("'in_progress' is not a closed status")
    except BifrostError:
        pass


def test_a_discriminator_can_be_recorded_before_it_is_run():
    """The moment you realise a check is needed had no verb.

    record_discriminator needs both results, so it can only be called once the
    question is settled -- while the moment worth capturing is the one where you
    have an observation, two readings and a cheap decisive check, and no results
    at all. Before this, that went into a gate run's prose and nothing surfaced
    it.
    """
    conn = fresh()
    a = seed_claim(conn, "the second mode is the cloth rigs")
    b = seed_claim(conn, "the second mode is a permutation error")

    try:
        core.propose_discriminator(conn, claim_a=a, claim_b=b,
                                   check_desc="compare the joint hashes",
                                   predicted_a="", predicted_b="cloth only")
        raise AssertionError("a proposal with no prediction was accepted")
    except BifrostError as e:
        check("prediction" in str(e), str(e))

    did = core.propose_discriminator(
        conn, claim_a=a, claim_b=b,
        check_desc="group the ~90 deg disagreements by joint name hash",
        predicted_a="all eight land on the same hash, which is a cloth joint",
        predicted_b="the hashes are spread across unrelated joints",
        why_decisive="a permutation error is indifferent to which joint it hits")

    open_now = core.rows(conn, "SELECT * FROM v_discriminator_open")
    check(len(open_now) == 1 and open_now[0]["id"] == did, str(open_now))
    check(open_now[0]["statement_a"] == "the second mode is the cloth rigs", str(open_now[0]))
    check("CHECKS WORTH RUNNING" in digest.render(conn), "and it must reach the digest")

    # Settling keeps predictions beside results, in the same row: whether the
    # reasoning was any good is exactly what two rows would lose.
    row = core.settle_discriminator(conn, did, result_a="all eight on hash 0x4d3f2a",
                                    result_b="-", separation="one hash vs eight")
    check(row["predicted_a"] and row["result_a"], str(row))
    check(core.rows(conn, "SELECT * FROM v_discriminator_open") == [], "and it leaves the open list")

    try:
        core.settle_discriminator(conn, did, result_a="x", result_b="y")
        raise AssertionError("settling twice was accepted")
    except BifrostError:
        pass
    return "proposed before it is run, settled into the same row"


def test_search_answers_a_question_you_cannot_name():
    """"Which claims mention decals" had no verb.

    realm needs a name you already have and query needs a view, so the only
    route was dumping all 276 claims and grepping the file -- 48,000 tokens to
    find three rows, and the most expensive gap a tester hit in a session.
    """
    from bifrost import search as search_mod
    conn = fresh()
    a = core.record_claim(
        conn, subject_type="format", subject_id=None,
        statement="Decals are runtime weapon-impact effects, not authored level dressing",
        citations=[{"kind": "disasm_fn", "locator": "0x825ae690"}])
    core.record_claim(
        conn, subject_type="format", subject_id=None,
        statement="The viewer binds only seven of the attribute set's slot names",
        layer="divergence",
        citations=[{"kind": "measurement", "locator": "seven slots"}])
    search_mod.reindex(conn)

    hits = search_mod.search(conn, "decals")
    check(len(hits) == 1 and hits[0]["ref"] == str(a), str(hits))
    check("retail" in hits[0]["extra"], f"the layer must be triageable: {hits[0]['extra']}")

    # A citation locator is searchable too: the address finds the claim.
    check(search_mod.search(conn, "0x825ae690", kind="claim"), "locators are indexed")

    # fts5 syntax in a bare term must not be an error. `obdef_s.flags` and a
    # hex address are what people actually type.
    for q in ("obdef_s.flags", "0x825ae690", "attribute set's"):
        search_mod.search(conn, q)      # must not raise

    check(search_mod.search(conn, "nothing_matches_this_xyzzy") == [], "and nothing is invented")
    try:
        search_mod.search(conn, "   ")
        raise AssertionError("an empty search was accepted")
    except BifrostError:
        pass

    # Hits are compact by design; returning whole rows recreates the problem.
    check(set(hits[0]) == {"kind", "ref", "title", "extra", "snippet"}, str(hits[0]))
    return "claims, comments, formats, todos and locators, in one index"


def test_a_claim_says_whether_it_is_about_them_or_about_us():
    """Claims 277 and 278 were filed under attribute_info and shader_db but are
    about the viewer's divergence from them, with only prose to say so."""
    conn = fresh()
    r = core.record_claim(conn, subject_type="format", subject_id=None,
                          statement="the retail engine sorts by shader pass bits",
                          citations=[{"kind": "disasm_fn", "locator": "0x825ae690"}])
    d = core.record_claim(conn, subject_type="format", subject_id=None,
                          statement="the viewer's opaque/alpha split is a single bit",
                          layer="divergence",
                          citations=[{"kind": "disasm_fn", "locator": "0x825ae691"}])
    check(core.one(conn, "SELECT layer FROM v_claim_status WHERE id=?", (r,))["layer"] == "retail",
          "retail is the default")
    check(core.one(conn, "SELECT layer FROM v_claim_status WHERE id=?",
                   (d,))["layer"] == "divergence", "and divergence sticks")
    try:
        core.record_claim(conn, subject_type="format", subject_id=None, statement="x",
                          layer="whatever",
                          citations=[{"kind": "measurement", "locator": "y"}])
        raise AssertionError("an unknown layer was accepted")
    except BifrostError as e:
        check("divergence" in str(e), str(e))


def test_a_path_citation_is_pinned_to_a_commit():
    """"src/SelotapeD3D11.cpp:3666-3698" rots the next time anyone edits it.

    It was recorded as `shipped_file`, which is doubly wrong: not shipped, and
    not theirs.
    """
    conn = fresh()
    sid = core.ensure_source(conn, "source_ref", "src/SelotapeD3D11.cpp:3666-3698")
    row = core.one(conn, "SELECT * FROM source WHERE id=?", (sid,))
    check(row["pinned_commit"], "a path citation must carry the commit it was true at")

    addr = core.ensure_source(conn, "disasm_fn", "0x825ae690", "bf3_360")
    check(core.one(conn, "SELECT pinned_commit FROM source WHERE id=?",
                   (addr,))["pinned_commit"] is None,
          "an address in the retail image does not move, so it is not pinned")
    return "paths pinned, addresses not"


def test_running_the_tests_cannot_migrate_the_project_database():
    """Running a test suite must not be able to alter production.

    main() migrated before dispatching any command, so `bifrost test` opened the
    project's database and applied pending migrations as a side effect. 009 then
    failed part-way and left that database with no discriminator table. Nothing
    about running tests suggests it will touch real data.
    """
    from bifrost import cli
    check("test" in cli.NO_DB, "the test command must not open the project database")
    src = (Path(cli.__file__)).read_text(encoding="utf-8")
    i, j = src.index("args = p.parse_args(argv)"), src.index("conn = core.connect(args.db)")
    check("NO_DB" in src[i:j], "the NO_DB check must come before connect(), not after")


def test_gate_view_says_where_the_probe_lives():
    """corpus_m0v's gate is a Python script, not the C++ probe of the same name.

    Finding that out meant grepping the project profile, after building and
    running the wrong one. Both paths were in `gate` and in no view.
    """
    conn = fresh()
    cols = [d[0] for d in conn.execute("SELECT * FROM v_gate_latest LIMIT 0").description]
    for c in ("probe_path", "exe_path", "finding", "todo_id"):
        check(c in cols, f"v_gate_latest should carry {c}: {cols}")


def test_close_is_not_on_the_mcp_surface():
    """The whole point: an agent cannot mark its own work done."""
    from bifrost import mcp_server
    names = [t["name"] for t in mcp_server.TOOLS]
    check(not any("close" in n for n in names), str(names))
    check("bifrost_propose" in names, str(names))


def test_edge_kind_validated():
    conn = fresh()
    try:
        core.propose(conn, "edge", {"src_type": "format", "src_id": 1, "kind": "vibes_with",
                                    "dst_type": "capability", "dst_id": 1})
        raise AssertionError("an unknown edge kind was accepted")
    except BifrostError as e:
        check("unknown edge kind" in str(e), str(e))


def test_rule6_constant_audit():
    conn = fresh()
    conn.execute("INSERT INTO format(id,name) VALUES (1,'x2t')")
    sid = core.ensure_source(conn, "data_addr", "0x8213EA70", note="__real@3dcccccd")
    conn.execute("INSERT INTO constant(format_id,value,meaning,source_id) VALUES (1,'0.1','scaleX10 multiplier',?)", (sid,))
    conn.execute("INSERT INTO constant(format_id,value,meaning) VALUES (1,'32','padding alignment')")
    conn.commit()
    bad = core.rows(conn, "SELECT * FROM v_constant_unsourced")
    check(len(bad) == 1 and bad[0]["value"] == "32", str(bad))


def test_measurement_only_audit():
    conn = fresh()
    measured = core.record_claim(conn, subject_type="format", subject_id=None,
                                 statement="the .cld payload is 2048 + w*h*d*4 bytes",
                                 citations=[{"kind": "measurement", "locator": "noise3d.cld"}])
    read = core.record_claim(conn, subject_type="format", subject_id=None,
                             statement="positions dequantise by 1/1024",
                             citations=[{"kind": "disasm_fn", "locator": "0x825ae690"}])
    ids = {r["id"] for r in core.rows(conn, "SELECT * FROM v_claim_measurement_only")}
    check(ids == {measured}, f"only the unread claim should flag, got {ids}")


def test_migration_coverage():
    conn = fresh()
    conn.execute("""INSERT INTO migration_source(id,path,commit_sha,ingested_at)
                    VALUES (1,'bf3_execution_roadmap.md','abc123','t')""")
    conn.executemany(
        """INSERT INTO migration_para(migration_source_id, ordinal, text,
                                      citation_tokens, accounted, accounted_kind)
           VALUES (1,?,?,?,?,?)""",
        [(0, "p0", '["0x825ae690"]', 1, "claim"),
         (1, "p1", "[]", 1, "passage"),
         (2, "p2", '["0x826351d0"]', 0, None)])
    conn.commit()
    cov = core.one(conn, "SELECT * FROM v_migration_coverage")
    check(cov["paragraphs"] == 3 and cov["accounted"] == 2 and cov["unaccounted"] == 1, str(cov))
    check(abs(cov["pct"] - 66.7) < 0.1, str(cov))


# --- tests that need the retail symbol database ----------------------------

def test_resolve_real_addresses():
    conn = fresh()
    if not core.attach_symbols(conn):
        return "skipped: no xenon_symbols.db at %s" % core.SYMBOLS_360

    r = core.resolve_address(conn, 0x825AE690)
    check(r.kind == "exact" and r.symbol == "obGetVertexPosHW", f"{r}")

    r = core.resolve_address(conn, 0x825B219C)          # cited mid-function
    check(r.kind == "inside" and "obinstTick" in r.containing, f"{r}")

    r = core.resolve_address(conn, 0x82068EE4)          # pi/180, a data address
    check(r.kind == "unresolved",
          "a constant-pool address must not resolve to a neighbouring function")

    return f"resolved obGetVertexPosHW, obinstTick+0x{r.offset or 0:x}, and a data address"


def test_source_records_resolution():
    conn = fresh()
    if not core.attach_symbols(conn):
        return "skipped: no symbol database"
    sid = core.ensure_source(conn, "disasm_fn", "0x8263EF70", "bf3_360")
    row = core.one(conn, "SELECT * FROM source WHERE id=?", (sid,))
    check(row["symbol"] == "pmeshCreateFromTTRFile", str(row))
    return "citation auto-resolved to pmeshCreateFromTTRFile"


def test_pdb_validates_field_offsets():
    conn = fresh()
    if not core.attach_symbols(conn):
        return "skipped: no symbol database"

    # roadmap 6.10 records this record as 0 / 6 / 10 / 16
    for field, offset in (("position[3]", 0), ("matrixIdx[4]", 6),
                          ("matrixWeight[3]", 10), ("packSTN[4]", 16)):
        row = core.pdb_field(conn, "skinInputVertexCompressed_s", field)
        check(row is not None, f"PDB has no skinInputVertexCompressed_s.{field}")
        check(row["offset"] == offset,
              f"{field}: roadmap says {offset}, PDB says {row['offset']}")

    conn.execute("INSERT INTO format(id,name) VALUES (1,'ob_xb_v184')")
    row = core.pdb_field(conn, "skinInputVertexCompressed_s", "matrixIdx[4]")
    for recorded, expect_valid in ((6, 1), (8, 0)):
        conn.execute(
            """INSERT INTO format_field(format_id, struct_name, offset, name,
                                        pdb_struct, pdb_field, pdb_validated)
               VALUES (1,'skinInputVertexCompressed_s',?,?,?,?,?)""",
            (recorded, "matrixIdx", "skinInputVertexCompressed_s", "matrixIdx[4]",
             1 if recorded == row["offset"] else 0))
    conn.commit()
    conflicts = core.rows(conn, "SELECT * FROM v_field_pdb_conflict")
    check(len(conflicts) == 1 and conflicts[0]["offset"] == 8,
          f"a wrong offset must surface as a conflict, got {conflicts}")
    return "4 fields checked against the retail PDB; a wrong offset surfaces"


# ---------------------------------------------------------------------------

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = skipped = 0
    print("=" * 60)
    print("   Bifrost — schema, invariants and derived state")
    print("=" * 60)
    for fn in TESTS:
        name = fn.__name__
        print(f"[RUNNING] {name}...")
        try:
            note = fn()
            if isinstance(note, str) and note.startswith("skipped"):
                print(f"  -> SKIPPED ({note[9:].strip()})")
                skipped += 1
            else:
                print(f"  -> PASSED{'  ' + note if note else ''}")
                passed += 1
        except Exception as e:  # noqa: BLE001 - a harness reports, it does not raise
            print(f"  -> FAILED: {type(e).__name__}: {e}")
            failed += 1
    print("=" * 60)
    print(f"   TEST RESULTS: {passed} / {passed + failed} passed"
          + (f", {skipped} skipped" if skipped else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
