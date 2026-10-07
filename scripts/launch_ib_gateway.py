"""Point at the deployed paper Gateway start script.

``vps/deploy/ibgw-start.sh`` and ``vps/deploy/render_template.py`` are the
copies that run on the VPS. This process does not render a config, does not
read login secrets, and does not write them to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "vps" / "deploy" / "ibgw-start.sh"


def main(argv: list[str] | None = None) -> None:
    """Refuse a local render. The VPS unit is the only starter."""
    del argv
    print(
        "paper Gateway starts from vps/deploy/ibgw-start.sh; "
        "this command does not write a config or read login secrets",
        file=sys.stderr,
    )
    print(START)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
