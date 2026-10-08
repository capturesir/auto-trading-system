# 常駐服務

生產環境只啟用下面這些單元，使用者是 `trader`，不要用 root。長駐行程用 `Restart=always`。一次性行程由 timer 叫起，不設 `Restart=always`，否則會和 timer 疊在一起空轉。單元檔不放登入名稱或密碼。

啟用：

- `trading-account.service`：連線已登入的模擬 Gateway（client id 13），做 R0 檢查，把淨值和持倉寫入 `state/account.json`。
- `trading-oms.service`：每 10 秒跑心跳、R9、critical 轉送、R29/R30。不重建股票池，也不寫每日摘要。
- `trading-recon.service`：用 Gateway 快照的持倉和委託，對本地日記帳與未完成訂單。不一致就凍結。
- `trading-watchdog.service`：每 30 秒查 Gateway 登入、R0 帳戶、SQLite、磁碟使用率（超過 85% 暫停開倉）、chrony 時鐘偏差（超過 1 秒警告、超過 5 秒暫停開倉），以及 OMS 心跳（client id 14）。檢查失敗就暫停新開倉；連續 3 次失敗重啟 `ibgateway.service`。
- `trading-submit.service`：讀 `intent/1.0`（`ticker`／`qty`），經完整風控閘門、`decide_approval_mode`、先寫日記帳，然後才到券商（client id 11）。同一 `intent_id` 不重送。`IB_READ_ONLY_API=yes` 時什麼都不做。E2 把 `ReadOnlyApi` 改成 no 之前不會送單。`mode` 不是 paper，或帶有 `live_size_multiplier`，都不會送出。
- `trading-sync.timer`：每 30 秒。box → VPS 用 `rsync -a --checksum`（沒有 `--update`）拉核准策略、登記表、ADR 規則和意圖，box 的內容一律蓋過 VPS。VPS → box 只用 `--update`，而且只推 VPS 擁有的路徑（日誌、報告、股票池、指定的 `state/` 檔）。`state/adr_premium_state.json` 只從 box 拉到 VPS，不會被 VPS 蓋回去。核准策略不在推出清單裡。沒有 `--delete`。
- `trading-summary.timer`：每日 06:00（澳門）寫摘要。
- `pool-rebuild.timer`：每日 06:00（香港）檢查是否該重建股票池。逾時 45 分鐘。

不要啟用 `trading-main.service`。它和 oms、recon、watchdog、account、submit 衝突，避免同一條迴圈佔用同一個 client id。本機除錯可以 `python -m auto_trading.execution <command> --once`。

`TRADING_SYNC_REMOTE` 是 rsync 目標（例如 `box:/srv/trading`）。未設定時同步行程直接結束，不連線。
