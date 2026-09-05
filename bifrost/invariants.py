"""Gate invariants — what each probe actually asserts, and which claims that backs.

`gate.trees` links a gate to a whole asset tree, which is too coarse to answer
"is THIS claim corpus-verified?" and useless for the gates that are not
tree-scoped at all (corpus_level, corpus_res, corpus_scenedesc). A
`gate_invariant` links one specific check to one specific claim, which is what
rule 4 actually asks for.

The matching below is deliberately conservative. An invariant is linked to a
claim only when the check would FAIL if the claim were wrong. corpus_mesh
walking every position through CObinstLoader does not verify that obScale is not
a vertex multiplier -- it verifies that positions land inside their AABBs, and it
happens to be true that a wrong scale would break that, which is why THAT link is
made and a link to, say, the material-descriptor layout is not.

Over-linking would be worse than not linking at all: it would turn rule 4 from a
real gate into a decoration, which is precisely the failure mode AGENTS.md rule 7
is about.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import core, profile

# INVARIANTS: project data, from the profile.



def run(conn, verbose: bool = True) -> dict:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    stats = {"invariants": 0, "linked": 0, "unmatched": []}

    inv = profile.load().INVARIANTS
    for gate_name, entries in inv.items():
        gid = core.get_id(conn, "gate", gate_name)
        if gid is None:
            log(f"[ERROR] unknown gate {gate_name}")
            continue
        for assertion, patterns in entries:
            for pat in patterns:
                rows = core.rows(
                    conn, "SELECT id FROM claim WHERE statement LIKE ?", (f"%{pat}%",))
                if not rows:
                    stats["unmatched"].append((gate_name, pat))
                    continue
                for r in rows:
                    if core.one(conn, """SELECT id FROM gate_invariant
                                         WHERE gate_id=? AND claim_id=? AND assertion=?""",
                                (gid, r["id"], assertion)):
                        continue
                    conn.execute(
                        """INSERT INTO gate_invariant(gate_id, claim_id, assertion)
                           VALUES (?,?,?)""", (gid, r["id"], assertion))
                    stats["linked"] += 1
            stats["invariants"] += 1
    conn.commit()

    log(f"[SUCCESS] {stats['invariants']} invariants across {len(inv)} gates, "
        f"{stats['linked']} claim links")
    if stats["unmatched"]:
        log(f"[ERROR] {len(stats['unmatched'])} patterns matched no claim -- a pattern "
            f"that matches nothing is a silently missing link, not a harmless one:")
        for g, p in stats["unmatched"]:
            log(f"    {g}: {p!r}")
    return stats


if __name__ == "__main__":
    conn = core.connect()
    core.migrate(conn)
    core.attach_symbols(conn)
    run(conn)
