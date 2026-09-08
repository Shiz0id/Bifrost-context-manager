"""Bifrost ingest — everything that fills the database without anyone typing.

The drift surface of this whole system is the set of rows a human or an agent has
to remember to update. Everything in this module exists to keep that set small:
trees come from a filesystem scan, gate results from parsing a probe's own stdout,
symbols from the shipped linker maps, staleness from git. All of it is re-runnable
and self-healing, so a stale row is a bug in the scanner rather than a lapse in
discipline.

What is deliberately NOT here: claims, discriminators, todos. Those are judgements
and they are written through core.record_* / core.propose.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import core
from .core import BifrostError, utcnow

from . import profile

# ---------------------------------------------------------------------------
# builds
# ---------------------------------------------------------------------------

# BUILDS now comes from the project profile; see profile.py.
def _builds():
    return profile.load().BUILDS


def wii_map() -> Path | None:
    p = getattr(profile.load()._m, 'WII_MAP', None) if profile.load().loaded else None
    return Path(p) if p else None


def ingest_builds(conn: sqlite3.Connection) -> int:
    for b in _builds():
        conn.execute(
            """INSERT INTO build(name, platform, root_path, exe_path, image_base,
                                 endianness, symbol_origin)
               VALUES (:name,:platform,:root_path,:exe_path,:image_base,
                       :endianness,:symbol_origin)
               ON CONFLICT(name) DO UPDATE SET
                 root_path=excluded.root_path, exe_path=excluded.exe_path,
                 image_base=excluded.image_base, symbol_origin=excluded.symbol_origin""",
            b,
        )
    conn.commit()
    return len(_builds())


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------

# "80004000 00000050 memcpy" -- addr, size, name. Zero-size entries are the
# CodeWarrior map's data/thunk rows and are kept: they still name an address.
_MAP_LINE = re.compile(r"^([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s+(\S.*?)\s*$")


def ingest_wii_symbols(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """Load RABAZZ_full.map into wii_symbol.

    83 of the 277 distinct addresses the roadmap cites are Broadway-range, and
    until this runs every one of them cites a bare number with no name attached.
    """
    p = Path(path) if path else wii_map()
    if p is None:
        raise BifrostError('this project declares no WII_MAP in its profile')
    if not p.exists():
        raise BifrostError(f"no Wii linker map at {p}")

    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _MAP_LINE.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        if not (0x80000000 <= addr < 0x81000000):
            continue
        rows.append((addr, m.group(3), p.name))

    conn.executemany(
        "INSERT OR REPLACE INTO wii_symbol(addr, symbol, origin) VALUES (?,?,?)", rows
    )
    conn.commit()
    return len(rows)


def backfill_source_symbols(conn: sqlite3.Connection) -> dict:
    """Re-resolve every address-bearing source row.

    Run after ingesting a symbol map: rows recorded before the map existed are
    sitting there unresolved, and nothing else would ever revisit them.
    """
    core.attach_symbols(conn)
    stats = {"exact": 0, "inside": 0, "unresolved": 0}
    for row in core.rows(conn,
                         "SELECT s.id, s.locator, s.build_id, b.name AS build "
                         "FROM source s LEFT JOIN build b ON b.id=s.build_id "
                         "WHERE s.kind IN ('disasm_fn','data_addr','map_symbol')"):
        if not re.fullmatch(r"0x[0-9A-Fa-f]+", row["locator"] or ""):
            continue
        res = core.resolve_address(conn, int(row["locator"], 16), row["build"] or "bf3_360")
        stats[res.kind] += 1
        conn.execute(
            "UPDATE source SET symbol=?, containing_symbol=?, offset_in_symbol=? WHERE id=?",
            (res.symbol, res.containing, res.offset, row["id"]),
        )
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# trees
# ---------------------------------------------------------------------------

def _scan_dir(root: Path) -> tuple[int, int, dict]:
    """Walk one asset tree. os.scandir rather than glob: on Windows the stat
    comes off the directory entry, which is the difference between seconds and
    minutes on a 31,102-file tree."""
    files = 0
    total = 0
    hist: dict[str, int] = {}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            files += 1
                            total += e.stat().st_size
                            ext = os.path.splitext(e.name)[1].lower() or "(none)"
                            hist[ext] = hist.get(ext, 0) + 1
                    except OSError:
                        continue
        except OSError:
            continue
    return files, total, hist


def scan_trees(conn: sqlite3.Connection, build_name: str,
               only: Iterable[str] | None = None) -> list[dict]:
    """Inventory every asset tree of a build.

    This is what makes the surface map a query instead of an afternoon. It is
    also what would have caught roadmap section 3 omitting atlas_xb_v8,
    cloud_xb_v1 and dgeom_xb_v7 -- three trees that are on the disc and were not
    in the document.
    """
    b = core.one(conn, "SELECT * FROM build WHERE name=?", (build_name,))
    if not b:
        raise BifrostError(f"unknown build {build_name!r}; run ingest_builds first")

    assets = Path(b["root_path"]) / "assets" / "bf"
    if not assets.exists():
        raise BifrostError(f"no asset root at {assets}")

    wanted = set(only) if only else None
    out = []
    for entry in sorted(assets.iterdir()):
        if not entry.is_dir():
            continue
        if wanted and entry.name not in wanted:
            continue
        files, total, hist = _scan_dir(entry)
        # ob_wi_v194 -> 194; particle_v0 -> 0. The version is also in the retail
        # asset-type table at 0x82A80FE0, which is the authority; this is the
        # directory's own claim about itself.
        m = re.search(r"_v(\d+)$", entry.name)
        version = int(m.group(1)) if m else None
        conn.execute(
            """INSERT INTO tree(build_id, name, path, version, file_count, bytes,
                                ext_histogram, scanned_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(build_id, name) DO UPDATE SET
                 path=excluded.path, version=excluded.version,
                 file_count=excluded.file_count, bytes=excluded.bytes,
                 ext_histogram=excluded.ext_histogram, scanned_at=excluded.scanned_at""",
            (b["id"], entry.name, str(entry).replace("\\", "/"), version, files, total,
             json.dumps(hist, sort_keys=True), utcnow()),
        )
        out.append(dict(name=entry.name, files=files, bytes=total, version=version))
    conn.commit()
    return out


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------

# GATES now comes from the project profile; see profile.py.
def _gates():
    return profile.load().GATES



def register_gates(conn: sqlite3.Connection) -> int:
    for g in _gates():
        conn.execute(
            """INSERT INTO gate(name, probe_path, exe_path, description, trees, readers)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 probe_path=excluded.probe_path, exe_path=excluded.exe_path,
                 description=excluded.description, trees=excluded.trees,
                 readers=excluded.readers""",
            (g["name"], g["probe_path"], g["exe_path"], g["description"],
             json.dumps(g["trees"]), json.dumps(g["readers"])),
        )
    conn.commit()
    return len(_gates())


# ---------------------------------------------------------------------------
# gate stdout parsing
# ---------------------------------------------------------------------------

# A probe SAYS how it went, in a block it wrote on purpose:
#
#     BIFROST-RESULT-BEGIN
#     {"pass": 1387, "fail": 0, "metrics": {"files": 1387}}
#     BIFROST-RESULT-END
#
# or, equivalently, KEY=value lines inside the same markers, where pass, fail
# and refused are reserved and every other key is a metric.
#
# This exists because the alternative was inference, and inference at this
# boundary is the exact thing AGENTS.md rule 1 forbids everywhere else: never
# infer a format, read the thing that writes it. Bifrost was scraping
# "<number> <nearby word>" out of prose and storing the result as structured
# data, which produced metrics like `and_all_eight: 90` and, from a paragraph
# explaining that an earlier run was a mis-parse, `is_a_recording_artefact: 32`.
# The system built to stop guessing was guessing at its own front door.
_BLOCK = re.compile(
    r"^[ \t]*BIFROST-RESULT-BEGIN[ \t]*$\n(.*?)^[ \t]*BIFROST-RESULT-END[ \t]*$",
    re.M | re.S)
_KV = re.compile(r"^[ \t]*([A-Za-z][A-Za-z0-9_]*)[ \t]*=[ \t]*(-?[\d,]+)[ \t]*$", re.M)
_RESERVED = ("pass", "fail", "refused")

# Legacy summary lines, for probes that have not been given a block yet. These
# recover COUNTS ONLY -- never metrics. A number sitting near a word is not a
# measurement of anything, and treating it as one is what this change ends.
#
# The colon forms are tried first and win, because the bare "<n> <label>" form
# reads a summary backwards whenever the label follows a different number:
# `PARSED OK: 1387   FAILED: 0` bound 1387 to fail, flipped corpus_res red, and
# put a failing gate in front of every session that read the digest cold.
#
# `[ \t]+` rather than `\s+` on the bare forms, because \s crosses newlines:
# corpus_level ends its class-id histogram with a bare number on one line and a
# bare "PASS" on the next, which \s+ read as "59 pass".
_PASS_C = re.compile(r"\bpass(?:ed)?[ \t]*:[ \t]*(\d[\d,]*)", re.I)
_FAIL_C = re.compile(r"\bfail(?:ed|ures)?[ \t]*:[ \t]*(\d[\d,]*)", re.I)
_REFUSED_C = re.compile(r"\brefused[ \t]*:[ \t]*(\d[\d,]*)", re.I)
_PASS = re.compile(r"(\d[\d,]*)[ \t]+pass\b", re.I)
_FAIL = re.compile(r"(\d[\d,]*)[ \t]+fail(?:ed)?\b", re.I)
_REFUSED = re.compile(r"(\d[\d,]*)[ \t]+refused\b", re.I)
_WALK = re.compile(r"(\d[\d,]*)[ \t]+walk\b", re.I)
_PARSEFAIL = re.compile(r"(\d[\d,]*)[ \t]+failed to parse", re.I)

# A verdict has to START a line. `"[PASS]" in text` also matches the literal
# inside the probe's OWN SOURCE -- `std::cout << (pass ? "[PASS]" : "[FAIL]")`
# -- which is how a `sed` of corpus_matattr.cpp came to be recorded as a green
# run of corpus_matattr. A bare PASS/FAIL must be the whole line; a bracketed
# one may carry its counts after it, which is how the probes actually print.
_VERDICT_PASS = re.compile(r"^[ \t]*(?:\[PASS\]|PASS[ \t]*$)", re.M)
_VERDICT_FAIL = re.compile(r"^[ \t]*(?:\[FAIL\]|FAIL[ \t]*$)", re.M)


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def parse_declared_block(text: str) -> dict | None:
    """Read a BIFROST-RESULT block, or return None if the probe emitted none.

    Raises rather than falling back when a block is present but malformed. A
    probe that meant to declare its result and got the syntax wrong must not be
    quietly re-read by the guesser it was written to replace -- that would put
    the inference back exactly where it does the most damage, on output somebody
    believed was structured.
    """
    blocks = _BLOCK.findall(text)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise BifrostError(
            f"{len(blocks)} BIFROST-RESULT blocks in one log; a run has one result. "
            f"If several probes ran, ingest their outputs separately.")

    body = blocks[0].strip()
    got: dict = {"pass": None, "fail": None, "refused": None, "metrics": {}}

    if body.startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise BifrostError(f"BIFROST-RESULT block is not valid JSON: {e}")
        if not isinstance(data, dict):
            raise BifrostError("BIFROST-RESULT JSON must be an object")
        for k in _RESERVED:
            if data.get(k) is not None:
                if not isinstance(data[k], int) or isinstance(data[k], bool):
                    raise BifrostError(f"BIFROST-RESULT {k!r} must be an integer")
                got[k] = data[k]
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            raise BifrostError("BIFROST-RESULT 'metrics' must be an object")
        got["metrics"] = metrics
    else:
        pairs = _KV.findall(body)
        if not pairs:
            raise BifrostError(
                "BIFROST-RESULT block is neither JSON nor KEY=value lines. "
                "Emit {\"pass\": N, \"fail\": N, \"metrics\": {...}} or pass=N / fail=N.")
        for k, v in pairs:
            (got if k.lower() in _RESERVED else got["metrics"])[
                k.lower() if k.lower() in _RESERVED else k] = _num(v)

    if got["pass"] is None and got["fail"] is None:
        raise BifrostError("BIFROST-RESULT block declares neither pass nor fail")
    return got


def parse_gate_stdout(name: str, text: str) -> dict:
    """Extract pass/fail/refused and a metrics bag from a probe's own output.

    A declared BIFROST-RESULT block is authoritative and is the only source of
    metrics. Without one, the legacy summary regexes recover COUNTS ONLY, and
    the run carries no metrics at all -- which is the honest result, because
    nothing in free text was ever a declared measurement.

    Deliberately tolerant about verdicts: a gate whose summary cannot be parsed
    records ok=0 with a note rather than silently reporting a pass. Reporting a
    green result for output nobody understood is exactly the failure AGENTS.md
    rule 7 is about.
    """
    out: dict = {"pass": None, "fail": None, "refused": None, "metrics": {},
                 "ok": 0, "recognised": True, "declared": False}

    if (declared := parse_declared_block(text)) is not None:
        out.update(declared)
        out["declared"] = True
        explicit_fail = bool(_VERDICT_FAIL.search(text))
        out["ok"] = 0 if (explicit_fail or (out["fail"] or 0) > 0) else 1
        return out

    tail = "\n".join(text.strip().splitlines()[-40:])

    # Tail first, whole text as a fallback. corpus_level prints its summary block
    # ABOVE a 50-line class-id histogram, so a tail-only search finds nothing --
    # which would have recorded a run with no counts at all.
    def find(rx):
        return rx.search(tail) or rx.search(text)

    # Strongest signal available without a declared block: ONE line carrying
    # both a pass and a fail count. Those numbers were printed together and
    # belong together, which no cross-line search can establish.
    summary = next((ln for ln in tail.splitlines() + text.splitlines()
                    if _PASS.search(ln) and _FAIL.search(ln)), None)
    if summary:
        out["pass"] = _num(_PASS.search(summary).group(1))
        out["fail"] = _num(_FAIL.search(summary).group(1))
        if (m := _REFUSED.search(summary)):
            out["refused"] = _num(m.group(1))
    else:
        # No summary line. Now the colon forms are worth more than the bare
        # ones, because `FAILED: 0` says which number is the failure count while
        # `1387   FAILED` merely sits next to one -- that adjacency is what read
        # corpus_res backwards and turned a clean gate red.
        #
        # The reverse is also true, which is why this ordering is conditional
        # and not global: corpus_wii_ob prints `failures: 0 bone past the
        # skeleton, 4 position outside its AABB`, where the colon introduces a
        # BREAKDOWN and the real total is the 4 on its summary line. Every
        # ordering here has a counterexample somewhere -- which is the argument
        # for the declared block, not for a cleverer regex.
        if (m := find(_PASS_C)) or (m := find(_PASS)) or (m := find(_WALK)):
            out["pass"] = _num(m.group(1))
        if (m := find(_FAIL_C)) or (m := find(_FAIL)) or (m := find(_PARSEFAIL)):
            out["fail"] = _num(m.group(1))

    if out["refused"] is None:
        if (m := find(_REFUSED_C)) or (m := find(_REFUSED)):
            out["refused"] = _num(m.group(1))

    # explicit verdicts win over inference
    explicit_fail = bool(_VERDICT_FAIL.search(text))
    explicit_pass = bool(_VERDICT_PASS.search(text))

    if explicit_fail:
        out["ok"] = 0
    elif explicit_pass:
        out["ok"] = 1
    elif out["fail"] is not None:
        out["ok"] = 1 if out["fail"] == 0 else 0
    elif out["pass"]:
        out["ok"] = 1
    else:
        # Nothing here says how a run went, so this text is not a gate run at
        # all -- it is a grep, a traceback, or a log that was lost. Report that
        # as its own state rather than as a failure: ingest_gate_run refuses it,
        # because a red row invites someone to go looking for a defect that was
        # never there, and mining free text for metrics under those conditions
        # is how `read_the_material_sets: 7592` got into the database, parsed
        # out of the commit subject `3dc7592 Read the material sets, ...`.
        out["recognised"] = False
        out["metrics"]["parse_note"] = (
            "no BIFROST-RESULT block and no pass/fail summary in the last 40 lines")
        return out

    # No metrics are inferred here, by design. A probe that wants metrics
    # recorded declares them in a BIFROST-RESULT block; anything else is a
    # number that happened to sit next to a word.
    return out


def ingest_gate_run(conn: sqlite3.Connection, gate_name: str, stdout: str,
                    *, commit_sha: str | None = None, tree_dirty: bool | None = None,
                    stdout_dir: Path | None = None, note: str | None = None,
                    supersedes: int | None = None, finding: str | None = None,
                    todo_id: int | None = None) -> int:
    """Record one gate run.

    stdout is written to disk and only its head kept in-row: agents querying runs
    want the summary, and a 200-line dump per row would bloat every digest and
    every query result.

    `note` is for what a person concluded, kept out of stdout so that the probe's
    own bytes stay the probe's own bytes. `supersedes` retracts an earlier run
    that was a recording error rather than a result -- see migration 008.

    `finding` is the third state of a run: it PASSED and it FOUND SOMETHING. The
    digest renders it, because `ok` alone reads as an unqualified success and
    the digest is trusted precisely because people skip reading anything else.
    `todo_id` attaches the run to the open work it bears on.
    """
    gid = core.get_id(conn, "gate", gate_name)
    if gid is None:
        raise BifrostError(f"unknown gate {gate_name!r}; run register_gates first")

    if todo_id is not None and core.one(conn, "SELECT id FROM todo WHERE id=?", (todo_id,)) is None:
        raise BifrostError(f"no todo #{todo_id} to attach this run to")

    if supersedes is not None:
        prior = core.one(conn, "SELECT gate_id FROM gate_run WHERE id=?", (supersedes,))
        if prior is None:
            raise BifrostError(f"no gate_run #{supersedes} to supersede")
        if prior["gate_id"] != gid:
            raise BifrostError(
                f"gate_run #{supersedes} belongs to a different gate; a run can only "
                f"supersede another run of {gate_name!r}")
        already = core.one(conn, "SELECT id FROM gate_run WHERE supersedes=?", (supersedes,))
        if already:
            raise BifrostError(
                f"gate_run #{supersedes} is already superseded by #{already['id']}")

    g = core.one(conn, "SELECT readers FROM gate WHERE id=?", (gid,))
    readers = json.loads(g["readers"] or "[]")

    if commit_sha is None:
        commit_sha = core.head_commit()
    if tree_dirty is None:
        tree_dirty = core.tree_dirty(readers) if readers else core.tree_dirty()

    parsed = parse_gate_stdout(gate_name, stdout)

    # Refuse rather than record. A run row is a claim that this gate was run and
    # this is how it went; text with no verdict in it supports neither half, and
    # recording it as a failure spends someone's afternoon on a defect that does
    # not exist. gate_run rows 11-19 of the BF3 database are what this prevents:
    # greps, an `ls -l` and a README, ingested because a hook matched a gate name
    # in a command that only mentioned it. They have since been deleted.
    if not parsed["recognised"]:
        first = (stdout.strip().splitlines() or ["<empty>"])[0][:100]
        raise BifrostError(
            f"{gate_name}: this output carries no pass/fail verdict, so it is not a "
            f"gate run and has not been recorded. If the probe did run, its output "
            f"was lost -- re-run it and record that. First line: {first!r}")

    # AGENTS.md rule 4 asks for pass/fail counts with every failure JUSTIFIED --
    # not for zero failures. corpus_mesh has reported "1 fail" since the krayt
    # outlier was found, and that file is a named, accepted exception; a gate
    # that went red forever because of it would train everyone to ignore red.
    #
    # So the parser reports the raw verdict and the database, which is the only
    # thing that knows what has been justified, decides. An UNEXPLAINED failure
    # still fails.
    # SUM(expected_failures), not COUNT(*): one exception can cover many files.
    # corpus_wii_ob's four failures are all the single "Wii cloth parts are
    # simulated" exception, and counting rows left two of them unexplained.
    accepted = conn.execute(
        "SELECT COALESCE(SUM(expected_failures),0) FROM exception_gate WHERE gate_id=?", (gid,)
    ).fetchone()[0]
    fails = parsed["fail"] or 0
    unexplained = max(0, fails - accepted)
    if parsed["fail"] is not None:
        parsed["ok"] = 1 if unexplained == 0 else 0
        parsed["metrics"]["accepted_exceptions"] = accepted
        parsed["metrics"]["unexplained_failures"] = unexplained

    stdout_path = None
    if stdout_dir:
        stdout_dir = Path(stdout_dir)
        stdout_dir.mkdir(parents=True, exist_ok=True)
        ts = utcnow().replace(":", "").replace("-", "")
        p = stdout_dir / f"{gate_name}_{ts}.log"
        p.write_text(stdout, encoding="utf-8")
        stdout_path = str(p).replace("\\", "/")

    head = "\n".join(stdout.strip().splitlines()[-12:])[:2000]

    cur = conn.execute(
        """INSERT INTO gate_run(gate_id, ts, commit_sha, tree_dirty, ok, pass, fail,
                                refused, metrics, stdout_path, stdout_head,
                                note, supersedes, finding, todo_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (gid, utcnow(), commit_sha, 1 if tree_dirty else 0, parsed["ok"],
         parsed["pass"], parsed["fail"], parsed["refused"],
         json.dumps(parsed["metrics"], sort_keys=True), stdout_path, head,
         note, supersedes, finding, todo_id),
    )
    conn.commit()

    # keep staleness answerable for the readers this gate touches
    if readers:
        core.refresh_git_state(conn, readers)
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# one-shot bootstrap
# ---------------------------------------------------------------------------

def bootstrap(conn: sqlite3.Connection, *, scan: bool = True,
              verbose: bool = True) -> dict:
    """Wave 1: everything that needs no judgement.

    Order matters. Symbols come before anything that cites an address, because a
    citation recorded against an unloaded map resolves to nothing and nothing
    would revisit it.
    """
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats: dict = {}

    core.migrate(conn)
    stats["builds"] = ingest_builds(conn)
    log(f"[SUCCESS] builds: {stats['builds']}")

    stats["symbols_360"] = core.attach_symbols(conn)
    log(f"[{'SUCCESS' if stats['symbols_360'] else 'ERROR'}] 360 symbol database "
        f"{'attached' if stats['symbols_360'] else 'NOT FOUND at ' + str(core.SYMBOLS_360)}")

    try:
        stats["symbols_wii"] = ingest_wii_symbols(conn)
        log(f"[SUCCESS] Wii symbols: {stats['symbols_wii']} from {wii_map().name}")
    except BifrostError as e:
        stats["symbols_wii"] = 0
        log(f"[ERROR] {e}")

    stats["gates"] = register_gates(conn)
    log(f"[SUCCESS] gates registered: {stats['gates']}")

    all_readers = sorted({r for g in _gates() for r in g["readers"]})
    stats["git_state"] = core.refresh_git_state(conn, all_readers)
    stats["git_ancestry"] = core.refresh_git_ancestry(conn)
    log(f"[SUCCESS] git: {stats['git_state']} reader paths, "
        f"{stats['git_ancestry']} commits of ancestry")

    if scan:
        for b in ("bf3_360", "bf3_wii"):
            try:
                trees = scan_trees(conn, b)
                stats[f"trees_{b}"] = len(trees)
                total = sum(t["files"] for t in trees)
                log(f"[SUCCESS] {b}: {len(trees)} asset trees, {total:,} files")
            except BifrostError as e:
                stats[f"trees_{b}"] = 0
                log(f"[ERROR] {b}: {e}")

    return stats
