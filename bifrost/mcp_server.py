"""Bifrost MCP server — newline-delimited JSON-RPC 2.0 over stdio.

No SDK dependency, for the same reason tools/wii/png.py carries its own PNG
reader: this repository is easier to move when its tooling does not need an
install step. The protocol surface used here is three methods -- initialize,
tools/list, tools/call.

Nine tools, and one deliberate omission: there is no bifrost_review. Confirming
and rejecting proposals lives in the CLI only, so an agent is structurally
incapable of approving its own work.

Register with:
    claude mcp add bifrost -- python -m bifrost.mcp_server
or in .mcp.json:
    {"mcpServers": {"bifrost": {"command": "python",
                                "args": ["-m", "bifrost.mcp_server"],
                                "cwd": "E:/Project/tools"}}}
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from . import core, digest
from . import ingest as ingest_mod
from .cli import QUERYABLE, cmd_realm
from .core import BifrostError

PROTOCOL_VERSION = "2024-11-05"

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = core.connect()
        core.migrate(_conn)
        core.attach_symbols(_conn)
    return _conn


# ---------------------------------------------------------------------------
# tool definitions
# ---------------------------------------------------------------------------

def _str(desc, **kw):
    return {"type": "string", "description": desc, **kw}


CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(core.SOURCE_KINDS),
                 "description": "disasm_fn for a function you disassembled; data_addr for a "
                                "constant pool, table or string blob (these have no symbol and "
                                "that is correct, not a failure); pdb_type for a struct in the "
                                "retail PDB; build_manifest for a converter decision; "
                                "measurement for a corpus figure with no code read behind it."},
        "locator": _str("0x825ae690, obdef_s.numParts, a shipped path, or a document section"),
        "build": {"type": "string", "enum": ["bf3_360", "bf3_wii"]},
        "note": _str("why this source supports the claim"),
    },
    "required": ["kind", "locator"],
}

TOOLS = [
    {
        "name": "bifrost_digest",
        "description": (
            "Whole-project state for the BF3 360/Wii corpus in a few hundred tokens: blocked "
            "capabilities and what blocks them, stale gate runs, claims at risk, proposals "
            "awaiting review, and recent gate results. Call this FIRST in any session touching "
            "this project -- it replaces reading bf3_execution_roadmap.md."),
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 12}}},
    },
    {
        "name": "bifrost_realm",
        "description": (
            "Everything known about one format, tree or capability: derived status and why, "
            "field layouts with their PDB validation state, every claim with its citations, "
            "refutations and discriminators, gate results, accepted exceptions and edges. "
            "This is what replaces reading a roadmap section."),
        "inputSchema": {"type": "object", "properties": {
            "name": _str("e.g. ob_xb_v184, embed_wi_v4, ai_pathing")}, "required": ["name"]},
    },
    {
        "name": "bifrost_query",
        "description": ("Read one of the project's derived views. Views: "
                        + ", ".join(sorted(QUERYABLE))),
        "inputSchema": {"type": "object", "properties": {
            "view": {"type": "string", "enum": sorted(QUERYABLE)},
            "limit": {"type": "integer", "default": 50}}, "required": ["view"]},
    },
    {
        "name": "bifrost_symbol",
        "description": (
            "Resolve an address or name against the retail symbol databases -- 42,533 methods "
            "and 286,660 struct fields for the 360, 3,113 map symbols for the Wii. Returns an "
            "exact hit, or the containing function and offset, or unresolved. Unresolved is "
            "often CORRECT: the 360 map holds functions and classes only, so a constant pool "
            "has no symbol, and the Wii map is sparse with gaps up to 185 KB."),
        "inputSchema": {"type": "object", "properties": {
            "addr": _str("hex address, e.g. 0x825ae690"),
            "struct": _str("struct name, to list its fields with offsets"),
            "field": _str("field within struct, to check one offset"),
            "build": {"type": "string", "enum": ["bf3_360", "bf3_wii"], "default": "bf3_360"}}},
    },
    {
        "name": "bifrost_trace",
        "description": (
            "Closure over the project graph. type=format gives the capabilities a format gates "
            "and what they unlock -- 'what does this block?'. type=source gives every claim "
            "resting on an address or symbol -- the blast radius when a reading is revised."),
        "inputSchema": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["format", "source"]},
            "name": _str("format name, or an address/symbol")}, "required": ["type", "name"]},
    },
    {
        "name": "bifrost_record_run",
        "description": (
            "Record a corpus gate run by handing over the probe's own stdout, which is parsed "
            "for pass/fail/refused. Failures already covered by a registered exception do not "
            "fail the gate; unexplained ones do. Append-only, no review."),
        "inputSchema": {"type": "object", "properties": {
            "gate": _str("e.g. corpus_mesh"),
            "stdout": _str("the probe's complete output"),
            "commit": _str("commit the run was made at; defaults to HEAD")},
            "required": ["gate", "stdout"]},
    },
    {
        "name": "bifrost_record_claim",
        "description": (
            "Append a claim about a format, with its citations. REJECTED without at least one "
            "citation -- that is AGENTS.md rule 6 enforced here rather than left to discipline. "
            "asserted_status is only what you claim: an unbacked 'verified' is downgraded to "
            "'asserted-unbacked' until a passing gate invariant or a discriminator backs it. "
            "Use 'assumed' for something believed but untested."),
        "inputSchema": {"type": "object", "properties": {
            "format": _str("the format this is about"),
            "statement": _str("one checkable assertion"),
            "asserted_status": {"type": "string", "enum": ["assumed", "plausible", "verified"],
                                "default": "plausible"},
            "citations": {"type": "array", "items": CITATION_SCHEMA, "minItems": 1},
            "roadmap_anchor": _str("e.g. 6.13")},
            "required": ["format", "statement", "citations"]},
    },
    {
        "name": "bifrost_record_evidence",
        "description": (
            "Record evidence about existing claims. kind=discriminator for the check that "
            "SEPARATES two candidate readings, with both results -- this is the row that would "
            "have caught four of the five defects corrected on 2-4 Sep 2026, each of which "
            "survived a gate the wrong reading also passed. kind=refutation retires a reading, "
            "keeping why it was plausible. kind=tautology flags a check that could not have "
            "failed, so it is never counted as evidence again."),
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["discriminator", "refutation", "tautology"]},
            "claim_id": {"type": "integer"},
            "other_claim_id": {"type": "integer", "description": "discriminator: the rival reading"},
            "check": _str("discriminator: what was measured"),
            "result_a": _str("discriminator: result for claim_id"),
            "result_b": _str("discriminator: result for the rival, or the control"),
            "separation": _str("discriminator: how far apart they are"),
            "why_plausible": _str("refutation: why the wrong reading looked right"),
            "what_killed_it": _str("refutation: the evidence that settled it"),
            "why_it_could_not_fail": _str("tautology: why the check was vacuous")},
            "required": ["kind", "claim_id"]},
    },
    {
        "name": "bifrost_propose",
        "description": (
            "Propose a change to mutable project state -- a todo, a capability, or an edge. "
            "Lands as 'proposed' and takes effect only once a human confirms it at the CLI. "
            "This is the only tool that writes state rather than history."),
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["todo", "capability", "edge"]},
            "payload": {"type": "object", "description":
                        "todo: {title, rationale, difficulty, priority}. "
                        "capability: {name, description}. "
                        "edge: {src_type, src_id, kind, dst_type, dst_id}"}},
            "required": ["kind", "payload"]},
    },
]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def _capture_realm(name: str) -> str:
    import io
    from contextlib import redirect_stdout

    class A:
        pass
    a = A(); a.name = name
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_realm(conn(), a)
    return buf.getvalue()


def call_tool(name: str, args: dict) -> Any:
    c = conn()

    if name == "bifrost_digest":
        return digest.render(c, limit=args.get("limit", 12))

    if name == "bifrost_realm":
        return _capture_realm(args["name"])

    if name == "bifrost_query":
        view = QUERYABLE.get(args["view"])
        if not view:
            raise BifrostError(f"unknown view {args['view']!r}")
        return core.rows(c, f"SELECT * FROM {view} LIMIT ?", (args.get("limit", 50),))

    if name == "bifrost_symbol":
        out: dict = {}
        if args.get("addr"):
            va = int(args["addr"], 16)
            r = core.resolve_address(c, va, args.get("build", "bf3_360"))
            out["address"] = {"kind": r.kind, "symbol": r.symbol,
                              "containing": r.containing, "offset": r.offset}
            if r.kind == "unresolved":
                out["note"] = ("No symbol. This is often correct: the 360 map holds functions "
                               "and classes only, so constant pools and tables have none, and "
                               "the Wii map is sparse. Cite it as kind=data_addr with its "
                               "meaning as the note.")
        if args.get("struct"):
            if args.get("field"):
                row = core.pdb_field(c, args["struct"], args["field"])
                out["field"] = dict(row) if row else None
            else:
                out["fields"] = core.rows(
                    c, "SELECT name, type, offset, size FROM sym.fields "
                       "WHERE class=? ORDER BY offset", (args["struct"],)) \
                    if core.has_symbols(c) else []
        return out

    if name == "bifrost_trace":
        import io
        from contextlib import redirect_stdout

        class A:
            pass
        a = A(); a.type = args["type"]; a.name = args["name"]
        from .cli import cmd_trace
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_trace(c, a)
        return buf.getvalue()

    if name == "bifrost_record_run":
        rid = ingest_mod.ingest_gate_run(
            c, args["gate"], args["stdout"], commit_sha=args.get("commit"),
            stdout_dir=core.repo_root() / "build" / "bifrost_logs")
        row = core.one(c, "SELECT * FROM gate_run WHERE id=?", (rid,))
        return {k: row[k] for k in ("id", "ok", "pass", "fail", "refused",
                                    "tree_dirty", "metrics")}

    if name == "bifrost_record_claim":
        fid = core.get_id(c, "format", args["format"])
        if fid is None:
            raise BifrostError(f"no format named {args['format']!r}; "
                               f"propose it first or use bifrost_query view=formats")
        cid = core.record_claim(
            c, subject_type="format", subject_id=fid, statement=args["statement"],
            citations=args["citations"],
            asserted_status=args.get("asserted_status", "plausible"),
            created_by="agent", roadmap_anchor=args.get("roadmap_anchor"))
        return core.one(c, "SELECT id, effective_status, n_citations FROM v_claim_status "
                           "WHERE id=?", (cid,))

    if name == "bifrost_record_evidence":
        k = args["kind"]
        if k == "discriminator":
            eid = core.record_discriminator(
                c, claim_a=args["claim_id"], claim_b=args.get("other_claim_id"),
                check_desc=args["check"], result_a=args["result_a"],
                result_b=args["result_b"], separation=args.get("separation"))
        elif k == "refutation":
            eid = core.record_refutation(
                c, refuted_claim=args["claim_id"],
                what_killed_it=args["what_killed_it"],
                why_plausible=args.get("why_plausible"))
        else:
            eid = core.record_tautology(
                c, claim_id=args["claim_id"],
                why_it_could_not_fail=args["why_it_could_not_fail"])
        return {"id": eid, "kind": k,
                "claim": core.one(c, "SELECT id, effective_status FROM v_claim_status WHERE id=?",
                                  (args["claim_id"],))}

    if name == "bifrost_propose":
        rid = core.propose(c, args["kind"], args["payload"], proposed_by="agent")
        return {"id": rid, "kind": args["kind"], "review_state": "proposed",
                "note": "Awaiting human confirmation: `python -m bifrost review`"}

    raise BifrostError(f"unknown tool {name!r}")


def handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bifrost", "version": "1.0.0"}}}

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        except BifrostError as e:
            # A refusal is a result, not a transport error: the agent should see
            # WHY (usually "this needs a citation") and fix its call.
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"[REFUSED] {e}"}],
                               "isError": True}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text",
                                            "text": f"[ERROR] {type(e).__name__}: {e}\n"
                                                    f"{traceback.format_exc()}"}],
                               "isError": True}}

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
