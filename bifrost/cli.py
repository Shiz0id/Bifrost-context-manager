"""Bifrost CLI — `python -m bifrost <command>`.

The human half of the interface, and deliberately a superset of the MCP surface:
`review` and `close` live here and only here, so an agent is structurally
incapable of confirming its own proposals or declaring its own work done.

    python -m bifrost bootstrap          scan, ingest symbols, register gates
    python -m bifrost seed               the mutable core (formats, capabilities)
    python -m bifrost digest             whole-project state, for a session start
    python -m bifrost realm <name>       everything known about one subject
    python -m bifrost trace <type> <id>  closure: what blocks what, what cites what
    python -m bifrost query <view>       any view, as a table or json
    python -m bifrost run <gate> [file]  ingest a gate run from stdout or a log
    python -m bifrost link               attach claims to formats, link evidence
    python -m bifrost dump / restore     the knowledge layer as committable JSONL
    python -m bifrost review             confirm or reject proposals
    python -m bifrost close <id>...      close a todo: done, or abandoned
    python -m bifrost test               run both test suites
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import core, digest, dump as dump_mod, ingest, seed as seed_mod

# Views an agent or a person may query by name. A fixed list rather than raw SQL:
# the point of a typed surface is that a caller cannot ask for something the
# schema does not promise to keep meaning the same thing.
QUERYABLE = {
    "blocked": "v_blocked",
    "capabilities": "v_capability",
    "formats": "v_format_status",
    "claims": "v_claim_status",
    "risk": "v_claim_risk",
    "stale": "v_gate_stale",
    "gates": "v_gate_latest",
    "review": "v_review_queue",
    "constant_unsourced": "v_constant_unsourced",
    "pdb_conflicts": "v_field_pdb_conflict",
    "measurement_only": "v_claim_measurement_only",
    "coverage": "v_migration_coverage",
    "trees": "tree",
    "exceptions": "exception",
    "todos": "todo",
    "sources": "source",
}


# Bifrost's own checkout. Never a project root: it has no `.bifrost/`, so
# find_root() falls through to the working directory and hands back a
# build/bifrost.db that migrate() then fills with a complete, empty schema.
BIFROST_REPO = Path(__file__).resolve().parent.parent

# Commands allowed to see an empty database -- they are how one stops being
# empty. For every other command zero formats means the wrong file was opened,
# and `(no rows)` is indistinguishable from "the project knows nothing".
EMPTY_DB_OK = {"bootstrap", "seed", "restore", "migrate", "test"}


def _empty_db_error(conn, args) -> str | None:
    """The message to print if this command needs a database that has content."""
    if args.cmd in EMPTY_DB_OK:
        return None
    if conn.execute("select count(*) from format").fetchone()[0]:
        return None
    from . import profile
    db = (Path(args.db) if args.db else core.default_db()).resolve()
    root = profile.load().root
    out = [f"[ERROR] no formats in {db}",
           "        The schema is present but the knowledge layer is empty, so this is"
           " almost certainly not the database you meant."]
    if root == BIFROST_REPO:
        out.append(f"        The project root resolved to Bifrost's own checkout ({root}),")
        out.append("        which is never a project -- BIFROST_PROJECT is unset.")
    else:
        out.append(f"        The project root resolved to {root}.")
    out += ["        Set it to the project directory:",
            "            export BIFROST_PROJECT=E:/Project",
            "        Run `bifrost bootstrap` instead if this project really is new."]
    return "\n".join(out)


def _table(rows: list[dict], max_width: int = 60) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    def cell(v):
        s = "" if v is None else str(v)
        return s if len(s) <= max_width else s[: max_width - 1] + "…"
    widths = {c: max(len(c), *(len(cell(r.get(c))) for r in rows)) for c in cols}
    out = [" | ".join(c.ljust(widths[c]) for c in cols),
           "-+-".join("-" * widths[c] for c in cols)]
    for r in rows:
        out.append(" | ".join(cell(r.get(c)).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def cmd_bootstrap(conn, args):
    stats = ingest.bootstrap(conn, scan=not args.no_scan)
    if args.backfill:
        print("[INFO] backfill:", ingest.backfill_source_symbols(conn))
    return 0


def cmd_seed(conn, args):
    seed_mod.seed(conn)
    return 0


def cmd_digest(conn, args):
    text = digest.render(conn, limit=args.limit)
    print(text)
    if args.check_budget:
        rep = digest.budget_report(text)
        print(f"\n[{'SUCCESS' if rep['within'] else 'ERROR'}] digest is "
              f"~{rep['est_tokens']} tokens against a budget of {rep['budget']}")
        return 0 if rep["within"] else 1
    return 0


def cmd_realm(conn, args):
    """Everything known about one subject. This is what replaces reading a
    roadmap section: status and WHY, fields, claims with their citations, gates,
    exceptions and edges, in one place."""
    name = args.name
    fmt = core.one(conn, "SELECT * FROM v_format_status WHERE name=?", (name,))
    cap = core.one(conn, "SELECT * FROM v_capability WHERE name=?", (name,))
    tree = core.one(conn, "SELECT * FROM tree WHERE name=?", (name,))

    if not (fmt or cap or tree):
        print(f"[ERROR] nothing named {name!r}. Try `bifrost query formats`.")
        return 1

    print(f"=== {name} ===")
    if fmt:
        f = core.one(conn, "SELECT * FROM format WHERE name=?", (name,))
        print(f"format   status={fmt['status']}  tree={fmt['tree']}  build={fmt['build']}"
              + (f"  sec {fmt['roadmap_anchor']}" if fmt["roadmap_anchor"] else ""))
        print(f"         gates={fmt['n_gates']} passing={fmt['n_passing']} "
              f"failing={fmt['n_failing']} stale={fmt['n_stale']}")
        if f and f["summary"]:
            print(f"         {f['summary']}")
    if cap:
        print(f"capability  status={cap['status']}  {cap['n_ready']}/{cap['n_gating']} formats ready")
        print(f"            {cap['description'] or ''}")
    if tree:
        hist = json.loads(tree["ext_histogram"] or "{}")
        top = ", ".join(f"{k} {v:,}" for k, v in sorted(hist.items(), key=lambda x: -x[1])[:5])
        print(f"tree     {tree['file_count']:,} files, {tree['bytes']/1048576:,.0f} MB  [{top}]")
        print(f"         {tree['path']}")

    fid = core.get_id(conn, "format", name)
    if fid:
        fields = core.rows(conn, "SELECT * FROM format_field WHERE format_id=? ORDER BY offset", (fid,))
        if fields:
            print(f"\n-- fields ({len(fields)}) --")
            for f in fields:
                flag = {1: " [PDB ok]", 0: " [PDB CONFLICT]"}.get(f["pdb_validated"], "")
                print(f"  +{f['offset']:<5} {f['name']:<22} {f['ctype'] or '':<12}{flag}")

        claims = core.rows(conn, """SELECT * FROM v_claim_status WHERE subject_type='format'
                                    AND subject_id=? ORDER BY id""", (fid,))
        if claims:
            print(f"\n-- claims ({len(claims)}) --")
            for c in claims:
                print(f"  [{c['effective_status']}] {c['statement']}")
                for ct in core.rows(conn, """SELECT s.kind, s.locator, s.symbol,
                                                    s.containing_symbol, s.offset_in_symbol, ct.note
                                             FROM citation ct JOIN source s ON s.id=ct.source_id
                                             WHERE ct.claim_id=?""", (c["id"],)):
                    sym = ct["symbol"] or (f"{ct['containing_symbol']}+0x{ct['offset_in_symbol']:x}"
                                           if ct["containing_symbol"] else "")
                    print(f"      {ct['kind']:<15} {ct['locator']}"
                          + (f"  ({sym})" if sym else "")
                          + (f"  -- {ct['note']}" if ct["note"] else ""))
                for r in core.rows(conn, "SELECT * FROM refutation WHERE refuted_claim=?", (c["id"],)):
                    print(f"      REFUTED: {r['what_killed_it']}")
                    if r["why_plausible"]:
                        print(f"      (it was plausible because: {r['why_plausible']})")
                for d in core.rows(conn, """SELECT * FROM discriminator
                                            WHERE claim_a=? OR claim_b=?""", (c["id"], c["id"])):
                    print(f"      DISCRIMINATOR: {d['check_desc']}")
                    print(f"        a={d['result_a']}  b={d['result_b']}"
                          + (f"  ({d['separation']})" if d["separation"] else ""))

    if tree:
        gates = core.rows(conn, """SELECT gl.name, gl.ok, gl.pass, gl.fail, gl.refused, gl.ts,
                                          gs.stale, gs.reason
                                   FROM v_gate_latest gl JOIN v_gate_stale gs ON gs.gate_id=gl.gate_id,
                                        json_each(COALESCE(gl.trees,'[]')) je
                                   WHERE je.value=?""", (tree["name"],))
        if gates:
            print(f"\n-- gates ({len(gates)}) --")
            for g in gates:
                print(f"  {'ok  ' if g['ok'] else 'FAIL'} {g['name']:<18} "
                      f"{g['pass']} pass, {g['fail']} fail, {g['refused']} refused"
                      + ("   STALE: " + (g["reason"] or "") if g["stale"] else ""))

    exs = core.rows(conn, """SELECT DISTINCT e.name, e.disposition, e.rationale
                             FROM exception e JOIN exception_gate eg ON eg.exception_id=e.id
                             JOIN gate g ON g.id=eg.gate_id, json_each(COALESCE(g.trees,'[]')) je
                             WHERE je.value=?""", (name,)) if tree else []
    if exs:
        print(f"\n-- accepted exceptions ({len(exs)}) --")
        for e in exs:
            print(f"  [{e['disposition']}] {e['name']}")

    for label, sql, params in (
        ("gates capability", "SELECT c.name FROM edge e JOIN capability c ON c.id=e.dst_id "
                             "WHERE e.kind='gates' AND e.src_type='format' AND e.src_id=?", (fid,)),
    ):
        if fid:
            got = [r["name"] for r in core.rows(conn, sql, params)]
            if got:
                print(f"\n-- {label} --\n  " + ", ".join(got))
    return 0


def cmd_trace(conn, args):
    """Closure over the edge graph. `trace format embed_wi_v4` answers what a
    format blocks; `trace source 0x826351d0` answers what rests on a reading."""
    if args.type == "source":
        rows = core.rows(conn, """SELECT c.id, c.statement, c.asserted_status
                                  FROM citation ct JOIN claim c ON c.id=ct.claim_id
                                  JOIN source s ON s.id=ct.source_id
                                  WHERE s.locator=? OR s.symbol=?""", (args.name, args.name))
        print(f"=== claims resting on {args.name} ({len(rows)}) ===")
        for r in rows:
            print(f"  [{r['asserted_status']}] {r['statement']}")
        return 0

    fid = core.get_id(conn, "format", args.name)
    if fid is None:
        print(f"[ERROR] no format named {args.name!r}")
        return 1
    caps = core.rows(conn, """SELECT c.name, vc.status, vc.n_ready, vc.n_gating
                              FROM edge e JOIN capability c ON c.id=e.dst_id
                              JOIN v_capability vc ON vc.id=c.id
                              WHERE e.kind='gates' AND e.src_type='format' AND e.src_id=?""", (fid,))
    st = core.one(conn, "SELECT status FROM v_format_status WHERE id=?", (fid,))
    print(f"=== {args.name} [{st['status']}] gates {len(caps)} capabilities ===")
    for c in caps:
        print(f"  {c['name']:<28} {c['status']:<10} {c['n_ready']}/{c['n_gating']} ready")
        for t in core.rows(conn, """SELECT t.title, t.status FROM edge e
                                    JOIN todo t ON t.id=e.dst_id
                                    WHERE e.kind='unlocks' AND e.src_type='capability'
                                      AND e.src_id=(SELECT id FROM capability WHERE name=?)""",
                           (c["name"],)):
            print(f"      unlocks: {t['title']}")
    return 0


def cmd_query(conn, args):
    view = QUERYABLE.get(args.view)
    if not view:
        print(f"[ERROR] unknown view {args.view!r}. Known: {', '.join(sorted(QUERYABLE))}")
        return 1
    sql, params = f"SELECT * FROM {view}", []
    if args.severity:
        # v_claim_risk is 165 rows of which 158 are the low-severity "no gate
        # invariant" (rule 4) exposure, so the handful that need a decision are
        # 4% of what the view prints. Filtering is what makes it readable.
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]
        if "severity" not in cols:
            print(f"[ERROR] view {args.view!r} has no severity column")
            return 1
        known = {r["severity"] for r in core.rows(
            conn, f"SELECT DISTINCT severity FROM {view}") if r["severity"]}
        wanted = [s.strip().lower() for s in args.severity.split(",") if s.strip()]
        if bad := [s for s in wanted if s not in known]:
            print(f"[ERROR] unknown severity {', '.join(bad)}. "
                  f"In this view: {', '.join(sorted(known))}")
            return 1
        sql += " WHERE severity IN (%s)" % ",".join("?" * len(wanted))
        params += wanted
    rows = core.rows(conn, sql + " LIMIT ?", (*params, args.limit))
    print(json.dumps(rows, indent=2) if args.json else _table(rows))
    return 0


def cmd_run(conn, args):
    text = Path(args.logfile).read_text(encoding="utf-8") if args.logfile else sys.stdin.read()
    try:
        rid = ingest.ingest_gate_run(conn, args.gate, text,
                                     stdout_dir=core.repo_root() / "build" / "bifrost_logs",
                                     note=args.note, supersedes=args.supersedes)
    except core.BifrostError as e:
        # Most often: the wrong thing got piped in. Nothing was written.
        print(f"[ERROR] {e}")
        return 1
    row = core.one(conn, "SELECT * FROM gate_run WHERE id=?", (rid,))
    m = json.loads(row["metrics"] or "{}")
    print(f"[{'SUCCESS' if row['ok'] else 'ERROR'}] {args.gate}: "
          f"{row['pass']} pass, {row['fail']} fail, {row['refused']} refused"
          + (f" ({m['unexplained_failures']} unexplained)"
             if m.get("unexplained_failures") else "")
          + ("  [dirty tree]" if row["tree_dirty"] else ""))
    if row["supersedes"]:
        print(f"[INFO] run #{rid} supersedes #{row['supersedes']}, which the views "
              f"and the digest now skip")
    if not ingest.parse_gate_stdout(args.gate, text)["declared"]:
        print(f"[INFO] no BIFROST-RESULT block, so counts came from the summary line "
              f"and no metrics were recorded. See README, 'Declaring a result'.")
    return 0 if row["ok"] else 1


def cmd_review(conn, args):
    q = core.rows(conn, "SELECT * FROM v_review_queue")
    if not q:
        print("(nothing awaiting review)")
        return 0
    if not args.confirm and not args.reject:
        print(_table(q))
        print("\nConfirm with:  python -m bifrost review --confirm todo:12")
        return 0
    for spec in (args.confirm or []) + (args.reject or []):
        kind, _, rid = spec.partition(":")
        decision = "confirmed" if spec in (args.confirm or []) else "rejected"
        core.review(conn, kind, int(rid), decision)
        print(f"[SUCCESS] {kind} #{rid} {decision}")
    return 0


def cmd_close(conn, args):
    """Close a todo. CLI-only, like review: see core.close_todo."""
    if not args.id:
        q = core.rows(conn, """
            SELECT id, COALESCE(priority, 999) pri, difficulty, status, title
            FROM todo WHERE review_state='confirmed' AND status IN ('open','in_progress')
            ORDER BY pri, id""")
        if not q:
            print("(nothing open)")
            return 0
        print(_table(q))
        print("\nClose with:  python -m bifrost close 4")
        print("             python -m bifrost close 4 --status abandoned")
        return 0
    rc = 0
    for tid in args.id:
        try:
            row = core.close_todo(conn, tid, args.status)
        except core.BifrostError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"[SUCCESS] todo #{tid} {row['status']}: {row['title']}")
    return rc


def cmd_dump(conn, args):
    """Write the hand-written knowledge layer out as committable JSONL."""
    if not dump_mod.verify(conn):
        print("[ERROR] the dump is not deterministic; refusing to write")
        return 1
    dump_mod.dump(conn)
    return 0


def cmd_restore(conn, args):
    dump_mod.restore(conn)
    return 0


def cmd_link(conn, args):
    """Attach migrated claims to their formats, and link the evidence.

    All three steps are idempotent, so this is safe to re-run after a migration
    pass adds claims.
    """
    from . import reattach, invariants, discriminators
    reattach.run(conn)
    invariants.run(conn)
    discriminators.run(conn)

    tot = core.one(conn, "SELECT COUNT(*) n FROM claim")["n"]
    gated = core.one(conn, "SELECT COUNT(DISTINCT claim_id) n FROM gate_invariant")["n"]
    print(f"\n[INFO] {gated} of {tot} claims carry a gate invariant "
          f"({100 * gated / tot:.0f}%); the rest are rule 4 exposure, reported at "
          f"LOW in v_claim_risk")
    return 0


def cmd_migrate(conn, args):
    """Wave 3: ingest a document, run the mechanical pass, report coverage."""
    from . import migrate as mg
    if args.ingest:
        st = mg.ingest_document(conn)
        print(f"[SUCCESS] {st['path']} @ {st['commit'][:7]}: {st['blocks']} blocks, "
              f"{st['tokens']} citation tokens ({st['distinct_tokens']} distinct)")
    if args.mechanical:
        mg.mechanical_pass(conn)
    print()
    print(mg.report(conn))
    pres = mg.citation_preservation(conn)
    return 0 if pres["ok"] else 1


def cmd_render(conn, args):
    from . import render as rd
    if not rd.render_twice_identical(conn):
        print("[ERROR] the renderer is not deterministic; refusing to write. "
              "A non-deterministic render makes the committed file unreviewable.")
        return 1
    if args.stdout:
        print(rd.render(conn))
        return 0
    r = rd.render_to_file(conn)
    print(f"[SUCCESS] {r['path']}: {r['bytes']:,} bytes, {r['lines']:,} lines "
          f"from source commit {r['source_commit'][:7]}"
          + ("" if r["changed"] else "  (unchanged)"))
    return 0


def cmd_test(conn, args):
    here = Path(__file__).resolve().parent / "tests"
    rc = 0
    for f in sorted(here.glob("test_*.py")):
        r = subprocess.run([sys.executable, str(f)], cwd=str(core.repo_root()))
        rc |= r.returncode
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bifrost", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=None, help="database path (default build/bifrost.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("bootstrap"); s.set_defaults(fn=cmd_bootstrap)
    s.add_argument("--no-scan", action="store_true")
    s.add_argument("--backfill", action="store_true",
                   help="re-resolve address citations recorded before a symbol map existed")

    s = sub.add_parser("seed"); s.set_defaults(fn=cmd_seed)

    s = sub.add_parser("digest"); s.set_defaults(fn=cmd_digest)
    s.add_argument("--limit", type=int, default=12)
    s.add_argument("--check-budget", action="store_true")

    s = sub.add_parser("realm"); s.set_defaults(fn=cmd_realm)
    s.add_argument("name")

    s = sub.add_parser("trace"); s.set_defaults(fn=cmd_trace)
    s.add_argument("type", choices=["format", "source"])
    s.add_argument("name")

    s = sub.add_parser("query"); s.set_defaults(fn=cmd_query)
    s.add_argument("view", nargs="?", default="blocked")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.add_argument("--severity", metavar="high,medium",
                   help="comma-separated, for views that carry a severity")

    s = sub.add_parser("run"); s.set_defaults(fn=cmd_run)
    s.add_argument("gate")
    s.add_argument("logfile", nargs="?", help="omit to read stdout from stdin")
    s.add_argument("--note", help="what YOU concluded; stdout stays the probe's own output")
    s.add_argument("--supersedes", type=int, metavar="RUN_ID",
                   help="retract an earlier run of this gate that was a recording "
                        "error rather than a result; the row stays, the views skip it")

    s = sub.add_parser("review"); s.set_defaults(fn=cmd_review)
    s.add_argument("--confirm", action="append")
    s.add_argument("--reject", action="append")

    s = sub.add_parser("close"); s.set_defaults(fn=cmd_close)
    s.add_argument("id", nargs="*", type=int,
                   help="todo ids to close; omit to list what is open")
    s.add_argument("--status", choices=list(core.CLOSED_STATUSES), default="done",
                   help="done: the work landed. abandoned: it will not happen.")

    s = sub.add_parser("dump"); s.set_defaults(fn=cmd_dump)

    s = sub.add_parser("restore"); s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("link"); s.set_defaults(fn=cmd_link)

    s = sub.add_parser("migrate"); s.set_defaults(fn=cmd_migrate)
    s.add_argument("--ingest", action="store_true", help="re-read the source document")
    s.add_argument("--mechanical", action="store_true",
                   help="account code, tables and uncited prose as passages")

    s = sub.add_parser("render"); s.set_defaults(fn=cmd_render)
    s.add_argument("--stdout", action="store_true", help="print instead of writing the file")

    s = sub.add_parser("test"); s.set_defaults(fn=cmd_test)

    args = p.parse_args(argv)
    conn = core.connect(args.db)
    core.migrate(conn)
    err = _empty_db_error(conn, args)
    if err:
        print(err, file=sys.stderr)
        return 2
    core.attach_symbols(conn)
    return args.fn(conn, args)


if __name__ == "__main__":
    raise SystemExit(main())
