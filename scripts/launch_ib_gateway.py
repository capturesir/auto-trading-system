"""Write an IBC config for an already-started paper Gateway.

This is the only program that reads the paper login variables. The trading
process connects to a Gateway that is already logged in on 127.0.0.1:4002
and does not read those variables.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "ibc.paper.ini.template"
USER_VAR = "IBKR_" + "PAPER_USERNAME"
PASSWORD_VAR = "IBKR_" + "PAPER_PASSWORD"


def write_ibc_config(dest: Path, *, template: Path = TEMPLATE) -> Path:
    """Render ``template`` into ``dest`` with mode 0600. Never logs the values."""
    username = os.environ.get(USER_VAR, "")
    password = os.environ.get(PASSWORD_VAR, "")
    if not username or not password:
        raise RuntimeError(f"missing {USER_VAR} or {PASSWORD_VAR}")
    text = template.read_text(encoding="utf-8")
    if "TradingMode=paper" not in text or "OverrideTwsApiPort=4002" not in text:
        raise RuntimeError("IBC template is not the paper Gateway template")
    rendered = text.replace("__" + USER_VAR + "__", username)
    rendered = rendered.replace("__" + PASSWORD_VAR + "__", password)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, encoding="utf-8")
    dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return dest


def main(argv: list[str] | None = None) -> None:
    """Write the config path given on the command line and do not connect."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: launch_ib_gateway.py <config-path>")
    try:
        path = write_ibc_config(Path(args[0]))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(path)


if __name__ == "__main__":
    main()
