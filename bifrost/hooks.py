"""Bifrost hooks — the enforcement tier.

Three hooks, each doing one job, and the logic lives here rather than in a shell
one-liner inside settings.json so it can be tested.

    SessionStart   inject the digest, so every agent starts from database state
                   instead of from a 215 KB markdown file
    PreToolUse     block a hand-edit of a GENERATED document and say what to
                   change instead -- the teeth of primary-source-of-truth
    PostToolUse    a corpus probe just ran -> ingest it, or ask for it
    Stop           a reader was edited and never re-gated -> say so

One thing deliberately NOT done: there is no per-turn logging mandate. That would
manufacture rows written to satisfy a hook, which is synthesised data by another
name and exactly what AGENTS.md rule 2 forbids. These fire on events that
actually happened.

Every hook fails OPEN. A hook that breaks a session because a database is missing
is worse than no hook at all, so anything unexpected prints nothing and exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import core, digest, ingest  # noqa: E402

# A Bash command that ran one of the corpus probes. The gate name is the probe's
# own executable name, which is also its key in the gate table.
PROBE_RE = re.compile(r"\b(corpus_[a-z_0-9]+)(?:\.exe)?\b")


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return {}


def _conn():
    conn = core.connect()
    core.migrate(conn)
    core.attach_symbols(conn)
    return conn


# ---------------------------------------------------------------------------

def session_start() -> int:
    """Inject whole-project state as additionalContext.

    This is the whole point of the system. The most expensive thing about
    arriving on this project is not answering a question, it is not knowing
    there was a question.
    """
    conn = _conn()
    text = digest.render(conn)
    rep = digest.budget_report(text)
    if not rep["within"]:
        # Never blow the context budget silently; truncate and say so.
        text = text[: digest.BUDGET_TOKENS * digest.CHARS_PER_TOKEN]
        text += "\n[TRUNCATED: digest exceeded its token budget -- run `bifrost digest --check-budget`]"
    _emit({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext":
            "Project state from Bifrost, the source of truth for this corpus. "
            "Query it with the bifrost_* MCP tools or `python -m bifrost` rather "
            "than reading generated documents.\n\n" + text}})
    return 0


def pre_tool_use() -> int:
    """Block hand-edits to generated documents and redirect to the database.

    This is the teeth of primary-source-of-truth. Without it the roadmap drifts
    back into being hand-maintained within a week, and the next `bifrost render`
    silently discards whatever was typed -- which is worse than either the file
    or the database being authoritative on its own.
    """
    payload = _read_stdin()
    ti = payload.get("tool_input") or {}
    path = (ti.get("file_path") or ti.get("path") or "").replace("\\", "/")
    if not path:
        return 0

    conn = _conn()
    generated = {r["path"] for r in core.rows(conn, "SELECT DISTINCT path FROM migration_source")}
    if not any(path.endswith(g) for g in generated):
        return 0

    _emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason":
            f"{path} is GENERATED from Bifrost and a `bifrost render` would discard "
            f"this edit. Change the row instead:\n"
            f"  bifrost_record_claim / bifrost_record_evidence  to state or correct a claim\n"
            f"  python -m bifrost realm <format>                to see what is already there\n"
            f"  python -m bifrost render                        to regenerate the file"}})
    return 0


def post_tool_use() -> int:
    """A corpus probe just ran. Record it if the output is reachable, and ask for
    it if it is not -- compliance by ergonomics, since the log writes itself."""
    payload = _read_stdin()
    cmd = ((payload.get("tool_input") or {}).get("command") or "")
    m = PROBE_RE.search(cmd)
    if not m:
        return 0
    gate = m.group(1)

    conn = _conn()
    if core.get_id(conn, "gate", gate) is None:
        return 0

    resp = payload.get("tool_response") or {}
    out = ""
    if isinstance(resp, dict):
        for key in ("stdout", "output", "content", "result"):
            v = resp.get(key)
            if isinstance(v, str) and v.strip():
                out = v
                break
    elif isinstance(resp, str):
        out = resp

    if not out.strip():
        # The harness did not hand us the output. Ask rather than guess -- a run
        # recorded from output nobody saw would be exactly the fabricated status
        # this database exists to prevent.
        _emit({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext":
                f"[bifrost] {gate} ran but its output was not available to the hook. "
                f"Record it with bifrost_record_run(gate=\"{gate}\", stdout=...) so the "
                f"gate does not stay stale."}})
        return 0

    try:
        rid = ingest.ingest_gate_run(
            conn, gate, out, stdout_dir=core.repo_root() / "build" / "bifrost_logs")
    except Exception as e:  # noqa: BLE001
        _emit({"systemMessage": f"[bifrost] could not record {gate}: {e}"})
        return 0

    row = core.one(conn, "SELECT ok, pass, fail, refused, tree_dirty, metrics "
                         "FROM gate_run WHERE id=?", (rid,))
    metrics = json.loads(row["metrics"] or "{}")
    verdict = "ok" if row["ok"] else "FAILING"
    detail = (f"{row['pass']} pass, {row['fail']} fail, {row['refused']} refused"
              if row["pass"] is not None else "no counts parsed")
    extra = ""
    if metrics.get("unexplained_failures"):
        extra = (f" -- {metrics['unexplained_failures']} failure(s) not covered by a "
                 f"registered exception; register one or fix the reader")
    if row["tree_dirty"]:
        extra += " [run against a dirty working tree, so it is already stale]"

    _emit({"systemMessage": f"[bifrost] recorded {gate}: {verdict}, {detail}{extra}"})
    return 0


def stop() -> int:
    """Say what was left undone.

    Only one check, and it is the one that would have caught this session's own
    problem: a reader was edited and no gate exercising it has been run since.
    """
    try:
        conn = _conn()
    except Exception:  # noqa: BLE001
        return 0

    stale = core.rows(conn, """
        SELECT name, reason FROM v_gate_stale
        WHERE stale = 1 AND reason IS NOT NULL AND reason != 'never run'""")
    if not stale:
        return 0

    lines = [f"  {s['name']}: {s['reason']}" for s in stale[:6]]
    more = f"\n  (+{len(stale) - 6} more)" if len(stale) > 6 else ""
    _emit({"systemMessage":
           f"[bifrost] {len(stale)} gate(s) no longer describe the current engine:\n"
           + "\n".join(lines) + more
           + "\nRe-run them, or their recorded results are claims about code that "
             "has since changed."})
    return 0


COMMANDS = {"session_start": session_start, "pre_tool_use": pre_tool_use,
            "post_tool_use": post_tool_use, "stop": stop}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(f"usage: hooks.py {{{'|'.join(COMMANDS)}}}\n")
        return 2
    try:
        return COMMANDS[argv[0]]()
    except Exception as e:  # noqa: BLE001
        # Fail open, always. A hook must never break a session.
        sys.stderr.write(f"[bifrost hook] {type(e).__name__}: {e}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
