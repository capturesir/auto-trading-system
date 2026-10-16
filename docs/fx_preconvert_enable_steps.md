# 模擬盤買進前換匯（paper only）

帳戶 `DUR243736` 的可用現金主要是 USD。買進以其他幣別交割的股票（KRW、TWD、HKD、JPY 等）之前，必須先從 USD 換成足夠的目標幣，含股票佣金／稅與匯率緩衝，避免該幣現金變成借方。

只做模擬。埠只有 4002 與 7497。沒有另一條路徑會對非模擬單換匯。

## 行為

`TRADING_FX_PRECONVERT` 未設成 `1` 時，送單與風控和現在一樣：不換匯，也不套用 R31。

設成 `1` 之後，紙上路徑對 **BUY 的 ENTRY／ADD**，且結算幣不是 USD：

1. 目標幣需求 = 股數 × 限價 + 該市場買進佣金與稅 + 名目 × 1.20%（100 bps 緩衝 + 20 bps FX 佣金估計）。
2. 讀 `cash_by_ccy`（已扣同幣未成交買單）。夠了就直接送股票單，不換匯。
3. 不夠就計算短少。換出的 USD = 短少金額用 `fx_to_base`（每單位折合 HKD）換算後，再加 2 USD 最低費用，向上取到分。
4. USD 夠：送 IB 換匯，等到目標幣餘額覆蓋需求，才送股票單。
   - HKD、JPY（以及其餘 IdealPro 幣）是 IdealPro 市價 FX：賣出 USD。
   - KRW、TWD 是 IB 現金轉換 `FXCONV`：同樣賣出 USD。
5. 餘額檔沒有 `cash_by_ccy`、沒有匯率、USD 不夠、換匯被拒、或等到逾時（30 秒）仍不夠：風控回 **R31**，**不送股票單**。R31 留在佇列，下一輪再試，不是永久拒絕。
6. 同一輪下一張單會先扣掉這一張已占用的目標幣，避免兩張單共用同一筆餘額。

USD 股票不在這條路徑裡，不會把其他幣換成 USD。出場、減倉、保護性停損也不換匯。

換匯嘗試寫在 `state/fx_conversions.jsonl`（`confirmed`、`usd_short`、`unconfirmed`、`unknown_balance`、`no_converter`）。股票單仍走原來的日記帳。

`trading-submit.service` 預設**沒有**這個變數。合併本 PR 不會自己打開換匯。

## 啟用（使用者明示後）

先確認送單行程已經可以連模擬 Gateway，而且 `ReadOnlyApi` 已依 E2 改成 no。只讀時換匯與股票單都不會送出。

```bash
cd /srv/trading

# 1. 帳戶行程寫入各幣現金。沒有這份餘額時，開了開關也只會 R31，不會猜。
python -m auto_trading.execution account --once
python - << 'PY'
import json
body = json.load(open("state/account.json"))
print(body.get("cash_by_ccy"))
PY
# 預期看到 USD，以及其他已有的幣別。印出 None 就先不要做第 2 步。

# 2. 只給送單行程。不要寫進會被下次部署蓋掉的 unit 本體；用 drop-in。
sudo mkdir -p /etc/systemd/system/trading-submit.service.d
sudo tee /etc/systemd/system/trading-submit.service.d/fx-preconvert.conf >/dev/null << 'EOF'
[Service]
Environment=TRADING_FX_PRECONVERT=1
EOF
sudo systemctl daemon-reload
sudo systemctl restart trading-submit.service
```

`TRADING_GATEWAY_CONNECT=1` 時，送單行程才會自己連 Gateway 換匯。單元檔裡已經有這一行。測試或沒有 Gateway 的行程不要設 `TRADING_FX_PRECONVERT=1`，否則外幣買單會停在 R31。

## 驗收

目標幣不夠、USD 也不夠付短少：`state/submit.json` 的 `sent` 是 0，`rejected` 含 `R31`，`fx_conversions.jsonl` 是 `usd_short`，Gateway 沒有該股票單，也沒有換匯單。

目標幣不夠、USD 夠，且換匯成交後餘額覆蓋需求：先出現換匯單，`fx_conversions.jsonl` 為 `confirmed`，然後才有股票單。

目標幣本來就夠：沒有換匯單，股票單直接送出。

關掉時刪除 drop-in 再 reload、restart `trading-submit.service`。未設定變數時外幣買單恢復原本路徑，不再因 R31 停下。
