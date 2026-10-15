# Box → VPS file sync (paper)

## Why

`trading-sync.timer` on the VPS expects `TRADING_SYNC_REMOTE` (e.g. `box:/srv/trading`) and pulls with `rsync -a --checksum` (no `--update`) for box-owned paths. The box has **no sshd**, so the VPS cannot pull. Leave `TRADING_SYNC_REMOTE` **unset** on the VPS (`state/sync.json` stays `remote_unset`). Push from the box instead.

## What is pushed

Manifest: `/workspace/trading/vps/deploy/sync_box_to_vps.txt` (same list as the repo / VPS). Semantics: `rsync -a --checksum`, **no** `--update`, **no** `--delete`. Box copy always wins for these paths.

| Relative path | Box source |
|---|---|
| `signals.json` | `/workspace/invest/signals.json` |
| `signals_archive/` | `/workspace/invest/signals/*.json` |
| `intents/` | **VPS-owned** — not in the box→VPS list; the runner on the VPS writes `/srv/trading/intents/` |
| `strategies/approved/` | `/workspace/trading/strategies/approved/` |
| `strategies/registry.jsonl` | `/workspace/trading/strategies/registry.jsonl` |
| `approvals/decided/` | `/workspace/trading/paper_home/approvals/decided/` |
| `config/adr_map.csv` | `/workspace/trading/config/adr_map.csv` |
| `config/adr_premium_rule_expected.json` | `/workspace/trading/config/…` |
| `state/adr_premium_state.json` | `/workspace/trading/state/…` if present |

Staging tree: `/workspace/trading/paper_home/` (built each run, then rsynced to `trading-vps:/srv/trading/`).

**Not** pushed (git deploy only): `config/confirmed_strategies.json`. **Not** touched by the box push: VPS-owned paths in `sync_vps_to_box.txt` (logs, `state/account.json`, reports, DB, `intents/`, …).

## Enable (run once or on a schedule)

```bash
# Tailscale if needed
bash /workspace/trading/vps/tailscale_box_start.sh

# One shot
bash /workspace/trading/vps/box_push_to_vps.sh

# Dry run
bash /workspace/trading/vps/box_push_to_vps.sh --dry-run

# Stage only (no SSH)
bash /workspace/trading/vps/box_push_to_vps.sh --stage-only
```

The box has no `crontab` / user systemd. Optional long-running pusher:

```bash
# every 30s (similar cadence to trading-sync.timer); log under paper_home/logs
nohup bash /workspace/trading/vps/box_push_to_vps.sh --loop 30 \
  >>/workspace/trading/paper_home/logs/box_push.loop.log 2>&1 &
echo $! >/workspace/trading/paper_home/logs/box_push.loop.pid
```

## Disable

```bash
# Stop loop if started
kill "$(cat /workspace/trading/paper_home/logs/box_push.loop.pid)" 2>/dev/null || true
rm -f /workspace/trading/paper_home/logs/box_push.loop.pid
# One-shot pushes simply stop being invoked; nothing is installed as a system service.
```

Do **not** set `TRADING_SYNC_REMOTE` on the VPS until the box can accept inbound SSH; that would make `trading-sync` fail every 30s and is not a substitute for this push.

## Verify

```bash
ssh trading-vps 'ls -la /srv/trading/signals.json /srv/trading/signals_archive/; sha256sum /srv/trading/signals.json'
sha256sum /workspace/invest/signals.json
# hashes must match after a successful push
```

## Engine intents

**VPS-owned.** The strategy runner on the VPS writes `/srv/trading/intents/<session>/<strategy>.vN.json` for `trading-submit`. Do **not** point a box `TRADING_HOME` at that tree so intents ride `box_push_to_vps.sh`. Optional copies under `paper_home/intents/` are staging or audit only and are **not** in `sync_box_to_vps.txt`.

`intents/` is listed in `sync_vps_to_box.txt` so the box can mirror what the VPS wrote. That pass uses `--update` and does not push intents back.

## SIGNALS_PATH on VPS

Repo default is `/workspace/invest/signals.json` (box path). After push, paper signals also sit at `/srv/trading/signals.json`. The strategy unit sets `SIGNALS_PATH=/srv/trading/signals.json`. Submit reads intents, not signals. Do not flip `ReadOnlyApi` as part of sync work.

## 2026-10-15 — intents ownership

`intents/` is **VPS-owned** (`trading-strategy.timer` → `scripts/run_paper_intents_daily.sh` → `auto-trading run-session`). Do **not** include `intents/` in the box→VPS rsync (`sync_box_to_vps.txt`); a box push would replace the VPS intent files. The VPS→box mirror is audit only.
