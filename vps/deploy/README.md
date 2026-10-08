# 常駐服務

生產環境只啟用下面這些單元，使用者是 `trader`，不要用 root。長駐行程用 `Restart=always`。一次性行程由 timer 叫起，不設 `Restart=always`，否則會和 timer 疊在一起空轉。單元檔不放登入名稱或密碼。

啟用：

- `trading-account.service`：連線已登入的模擬 Gateway（client id 13），做 R0 檢查，把淨值和持倉寫入 `state/account.json`。
- `trading-oms.service`：每 10 秒跑心跳、R9、critical 轉送、R29/R30。不重建股票池，也不寫每日摘要。
- `trading-recon.service`：用 Gateway 快照的持倉和委託，對本地日記帳與未完成訂單。不一致就凍結。
- `trading-watchdog.service`：每 30 秒查 Gateway 登入、R0 帳戶、SQLite、磁碟空間、時鐘偏差，以及 OMS 心跳（client id 14）。
- `trading-submit.service`：意圖、風控閘門、`decide_approval_mode`、然後券商（client id 11）。`IB_READ_ONLY_API=yes` 時什麼都不做。E2 把 `ReadOnlyApi` 改成 no 之前不會送單。
- `trading-sync.timer`：每 30 秒。box → VPS 拉核准策略、登記表和意圖；VPS → box 推日誌、狀態、報告和股票池。核准策略不在推出清單裡。兩邊都用 `--update`，較舊的檔案不能蓋掉較新的。沒有 `--delete`。
- `trading-summary.timer`：每日 06:00（澳門）寫摘要。
- `pool-rebuild.timer`：每日 06:00（香港）檢查是否該重建股票池。逾時 45 分鐘。

不要啟用 `trading-main.service`。它和 oms、recon、watchdog、account、submit 衝突，避免同一條迴圈佔用同一個 client id。本機除錯可以 `python -m auto_trading.execution <command> --once`。

`TRADING_SYNC_REMOTE` 是 rsync 目標（例如 `box:/srv/trading`）。未設定時同步行程直接結束，不連線。
