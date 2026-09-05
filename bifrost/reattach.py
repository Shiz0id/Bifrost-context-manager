"""Attach migrated claims to the format they are actually about.

The roadmap's section numbering is finer than the format list: sections 6.2
through 6.8, 6.10, 6.12 and 6.17 are all about `ob_xb_v184`, but only 6.2 named
it, so 128 of 218 claims landed on the generic `corpus` subject and
`bifrost realm ob_xb_v184` showed four claims instead of thirty.

The mapping below is by section, because the document's own structure is what
says which format a paragraph is about. Sections that are genuinely NOT about a
format -- the method essay, the toolchain, capture discipline -- stay on
`corpus`, which is the honest place for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import core, profile

# NEW_FORMATS: project data, from the profile.


# ANCHOR_MAP: project data, from the profile.



def run(conn, verbose: bool = True) -> dict:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats = {"formats": 0, "claims": 0, "passages": 0}

    prof = profile.load()
    for name, tree, anchor, summary in prof.NEW_FORMATS:
        if core.get_id(conn, "format", name) is None:
            conn.execute(
                "INSERT INTO format(name, roadmap_anchor, summary) VALUES (?,?,?)",
                (name, anchor, summary))
            stats["formats"] += 1
    conn.commit()

    for anchor, fmt in prof.ANCHOR_MAP.items():
        fid = core.get_id(conn, "format", fmt)
        if fid is None:
            log(f"[ERROR] anchor {anchor} maps to unknown format {fmt}")
            continue
        cur = conn.execute(
            """UPDATE claim SET subject_type='format', subject_id=?
               WHERE roadmap_anchor=? AND subject_type='corpus'""", (fid, anchor))
        stats["claims"] += cur.rowcount
        cur = conn.execute(
            """UPDATE passage SET subject_type='format', subject_id=?
               WHERE roadmap_anchor=? AND subject_type='corpus'""", (fid, anchor))
        stats["passages"] += cur.rowcount
    conn.commit()

    log(f"[SUCCESS] {stats['formats']} formats added, "
        f"{stats['claims']} claims and {stats['passages']} passages reattached")
    left = core.one(conn, "SELECT COUNT(*) n FROM claim WHERE subject_type='corpus'")["n"]
    log(f"[INFO] {left} claims remain on `corpus` -- method, toolchain and practice, "
        f"which are not about a format")
    return stats


if __name__ == "__main__":
    conn = core.connect()
    core.migrate(conn)
    core.attach_symbols(conn)
    run(conn)
