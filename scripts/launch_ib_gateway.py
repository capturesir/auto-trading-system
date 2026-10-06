"""Render the paper IBC config. The checker lives in ``vps/deploy``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "vps" / "deploy" / "render_template.py"


def _renderer():
    spec = importlib.util.spec_from_file_location("render_template", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing vps/deploy/render_template.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_ibc_config(dest: Path, *, template: Path | None = None) -> Path:
    """Delegate to the single deploy renderer."""
    renderer = _renderer()
    if template is None:
        return renderer.write_ibc_config(dest)
    return renderer.write_ibc_config(dest, template=template)


def main(argv: list[str] | None = None) -> None:
    """Write the config path given on the command line and do not connect."""
    _renderer().main(argv)


if __name__ == "__main__":
    main()
