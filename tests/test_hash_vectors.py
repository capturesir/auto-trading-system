"""Published spec hashes and the mode exclusion rule."""

from __future__ import annotations

import json
from pathlib import Path

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.spec import spec_hash

ROOT = Path(__file__).resolve().parents[1]
DRAFTS = ROOT / "strategies" / "drafts"

VECTORS = {
    "sig_trend.v2.json": (
        "sha256:7d3bbb3abaa25456990a18631c1d484d87b482cdb433d6793d7d08ec0dd33be2"
    ),
    "sig_pullback.v2.json": (
        "sha256:d7d5d60b5d0fa7a9a6f5dc4802ede014ece0ba6809ec104be1d9c401e5c8294e"
    ),
    "fv_rotation.v2.json": (
        "sha256:5c77a1677b90b62035dfc576ca3912d33cd942155f36cd1cf7870e2d058c4c04"
    ),
    "news_event.v3.json": (
        "sha256:31e5581640493b3ad7c565dc9dbd7002703a69977ed0a1ed02e54b42b8a48903"
    ),
    "news_event.v4.json": (
        "sha256:9767e8571eea3d128ad9117b6a99fcc3cddd1ec5c429255722e67e27d53a23d0"
    ),
}


def test_draft_hashes_match_v06_vectors() -> None:
    for name, digest in VECTORS.items():
        document = loads_strict((DRAFTS / name).read_text(encoding="utf-8"))
        assert spec_hash(document) == digest
        assert digest.startswith("sha256:31e558164049") or name != "news_event.v3.json"
        assert digest.startswith("sha256:9767e8571eea") or name != "news_event.v4.json"


def test_integer_and_float_are_different_documents() -> None:
    assert spec_hash({"n": 10}) != spec_hash({"n": 10.0})
    assert json.dumps(0.10) == "0.1"
