# 盤中再評估啟用步驟（paper DUR243736）

澳門時間。只限 `DUR243736`。合併本 PR 不會 `systemctl enable`。07:05 的 `trading-strategy.timer` 保持原樣，不要 disable，也不要再加第二個 daily runner。

holdings-monitor 只發提醒。盤中 intents 只由 `trading-intraday-reeval` 寫。兩個單元不要互相呼叫。

## 1. 安裝單元（先不要開寫入）

```bash
cd /srv/trading
sudo cp vps/deploy/trading-intraday-reeval.service \
        vps/deploy/trading-intraday-reeval.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
```

單元裡 `INTRADAY_REEVAL_WRITE_INTENTS=0`、`INTRADAY_REEVAL_WRITE_EXITS=0`。client id 是 16，避開 11、12、13、14、15、31。

## 2. 07:05 之後先看 watchlist

`trading-strategy.timer` 跑完會寫：

- `state/intraday_watchlist.json`：confirm 未過的 BUY，以及（預設）當日 confirm 已過的 BUY
- `state/intraday_debounce.json`：當日已經出現過的 ENTRY / EXIT 鍵

```bash
python3 -m json.tool /srv/trading/state/intraday_watchlist.json | head
```

## 3. 乾跑

```bash
sudo systemctl start trading-intraday-reeval.service
python3 -m json.tool /srv/trading/state/intraday_reeval.json | head
```

RTH 之外的代號是 `market_closed`。confirm 轉為通過時 `would_entry` 是 true，而且 intents 目錄不應多出新 ENTRY（旗標仍是 0）。

手動一槍與上面同一條路徑：

```bash
cd /srv/trading
IBKR_PAPER_ACCOUNT=DUR243736 \
INTRADAY_REEVAL_WRITE_INTENTS=0 \
INTRADAY_REEVAL_WRITE_EXITS=0 \
.venv/bin/python -m auto_trading.intraday --once --home /srv/trading
```

## 4. 啟用 timer（使用者明示後）

節奏預設 2 分鐘，落在 1–5 分鐘。要改快慢，只改 timer 的 `OnUnitActiveSec`（`1min` 到 `5min`）。不要低於 1 分鐘。行程內的 `--loop` 會把 `INTRADAY_REEVAL_INTERVAL_SEC` 夾在 60 到 300 秒；VPS 用的是這個 timer，不是 `--loop`。

```bash
sudo systemctl enable --now trading-intraday-reeval.timer
systemctl list-timers 'trading-intraday-reeval*'
```

## 5. 打開紙上 ENTRY（再明示一次）

```bash
sudo systemctl edit trading-intraday-reeval.service
```

覆寫：

```
[Service]
Environment=INTRADAY_REEVAL_WRITE_INTENTS=1
```

然後 `sudo systemctl daemon-reload`。下一輪通過 confirm、而且當日尚未有同一 `strategy|ticker|session` ENTRY 的代號，會追加 intents。`trading-submit` 約 10 秒後自己讀走，風控閘不改。

`KILL_SWITCH`、`state/paused`、R9 `HALTED_COOLDOWN`、或 risk snapshot 不允許開倉時，不追加 ENTRY。已在檔裡的早晨 ENTRY 不會被清掉。

## 6. 打開盤中 EXIT（可選，再明示一次）

同一 override 加上：

```
Environment=INTRADAY_REEVAL_WRITE_EXITS=1
```

只對已持倉、而且 parent intent 齊的策略倉。同一原因當日只寫一次；完整 EXIT 之後不再追加。Submit 仍過風控。

## 7. holdings-monitor（只提醒）

確認 unit 仍是 `IB_READ_ONLY=1`、`IB_CLIENT_ID_HOLDINGS=15`，而且 `ExecStart` 是 `auto_trading.alerts.holdings_monitor`。它的 `ReadWritePaths` 沒有 `intents/`。

使用者明示後才：

```bash
sudo cp vps/deploy/trading-holdings-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-holdings-monitor.service
```

不要把這個服務改成寫 intents 或下單。

## 8. 驗收

```bash
DATE=$(TZ=Asia/Macau date +%F)
python3 -m json.tool /srv/trading/state/intraday_reeval.json
python3 - << PY
import json
from pathlib import Path
day = "$DATE"
for path in Path("/srv/trading/intents", day).glob("*.json"):
    body = json.loads(path.read_text())
    fresh = [row for row in body["intents"] if row.get("source") == "intraday_reeval"]
    if fresh:
        print(path.name, body["revision"], [(row["intent_type"], row["ticker"]) for row in fresh])
PY
```

關寫入：把兩個旗標改回 0，`daemon-reload`。已送出的模擬單不會自動撤回。
