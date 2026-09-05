"""Recorded discriminators — the checks that separate two candidate readings.

The project's central epistemic move, and the one thing a corpus gate cannot
supply on its own: a gate says "this reading is self-consistent across the tree",
a discriminator says "this reading and not that one".

Every entry here was already stated in the roadmap's own prose as a measurement
against a control. They were flagged `asserted verified from a measurement alone`
not because the claims are weak but because the CONTROL half was never recorded —
so the flag was right about the row and wrong about the claim, and the fix is a
discriminator rather than a looser rule.

Idempotent: a claim that already carries one is skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import core, profile

# DISCRIMINATORS: project data, from the profile.



def run(conn, verbose: bool = True) -> dict:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    made = skipped = missing = 0

    for head, check, ra, rb, sep in profile.load().DISCRIMINATORS:
        row = core.one(conn, "SELECT id FROM claim WHERE statement LIKE ?", (head + "%",))
        if not row:
            log(f"[ERROR] no claim starting {head!r}")
            missing += 1
            continue
        if core.one(conn, "SELECT id FROM discriminator WHERE claim_a=?", (row["id"],)):
            skipped += 1
            continue
        core.record_discriminator(conn, claim_a=row["id"], claim_b=None,
                                  check_desc=check, result_a=ra, result_b=rb,
                                  separation=sep)
        made += 1

    log(f"[SUCCESS] {made} discriminators recorded, {skipped} already present"
        + (f", {missing} matched no claim" if missing else ""))
    return {"made": made, "skipped": skipped, "missing": missing}


if __name__ == "__main__":
    conn = core.connect()
    core.migrate(conn)
    core.attach_symbols(conn)
    run(conn)
