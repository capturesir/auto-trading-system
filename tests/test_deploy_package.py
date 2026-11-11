"""Deploy pack checks. No broker connection."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from auto_trading.paper_only import scan_tree
from auto_trading.signals import load_signals

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "ats-install.sh"
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"

CORE = {
    "account",
    "oms",
    "recon",
    "watchdog",
    "submit",
    "strategy",
    "summary",
    "pool",
    "sync",
}
OPTIONAL = {"holdings", "reeval"}


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, text=True, capture_output=True)


def _compose() -> dict:
    loaded = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _walk(node: object) -> None:
    if isinstance(node, dict):
        assert "ports" not in node
        for value in node.values():
            _walk(value)
    elif isinstance(node, list):
        for value in node:
            _walk(value)


def test_paper_scan_stays_clean() -> None:
    assert scan_tree(ROOT) == []


def test_doctor_package_and_dry_run() -> None:
    doctor = _run(["bash", str(INSTALLER), "doctor", "--package"])
    assert doctor.returncode == 0, doctor.stderr
    dry = _run(["bash", str(INSTALLER), "install", "--dry-run"])
    assert dry.returncode == 0, dry.stderr
    assert "不會啟動 Gateway" in dry.stdout


def test_installer_does_not_start_gateway() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "systemctl enable --now ibgateway" not in text
    assert "systemctl start ibgateway" not in text


def test_compose_pins_paper_gateway() -> None:
    raw = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^[ \t]*ports:", raw) is None
    assert "trading-main" not in raw
    assert "gnzsnz" not in raw
    document = _compose()
    _walk(document)
    services = document["services"]
    assert set(services) == CORE | OPTIONAL
    for name, spec in services.items():
        assert spec["network_mode"] == "host"
        assert spec["environment"]["IB_HOST"] == "127.0.0.1"
        assert str(spec["environment"]["IB_PORT"]) == "4002"
        assert spec["command"][-1] != "main"
        assert any(item.endswith(":/srv/trading/signals.json") for item in spec["volumes"])
        if name in OPTIONAL:
            assert spec["profiles"] == ["optional"]
        else:
            assert "profiles" not in spec
    assert services["account"]["environment"]["IB_CLIENT_ID"] == "13"
    assert services["account"]["environment"]["TRADING_GATEWAY_CONNECT"] == "1"
    assert services["recon"]["environment"]["IB_CLIENT_ID"] == "12"
    assert "TRADING_GATEWAY_CONNECT" not in services["recon"]["environment"]
    assert "TRADING_GATEWAY_CONNECT" not in services["oms"]["environment"]
    assert "IB_CLIENT_ID" not in services["oms"]["environment"]
    assert services["watchdog"]["environment"]["IB_CLIENT_ID"] == "14"
    assert services["submit"]["environment"]["IB_CLIENT_ID"] == "11"
    assert services["submit"]["environment"]["IB_READ_ONLY_API"] == "${IB_READ_ONLY_API:-yes}"
    assert services["watchdog"]["pid"] == "host"
    assert services["watchdog"]["init"] is False
    assert "SYS_ADMIN" in services["watchdog"]["cap_add"]
    assert "cap_add" not in services["account"]
    assert services["reeval"]["environment"]["INTRADAY_REEVAL_WRITE_INTENTS"] == "0"
    assert services["reeval"]["environment"]["INTRADAY_REEVAL_WRITE_EXITS"] == "0"
    assert services["holdings"]["environment"]["IB_CLIENT_ID_HOLDINGS"] == "15"
    docker_text = (ROOT / "deploy" / "compose" / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE" not in docker_text
    assert "gnzsnz" not in docker_text


def test_env_example_is_names_only() -> None:
    seen: set[str] = set()
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        assert value.strip() == ""
        assert key not in seen
        seen.add(key)
    assert "IBKR_PAPER_ACCOUNT" in seen
    assert "HEALTHCHECK_PING_URL" in seen
    assert "IB_PORT" in seen


def test_signals_example_loads() -> None:
    hkt = ZoneInfo("Asia/Hong_Kong")
    document = load_signals(
        ROOT / "deploy" / "signals.example.json",
        now=datetime(2026, 1, 1, 9, 0, tzinfo=hkt),
    )
    assert document.schema_version == "1.0"
    assert document.signals == []


def test_timer_schedule_matches_units() -> None:
    script = ROOT / "deploy" / "compose" / "timer-loop.sh"
    strategy = int(_run(["sh", str(script), "--print-next", "strategy"]).stdout.strip())
    moment = datetime.fromtimestamp(strategy, UTC)
    assert moment.hour == 23 and moment.minute == 5 and moment.second == 0
    summary = int(_run(["sh", str(script), "--print-next", "summary"]).stdout.strip())
    local = datetime.fromtimestamp(summary, ZoneInfo("Asia/Macau"))
    assert local.hour == 6 and local.minute == 0
    pool = int(_run(["sh", str(script), "--print-next", "pool"]).stdout.strip())
    hong_kong = datetime.fromtimestamp(pool, ZoneInfo("Asia/Hong_Kong"))
    assert hong_kong.hour == 6 and hong_kong.minute == 0
    assert _run(["sh", str(script), "--print-next", "sync"]).stdout.strip() == "30"
    assert _run(["sh", str(script), "--print-next", "reeval"]).stdout.strip() == "120"


def test_port_guard_print_derives_refused_ports() -> None:
    script = ROOT / "deploy" / "host" / "ibkr-port-guard.sh"
    rows = [int(line) for line in _run(["sh", str(script), "--print"]).stdout.split()]
    assert rows[:3] == [4002, 7497, 7462]
    assert rows[3] == 4002 - 1
    assert rows[4] == 7497 - 1


def test_systemctl_wrapper_refuses_without_root() -> None:
    if os.geteuid() == 0:
        return
    script = ROOT / "deploy" / "compose" / "systemctl-host.sh"
    proc = _run(["sh", str(script), "restart", "ibgateway.service"])
    assert proc.returncode != 0
