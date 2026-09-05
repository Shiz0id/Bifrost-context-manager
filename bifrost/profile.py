"""The project profile — what Bifrost is pointed at.

Bifrost is the machinery: a schema, derived status, provenance, gates, a
migration and a renderer. None of that knows anything about Star Wars
Battlefront III. What it needs from a project is DATA:

    ROOT            where the project lives
    BUILDS          the shipped builds and their symbol sources
    GATES           the corpus probes, what they assert, and which readers they
                    exercise -- the last is what makes staleness computable
    FORMATS         the formats being read, anchored to the document's sections
    CAPABILITIES    what the project is trying to be able to do, and what gates it
    EXCEPTIONS      named, justified non-passing files (rule 4)
    CLAIMS, TODOS   the seeded judgements
    ANCHOR_MAP      document section -> format
    INVARIANTS      what each gate asserts, and which claims that backs
    DISCRIMINATORS  the recorded checks that separate two readings

A project supplies those in `<root>/.bifrost/profile.py`. Everything is
optional: a profile that defines only ROOT and BUILDS gets a working database
with an empty knowledge layer.

Resolution order for the project root, first hit wins:

    1. an explicit path passed to load()
    2. $BIFROST_PROJECT
    3. $CLAUDE_PROJECT_DIR          (set by Claude Code)
    4. walking up from the working directory for a `.bifrost/` directory
    5. the working directory

The database lives under the PROJECT's build directory, not Bifrost's. It is
project state; Bifrost is only the thing that reads and writes it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Optional

MARKER = ".bifrost"

# Every name a profile may define, with the empty default used when it does not.
FIELDS: dict[str, Any] = {
    "NAME": "unnamed project",
    "BUILDS": [],
    "GATES": [],
    "FORMATS": [],
    "CAPABILITIES": [],
    "EXCEPTIONS": [],
    "CLAIMS": [],
    "TODOS": [],
    "NEW_FORMATS": [],
    "ANCHOR_MAP": {},
    "INVARIANTS": {},
    "DISCRIMINATORS": [],
    "DOCUMENT": None,          # the markdown rendered from the database
    "SYMBOL_DB": None,         # a prebuilt symbol database to ATTACH read-only
    "READER_PATHS": [],        # source files whose age makes a gate stale
}


class Profile:
    """A loaded project profile. Missing fields read as their empty default, so
    a caller never has to guard for absence."""

    def __init__(self, root: Path, module: Any = None, path: Path | None = None):
        self.root = root
        self.path = path
        self._m = module

    def __getattr__(self, name: str) -> Any:
        if name not in FIELDS:
            raise AttributeError(
                f"{name!r} is not a profile field; expected one of {sorted(FIELDS)}")
        return getattr(self._m, name, FIELDS[name]) if self._m else FIELDS[name]

    @property
    def loaded(self) -> bool:
        return self._m is not None

    @property
    def db_path(self) -> Path:
        return self.root / "build" / "bifrost.db"

    @property
    def document(self) -> Optional[Path]:
        return self.root / self.DOCUMENT if self.DOCUMENT else None

    def __repr__(self) -> str:
        return (f"<Profile {self.NAME!r} root={self.root} "
                f"{'loaded' if self.loaded else 'EMPTY'}>")


def find_root(start: Path | None = None) -> Path:
    """Locate the project root. See the module docstring for the order."""
    for env in ("BIFROST_PROJECT", "CLAUDE_PROJECT_DIR"):
        v = os.environ.get(env)
        if v and Path(v).is_dir():
            return Path(v).resolve()

    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / MARKER).is_dir():
            return d
    return here


_cached: Optional[Profile] = None


def load(root: Path | str | None = None, *, force: bool = False) -> Profile:
    """Load `<root>/.bifrost/profile.py`.

    A project with no profile is not an error -- Bifrost still runs, with an
    empty knowledge layer. That is what a brand-new project looks like.
    """
    global _cached
    if _cached is not None and not force and root is None:
        return _cached

    r = Path(root).resolve() if root else find_root()
    p = r / MARKER / "profile.py"
    if not p.exists():
        prof = Profile(r)
    else:
        spec = importlib.util.spec_from_file_location("bifrost_project_profile", p)
        mod = importlib.util.module_from_spec(spec)
        # The profile may import helpers from its own directory.
        sys.path.insert(0, str(p.parent))
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        prof = Profile(r, mod, p)

    if root is None:
        _cached = prof
    return prof
