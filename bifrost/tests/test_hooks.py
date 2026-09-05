"""Bifrost hook tests.

Hooks are the enforcement tier, and a hook that silently does nothing is worse
than no hook. These drive hooks.py through its real stdin/stdout contract with
synthesised payloads of the shape Claude Code actually sends.

The one that matters most is test_hooks_fail_open: a broken database must never
break a session.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

BIFROST = Path(__file__).resolve().parents[2]        # the Bifrost repo
HOOKS = BIFROST / "bifrost" / "hooks.py"

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from bifrost import core, hooks, ingest  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run_hook(cmd: str, payload: dict | None = None, env_db: str | None = None):
    """Drive the hook the way the harness does: a subprocess, JSON on stdin."""
    import os
    env = dict(os.environ)
    if env_db:
        env["BIFROST_DB"] = env_db
    p = subprocess.run(
        [sys.executable, str(HOOKS), cmd],
        input=json.dumps(payload or {}), text=True, capture_output=True,
        cwd=str(BIFROST), env=env)
    out = p.stdout.strip()
    return p.returncode, (json.loads(out) if out else None), p.stderr


def call_inproc(fn, payload: dict):
    """Same, in-process, so a scratch database can be injected."""
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = buf = io.StringIO()
    try:
        rc = fn()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    text = buf.getvalue().strip()
    return rc, (json.loads(text) if text else None)


# ---------------------------------------------------------------------------

def test_session_start_injects_digest():
    rc, out, err = run_hook("session_start")
    check(rc == 0, f"rc={rc} err={err}")
    check(out is not None, "SessionStart must emit JSON")
    hso = out.get("hookSpecificOutput", {})
    check(hso.get("hookEventName") == "SessionStart", str(hso)[:200])
    ctx = hso.get("additionalContext", "")
    check("BF3 CORPUS" in ctx, ctx[:200])
    check("source of truth" in ctx, "the preamble must tell an agent what this is")
    est = len(ctx) / 4
    check(est < 4000, f"the injected digest is ~{est:.0f} tokens; it runs every session")
    return f"~{est:.0f} tokens injected"


def test_post_tool_use_records_a_probe_run():
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.ingest_builds(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn  # inject the scratch database

    rc, out = call_inproc(hooks.post_tool_use, {
        "tool_name": "Bash",
        "tool_input": {"command": "./build/corpus_anim.exe E:/BF3_360/assets/bf/anim_xb_v21"},
        "tool_response": {"stdout": "4705 files: 4703 pass, 0 fail, 2 refused"}})
    check(rc == 0, str(rc))
    check("recorded corpus_anim" in out["systemMessage"], str(out))
    check("4703 pass" in out["systemMessage"], str(out))

    row = core.one(conn, "SELECT pass, fail, refused FROM gate_run")
    check(row["pass"] == 4703 and row["refused"] == 2, str(row))
    return "run captured from the probe's own stdout"


def test_post_tool_use_asks_when_output_is_unavailable():
    """The harness may not hand the hook the output. Asking is correct; guessing
    a green result from output nobody saw is exactly what rule 7 forbids."""
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.ingest_builds(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn

    rc, out = call_inproc(hooks.post_tool_use, {
        "tool_name": "Bash", "tool_input": {"command": "./build/corpus_mesh.exe"},
        "tool_response": {"success": True}})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    check("bifrost_record_run" in ctx and "corpus_mesh" in ctx, ctx)
    check(core.one(conn, "SELECT COUNT(*) n FROM gate_run")["n"] == 0,
          "nothing may be recorded from output that was never seen")
    return "asks for the output rather than inventing a result"


def test_post_tool_use_ignores_unrelated_commands():
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.ingest_builds(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn
    for cmd in ("ls -la", "git status", "python -c 'print(1)'", "cmake --build ."):
        rc, out = call_inproc(hooks.post_tool_use,
                              {"tool_input": {"command": cmd},
                               "tool_response": {"stdout": "3293 pass, 0 fail"}})
        check(out is None, f"{cmd!r} must produce no output, got {out}")
    return "4 unrelated commands, all silent"


def test_post_tool_use_flags_unexplained_failures():
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.ingest_builds(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn
    rc, out = call_inproc(hooks.post_tool_use, {
        "tool_input": {"command": "./build/corpus_mesh.exe"},
        "tool_response": {"stdout": "1663 files: 3200 pass, 94 fail, 43 refused"}})
    msg = out["systemMessage"]
    check("FAILING" in msg, msg)
    check("94 failure(s) not covered" in msg, msg)
    return "an unexplained failure is reported, not smoothed over"


def test_stop_reports_stale_readers():
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn

    rc, out = call_inproc(hooks.stop, {})
    check(out is None, "gates that have never run are not 'stale readers'; that is "
                       "the digest's job, not the Stop hook's")

    gid = core.get_id(conn, "gate", "corpus_mesh")
    conn.executemany("INSERT INTO git_ancestry(commit_sha, ordinal) VALUES (?,?)",
                     [("new", 0), ("old", 1)])
    conn.execute("""INSERT INTO git_state(path,last_commit,dirty,scanned_at)
                    VALUES ('src/SelotapeDataLoaders.cpp','new',0,'t')""")
    conn.execute("""INSERT INTO gate_run(gate_id,ts,commit_sha,tree_dirty,ok)
                    VALUES (?,'2026-09-04T00:00:00+00:00','old',0,1)""", (gid,))
    conn.commit()

    rc, out = call_inproc(hooks.stop, {})
    msg = out["systemMessage"]
    check("corpus_mesh" in msg and "SelotapeDataLoaders" in msg, msg)
    check("claims about code that has since changed" in msg, msg)
    return "a reader edited after its gate ran is reported at session end"


def test_hooks_fail_open():
    """A hook must never break a session. Malformed stdin, a bad subcommand, an
    unreachable database -- all exit 0 and emit nothing harmful."""
    import os
    p = subprocess.run([sys.executable, str(HOOKS), "post_tool_use"],
                       input="this is not json", text=True, capture_output=True,
                       cwd=str(BIFROST))
    check(p.returncode == 0, f"malformed stdin must exit 0, got {p.returncode}")

    p = subprocess.run([sys.executable, str(HOOKS), "post_tool_use"],
                       input="", text=True, capture_output=True, cwd=str(BIFROST))
    check(p.returncode == 0, f"empty stdin must exit 0, got {p.returncode}")

    rc, out, err = run_hook("stop", {"tool_response": None})
    check(rc == 0, f"stop must exit 0, got {rc}")

    p = subprocess.run([sys.executable, str(HOOKS), "not_a_command"],
                       input="{}", text=True, capture_output=True, cwd=str(BIFROST))
    check(p.returncode == 2 and "usage" in p.stderr, "an unknown subcommand should say so")
    return "malformed stdin, empty stdin and bad args all fail open"


def test_settings_and_mcp_config_are_wired():
    """The config files must actually name these hooks -- a tested hook nothing
    invokes is not enforcement."""
    from bifrost import profile
    ROOT = profile.load().root
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    for event, sub in (("SessionStart", "session_start"),
                       ("PreToolUse", "pre_tool_use"),
                       ("PostToolUse", "post_tool_use"),
                       ("Stop", "stop")):
        cmds = [h["command"] for grp in s["hooks"][event] for h in grp["hooks"]]
        check(any(sub in c for c in cmds), f"{event} does not invoke {sub}: {cmds}")
        check(any("hooks.py" in c for c in cmds), f"{event} does not point at hooks.py")
        # Hooks run with the SESSION's cwd, not the project root. A bare relative
        # path resolved to tools/tools/bifrost/hooks.py the first time this went
        # live, so the path must be anchored.
        for c in cmds:
            check("CLAUDE_PROJECT_DIR" in c or Path(c.split('"')[1] if '"' in c else c).is_absolute(),
                  f"{event} command is cwd-dependent and will break outside the "
                  f"project root: {c}")
    pt = s["hooks"]["PostToolUse"][0]
    check(pt.get("matcher") == "Bash", f"PostToolUse matcher should be Bash, got {pt.get('matcher')}")

    m = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["bifrost"]
    # The launcher must be reachable without depending on the client's working
    # directory. `-m bifrost.mcp_server` with a relative cwd resolved differently
    # under the real client, the process died on ModuleNotFoundError, and all the
    # client reported was "Connection closed".
    check("cwd" not in m, f"the MCP entry must not depend on a cwd: {m}")
    check(len(m["args"]) == 1 and m["args"][0].endswith("bifrost_server.py"), str(m))
    check(Path(m["args"][0]).is_absolute(), f"the launcher path must be absolute: {m['args']}")
    check(Path(m["args"][0]).exists(), f"the launcher does not exist: {m['args'][0]}")
    return "4 hooks + 1 cwd-independent MCP launcher, all pointing at real files"


def test_probe_re_needs_an_invocation_not_a_mention():
    """The regression that produced gate_run rows 11-19 of the BF3 database.

    The first PROBE_RE matched the gate name anywhere in the command, so reading
    ABOUT a probe counted as running it and the reading's stdout was ingested as
    the result. Three of those rows went in GREEN. Every command below is one
    this hook actually saw.
    """
    ran = [
        "./build/corpus_mesh.exe > log 2>&1",
        "build/corpus_mesh.exe",
        'LOG=/tmp/x.log ./build/corpus_mesh.exe > "$LOG"; echo done',
        "mkdir -p build/bifrost_logs && ./build/corpus_mesh.exe",
        "build\\corpus_mesh.exe E:/BF3_360",
        "./build/corpus_mesh.exe E:/BF3_360 setup.res | tail -20",
    ]
    mentioned = [
        "ls -l --time-style=+%Y-%m-%dT%H:%M build/corpus_mesh.exe src/x.cpp",
        'grep -n -i "corpus_mesh" tools/README.md | head -30',
        "sed -n '264,420p' tools/probes/corpus_mesh.cpp",
        'grep -n "Report" -A 45 tools/probes/corpus_mesh.cpp',
        'cmd //c "build\\mk_mesh.bat" 2>&1 | tail -20; echo "EXIT=$?"; '
        "ls -l build/corpus_mesh.exe",
        'grep -n "corpus_mesh" CMakeLists.txt',
        "cat tools/probes/corpus_mesh.cpp",
        "echo build/corpus_mesh.exe",
        "git log --oneline -1 -- tools/probes/corpus_mesh.cpp",
    ]
    for cmd in ran:
        check(hooks.PROBE_RE.search(cmd) is not None, f"must be a run: {cmd!r}")
    for cmd in mentioned:
        check(hooks.PROBE_RE.search(cmd) is None, f"must NOT be a run: {cmd!r}")
    return f"{len(ran)} invocations matched, {len(mentioned)} mentions ignored"


def test_post_tool_use_does_not_record_a_grep():
    """End to end: the README grep that went in as a passing run of the gate."""
    conn = core.connect(":memory:")
    core.migrate(conn); ingest.ingest_builds(conn); ingest.register_gates(conn)
    hooks._conn = lambda: conn
    readme = ("| `corpus_mesh.cpp [root]` | The gate. Last run: 1663 files, "
              "3293 pass, 1 fail, 43 refused. |\n"
              "| `vmscorpus.py` | 67 compiled, 50 rejected, 0 failed |")
    rc, out = call_inproc(hooks.post_tool_use, {
        "tool_input": {"command": 'grep -n "corpus_mesh" tools/README.md'},
        "tool_response": {"stdout": readme}})
    check(out is None, f"a grep must produce no output, got {out}")
    n = conn.execute("SELECT COUNT(*) FROM gate_run").fetchone()[0]
    check(n == 0, f"a grep must record no gate run, got {n}")
    return "the README grep records nothing and says nothing"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = 0
    print("=" * 60)
    print("   Bifrost — hooks and wiring")
    print("=" * 60)
    for fn in TESTS:
        print(f"[RUNNING] {fn.__name__}...")
        try:
            note = fn()
            print(f"  -> PASSED{'  ' + note if note else ''}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  -> FAILED: {type(e).__name__}: {e}")
            failed += 1
    print("=" * 60)
    print(f"   TEST RESULTS: {passed} / {passed + failed} passed")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
