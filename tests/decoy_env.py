"""Write a decoy ``.env`` carrying every key ``.env.example`` documents.

The suite is supposed to be independent of the developer's real ``.env``: a
value sitting there used to change test outcomes, and the operator who sets one
-- the feature this branch adds, for one -- was exactly the one who saw the
suite go red. CI has no ``.env``, so CI never noticed.

Running the suite a second time with this file in place makes CI look like a
configured machine. Keys are derived from the template rather than listed here:
a hand-written list goes stale the day someone adds a key, and "the list no
longer matches reality" is the failure mode this branch kept rediscovering.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE = REPO_ROOT / ".env.example"

# ``KEY=`` or ``# KEY=`` at the start of a line -- the template comments out the
# optional ones, and those matter just as much.
_KEY = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")


def documented_keys(template: Path = TEMPLATE) -> list[str]:
    """Return every key name the template mentions, in order, deduplicated."""
    keys: list[str] = []
    for line in template.read_text(encoding="utf-8").splitlines():
        match = _KEY.match(line)
        if match and match.group(1) not in keys:
            keys.append(match.group(1))
    return keys


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else ".env")
    # Refuse to clobber: aimed at ".env" from a checkout, this would silently
    # replace a developer's real credentials with decoys. CI has no .env, so
    # the guard costs it nothing.
    if target.exists() and "--force" not in argv:
        print(
            f"refusing to overwrite the existing {target}; "
            "pass --force if you really mean to replace it",
            file=sys.stderr,
        )
        return 1
    keys = documented_keys()
    if not keys:
        print(f"no keys found in {TEMPLATE}", file=sys.stderr)
        return 1
    target.write_text(
        "".join(f"{key}=decoy-{key.lower()}\n" for key in keys), encoding="utf-8"
    )
    print(f"wrote {len(keys)} decoy keys to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
