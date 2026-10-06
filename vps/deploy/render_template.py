"""Render and check the paper IBC config.

This file is the only checker for the guard keys. ``ibgw-start.sh`` calls
``--check``. ``ReadOnlyApi`` stays ``yes`` until E2 is approved by the user.
The rendered file is created mode 0600. Credentials are not written to
``.env`` and are not printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TEMPLATE = Path(__file__).with_name("ibc.paper.ini.template")
USER_VAR = "IBKR_" + "PAPER_USERNAME"
PASSWORD_VAR = "IBKR_" + "PAPER_PASSWORD"

GUARDS = (
    ("TradingMode", "paper"),
    ("OverrideTwsApiPort", "4002"),
    ("ReadOnlyApi", "yes"),
)


def parse_config(text: str) -> dict[str, str]:
    """Read ``key=value`` lines. Comments and blanks are ignored."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        found[key.strip()] = value.strip()
    return found


def assert_guards(text: str) -> None:
    """Refuse a config whose guard keys are not the paper set."""
    found = parse_config(text)
    for key, value in GUARDS:
        if found.get(key) != value:
            raise RuntimeError(f"IBC guard {key} is not {value}")


def write_ibc_config(dest: Path, *, template: Path = TEMPLATE) -> Path:
    """Render ``template`` into ``dest`` as mode 0600. Never logs the values."""
    username = os.environ.get(USER_VAR, "")
    password = os.environ.get(PASSWORD_VAR, "")
    if not username or not password:
        raise RuntimeError(f"missing {USER_VAR} or {PASSWORD_VAR}")
    text = template.read_text(encoding="utf-8")
    assert_guards(text)
    rendered = text.replace("__" + USER_VAR + "__", username)
    rendered = rendered.replace("__" + PASSWORD_VAR + "__", password)
    assert_guards(rendered)
    dest.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(dest, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    return dest


def main(argv: list[str] | None = None) -> None:
    """``--check <file>`` or ``<dest>`` to render. Does not connect."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--check":
        assert_guards(Path(args[1]).read_text(encoding="utf-8"))
        return
    if len(args) != 1:
        raise SystemExit("usage: render_template.py --check <file> | <config-path>")
    try:
        path = write_ibc_config(Path(args[0]))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(path)


if __name__ == "__main__":
    main()
