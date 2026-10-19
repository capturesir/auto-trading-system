# 補記 2026-10-16 SPY 模擬成交

紙上帳戶 `DUR243736` 在 2026-10-16 21:35:08（澳門）於 ARCA 成交買進 10 股 SPY，價格 772.28，execId `00025b49.6ad2ade3.01.01`，permId `1145186315`。佣金 1.00 USD，是現金從 1,000,000 降到 992,276.20 再扣掉 10 × 772.28 的差額。意圖 `2026-10-16-SPY-entry-test10`，策略 `sig_trend`。內部訂單 id 是 `sha256(intent_id + "1")`，前綴 `04ad62c5`、後綴 `4501f4`。

當時送單迴圈把訂單留在 `SUBMITTED`，沒有呼叫 `record_fill`。日記帳沒有這筆成交，策略持倉是 0，`recon.json` 凍結（IB 10 股，內部 0，再加上這張內部未完成單）。

這個腳本只補這一筆。它不連 Gateway，也不改 `account.json`。套用前會把 `data/trading.db` 複製到 `data/backups/trading-before-spy-20261016.db`。

## 先停會寫日記帳的行程

在 VPS 上，以部署這份程式碼之後的版本執行。不要在雲端代理裡對 VPS 熱修。

```bash
cd /srv/trading
sudo systemctl stop trading-submit.service trading-oms.service trading-account.service trading-recon.service
```

`trading-recon` 只寫 `state/recon.json`，停掉是為了避免它在補記過程中用舊日記帳再寫一次凍結。

## 乾跑

```bash
cd /srv/trading
.venv/bin/python scripts/backfill_spy_fill_20261016.py --home /srv/trading
```

不寫資料庫。輸出要有訂單狀態 `SUBMITTED`（或已經是 `FILLED`）、execId、價格 772.28、數量 10、佣金 1.0。訂單不在日記帳裡就會拒絕，不會自己插入一張訂單。

## 套用

```bash
.venv/bin/python scripts/backfill_spy_fill_20261016.py --home /srv/trading --apply
```

做的事：

1. 若還沒有備份，複製 `data/trading.db`。
2. 用既有的 `record_fill` 寫入 execId，訂單改 `FILLED`，`book_fill` 到 `sig_trend`。
3. 再跑一次不會加第二筆，策略數量維持 10。
4. `state/account.json` 裡 SPY 數量至少 10 時，重寫 `state/recon.json`。數量對上、內部沒有未完成單，`freeze` 應為 false。

策略均價是成交價 772.28。IB 持倉均價約 772.38 含那 1 美元佣金，佣金記在 `executions.fee`。這筆沒有 conId，策略列的 `con_id` 用 0；之後同一策略同一代號的成交會沿用這一列。

## 還原

只有在備份之後沒有新的訂單 id 或別的 execId 時才還原。否則拒絕，避免蓋掉後來的成交。

```bash
.venv/bin/python scripts/backfill_spy_fill_20261016.py --home /srv/trading --undo
```

這會把整份 `trading.db` 換回備份。還原後若帳戶快照仍是 SPY 10 股，對帳會回到凍結，這是補記前的狀態。

## 恢復行程

```bash
sudo systemctl start trading-account.service trading-oms.service trading-recon.service trading-submit.service
```

部署後的送單迴圈每一輪都會向 Gateway 要 executions（filter `clientId` 0，所以看得到 client 11 送出的單），新的 execId 走同一條 `record_fill`。`ReadOnlyApi=yes` 仍然不送新單，但會記已經成交的回報。OMS 不另開 socket，它讀 `state/executions.json`。帳戶快照也會寫這份檔。同一 execId 再輪詢不會重複記帳。
