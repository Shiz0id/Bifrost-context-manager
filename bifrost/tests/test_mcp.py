"""Bifrost MCP tests.

The important one is test_no_review_tool. Everything else here checks plumbing;
that one checks a structural guarantee -- that an agent cannot confirm its own
proposals, because the tool to do it does not exist on this surface.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, ingest, mcp_server  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def setup_server():
    """Point the module's lazily-created connection at a scratch database."""
    conn = core.connect(":memory:")
    core.migrate(conn)
    ingest.ingest_builds(conn)
    ingest.register_gates(conn)
    conn.execute("INSERT INTO format(name) VALUES ('ob_xb_v184')")
    conn.commit()
    mcp_server._conn = conn
    return conn


def rpc(method, params=None, mid=1):
    return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                              "params": params or {}})


def call(tool, args):
    return rpc("tools/call", {"name": tool, "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------

def test_initialize():
    setup_server()
    r = rpc("initialize")
    check(r["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION, str(r))
    check(r["result"]["serverInfo"]["name"] == "bifrost", str(r))
    check(rpc("notifications/initialized") is None,
          "a notification must not get a response")


def test_tools_list_is_well_formed():
    setup_server()
    tools = rpc("tools/list")["result"]["tools"]
    # Derived, not hardcoded: adding a tool must not break this test, the same
    # way adding a migration does not break test_migrations_apply.
    from bifrost import mcp_server
    check(len(tools) == len(mcp_server.TOOLS),
          f"tools/list returned {len(tools)}, TOOLS defines {len(mcp_server.TOOLS)}")
    check({t["name"] for t in tools} == {t["name"] for t in mcp_server.TOOLS},
          "tools/list must return exactly what TOOLS defines")
    for t in tools:
        check(t["name"].startswith("bifrost_"), t["name"])
        check(len(t["description"]) > 60, f"{t['name']}: description too thin for a model to route on")
        s = t["inputSchema"]
        check(s["type"] == "object" and "properties" in s, f"{t['name']}: bad schema")
        for req in s.get("required", []):
            check(req in s["properties"], f"{t['name']}: required {req!r} is not a property")
    return f"{len(tools)} tools, schemas well formed"


def test_no_review_tool():
    """The structural guarantee: agents propose, only the CLI confirms."""
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    for forbidden in ("bifrost_review", "bifrost_confirm", "bifrost_approve"):
        check(forbidden not in names, f"{forbidden} must not be reachable over MCP")
    check("bifrost_propose" in names, "agents must still be able to propose")
    return "no confirm/approve tool is reachable over MCP"


def test_digest_tool():
    setup_server()
    out = text_of(call("bifrost_digest", {}))
    check("BF3 CORPUS" in out, out[:200])
    check(len(out) < 20000, "the digest must stay small even on an empty database")


def test_claim_without_citation_is_refused():
    setup_server()
    r = call("bifrost_record_claim", {
        "format": "ob_xb_v184", "statement": "positions are three s16", "citations": []})
    check(r["result"].get("isError"), "a citation-free claim must be refused")
    check("[REFUSED]" in text_of(r), text_of(r))
    check("citation" in text_of(r), text_of(r))
    return "rule 6 enforced at the tool boundary"


def test_claim_roundtrip_and_downgrade():
    setup_server()
    # A MEASUREMENT-only claim: since migration 006 a disassembly citation is
    # itself rule-1 backing, so the downgrade this test is about only applies to
    # a bare statistic with no code and no artefact behind it.
    r = call("bifrost_record_claim", {
        "format": "ob_xb_v184",
        "statement": "positions dequantise by 1/1024",
        "asserted_status": "verified",
        "citations": [{"kind": "measurement", "locator": "AABBs over 1663 files"}]})
    check(not r["result"].get("isError"), text_of(r))
    got = json.loads(text_of(r))
    check(got["n_citations"] == 1, str(got))
    check(got["effective_status"] == "asserted-unbacked",
          f"an agent must not declare verified on a measurement alone, "
          f"got {got['effective_status']}")

    # a discriminator is what promotes it
    rival = json.loads(text_of(call("bifrost_record_claim", {
        "format": "ob_xb_v184", "statement": "positions are scaled by 1/32768",
        "citations": [{"kind": "measurement", "locator": "a guess"}]})))
    r2 = call("bifrost_record_evidence", {
        "kind": "discriminator", "claim_id": got["id"], "other_claim_id": rival["id"],
        "check": "decoded positions against each part's own stored AABB",
        "result_a": "reproduces the AABB to four decimals across 1663 files",
        "result_b": "every model 32x too small"})
    check(json.loads(text_of(r2))["claim"]["effective_status"] == "verified", text_of(r2))
    return "assert -> downgrade -> discriminate -> verified"


def test_unknown_format_is_refused():
    setup_server()
    r = call("bifrost_record_claim", {
        "format": "not_a_real_format", "statement": "x",
        "citations": [{"kind": "measurement", "locator": "y"}]})
    check(r["result"].get("isError"), "an unknown format must be refused")
    check("no format named" in text_of(r), text_of(r))


def test_propose_lands_unconfirmed():
    conn = setup_server()
    r = call("bifrost_propose", {"kind": "todo", "payload": {
        "title": "Decode the Wii .dsp tree", "difficulty": "low"}})
    got = json.loads(text_of(r))
    check(got["review_state"] == "proposed", str(got))
    row = core.one(conn, "SELECT review_state FROM todo WHERE id=?", (got["id"],))
    check(row["review_state"] == "proposed", str(row))
    q = core.rows(conn, "SELECT * FROM v_review_queue")
    check(len(q) == 1, str(q))
    return "proposals do not take effect until a human confirms"


def test_bad_edge_kind_is_refused():
    setup_server()
    r = call("bifrost_propose", {"kind": "edge", "payload": {
        "src_type": "format", "src_id": 1, "kind": "vibes_with",
        "dst_type": "capability", "dst_id": 1}})
    check(r["result"].get("isError"), "an unknown edge kind must be refused")


def test_record_run_tool():
    conn = setup_server()
    out = "1663 files, 3337 models: 3293 pass, 1 fail, 43 refused\n14021613 vertices"
    r = call("bifrost_record_run", {"gate": "corpus_mesh", "stdout": out, "commit": "abc"})
    got = json.loads(text_of(r))
    check(got["pass"] == 3293 and got["fail"] == 1, str(got))

    r = call("bifrost_record_run", {"gate": "nope", "stdout": "x"})
    check(r["result"].get("isError"), "an unknown gate must be refused")


def test_symbol_tool():
    setup_server()
    if not core.attach_symbols(mcp_server.conn()):
        return "skipped: no symbol database"

    got = json.loads(text_of(call("bifrost_symbol", {"addr": "0x825ae690"})))
    check(got["address"]["symbol"] == "obGetVertexPosHW", str(got))

    # a data address must come back unresolved WITH the explanation, so an agent
    # cites it correctly instead of treating it as a failure
    got = json.loads(text_of(call("bifrost_symbol", {"addr": "0x82068EE4"})))
    check(got["address"]["kind"] == "unresolved", str(got))
    check("data_addr" in got.get("note", ""), str(got))

    got = json.loads(text_of(call("bifrost_symbol", {
        "struct": "skinInputVertexCompressed_s", "field": "matrixIdx[4]"})))
    check(got["field"]["offset"] == 6, str(got))
    return "address, data address and PDB field lookup"


def test_unknown_method_and_tool():
    setup_server()
    r = rpc("nonsense/method")
    check(r["error"]["code"] == -32601, str(r))
    r = call("bifrost_nonexistent", {})
    check(r["result"].get("isError"), "an unknown tool must report an error result")


def test_serve_loop():
    """The transport itself: newline-delimited JSON in, JSON out."""
    setup_server()
    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + "\n"
        + "not json at all\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    out = io.StringIO()
    mcp_server.serve(inp, out)
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    check(len(lines) == 2, f"expected 2 responses (blank and malformed lines skipped), got {len(lines)}")
    check(lines[0]["id"] == 1 and lines[1]["id"] == 2, str(lines))
    return "blank and malformed lines are skipped without killing the loop"



def test_launcher_starts_from_any_cwd():
    """The failure that produced a bare 'Connection closed'.

    The server used to be started as `python -m bifrost.mcp_server` with a
    relative cwd. Launch it from anywhere else and the package is not on the
    path, the process exits on ModuleNotFoundError, and the client reports
    nothing a person could act on.
    """
    import json as _json, subprocess, sys as _sys, tempfile
    from pathlib import Path as _Path

    launcher = _Path(__file__).resolve().parents[2] / "bifrost_server.py"
    check(launcher.exists(), f"no launcher at {launcher}")

    p = subprocess.run(
        [_sys.executable, str(launcher)],
        input=_json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n",
        text=True, capture_output=True, cwd=tempfile.gettempdir(), timeout=60)
    check(p.returncode == 0, f"launcher exited {p.returncode}: {p.stderr[:300]}")
    out = [l for l in p.stdout.splitlines() if l.strip()]
    check(out, f"no response from a foreign cwd; stderr: {p.stderr[:300]}")
    got = _json.loads(out[0])
    check(got["result"]["serverInfo"]["name"] == "bifrost", str(got))
    return "starts from a temp directory, not just from tools/"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = skipped = 0
    print("=" * 60)
    print("   Bifrost — MCP surface")
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
