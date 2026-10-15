# 策略 runner 最短啟用步驟（paper only）

> **2026-10-15 現況**：VPS 已用 `scripts/run_paper_intents_once.py`、`scripts/run_paper_intents_daily.sh` 與 `trading-strategy.timer`（07:05 澳門＝23:05 UTC）。本倉庫只演進這一套。`auto-trading run-session` 與過渡腳本呼叫同一個 `run_session`。不要另加第二個 daily runner 或第二個 timer，也不要在合併時 `systemctl disable`。

澳門時間；只限 DUR243736。

## 0. 部署家：VPS

| 選項 | 利 | 弊 |
|---|---|---|
| **VPS**（採用） | 已有 systemd timer；07:05 可靠；signals 在 `/srv/trading/signals.json` | `sync_box_to_vps.txt` 不得含 `intents/`，否則 box 的空目錄或舊檔會蓋過 VPS（`rsync --checksum` 沒有 `--update`） |
| Box | 舊文曾把 engine 寫在 paper_home | box 沒有 crontab／user systemd，排程容易斷 |

Runner 寫 `/srv/trading/intents/<YYYY-MM-DD>/`。box push 不推 `intents/`。`sync_vps_to_box.txt` 含 `intents/`，方便 box 檢視。

## 1. 合併 PR 後（VPS）

```bash
cd /srv/trading
grep -E 'DUR243736|paper_only' state/account.json config/risk_limits.json 2>/dev/null | head

sudo cp vps/deploy/trading-strategy.service vps/deploy/trading-strategy.timer /etc/systemd/system/
sudo systemctl daemon-reload
# 使用者明示前：不要 enable，也不要 disable 已在跑的 timer
# sudo systemctl enable --now trading-strategy.timer
```

`sync_box_to_vps.txt` 已去掉 `intents/`。`sync_vps_to_box.txt` 已加上 `intents/`。

## 2. 合併後手動一槍

```bash
cd /srv/trading
.venv/bin/auto-trading run-session --date 2026-10-16 --trading-home /srv/trading

# 同一條路徑的過渡腳本
.venv/bin/python scripts/run_paper_intents_once.py \
  --trading-home /srv/trading \
  --signals /srv/trading/signals.json \
  --approved-dir /srv/trading/strategies/approved \
  --bars-cache /srv/trading/data/bars_cache \
  --session-date 2026-10-16
```

預期：`intents/2026-10-16/sig_*.v3.json`。BUY 且 confidence≥60 且 bars 齊 → 非空 intents。無候選人 → 空陣列但有 revision。hash 不符 → 不寫該策略檔。`ta_breakout52` 在 `plan_session` 仍不接受 `trigger.type=market` 時 skip，並寫警告，不當作成功。

## 3. 啟用 timer（僅使用者明示後）

```bash
sudo systemctl enable --now trading-strategy.timer
systemctl list-timers 'trading-strategy*'
# OnCalendar 對齊 07:05 Asia/Macau = 23:05 UTC
```

若 timer 已經 enable，合併後核對 unit 的 `ExecStart` 仍是 `run_paper_intents_daily.sh`（裡面只呼叫 `run-session`）。不要再掛第二個 ExecStart。

## 4. 驗收

```bash
DATE=$(TZ=Asia/Macau date +%F)
ls -la /srv/trading/intents/$DATE/
```

CI 與本 PR 不執行 `systemctl enable`。
