# 常駐服務

生產環境只啟用下面這些單元，使用者是 `trader`，不要用 root。長駐行程用 `Restart=always`。一次性行程由 timer 叫起，不設 `Restart=always`，否則會和 timer 疊在一起空轉。單元檔不放登入名稱或密碼。

啟用：

- `trading-account.service`：連線已登入的模擬 Gateway（client id 13），做 R0 檢查，把淨值和持倉寫入 `state/account.json`。
- `trading-oms.service`：每 10 秒跑心跳、R9、critical 轉送、R29/R30。不重建股票池，也不寫每日摘要。
- `trading-recon.service`：用 Gateway 快照的持倉和委託，對本地日記帳與未完成訂單。不一致就凍結。
- `trading-watchdog.service`：每 30 秒查 Gateway 登入、R0 帳戶、SQLite、磁碟使用率（超過 85% 暫停開倉）、chrony 的 `System time` 和 `Last offset`（取較大值；超過 1 秒警告、超過 5 秒暫停開倉）。`Leap status` 不是 Normal，或 Reference ID 是 0，也暫停開倉。時鐘、磁碟和 SQLite 只暫停開倉。只有 Gateway 登入連續失敗 3 次才重啟 `ibgateway.service`，而且 `systemctl` 退出碼是 0 才記下已重啟。
- `trading-submit.service`：讀 `intent/1.0`（`ticker`／`qty`）。報價來自 `state/quotes.json`（帳戶行程寫入；允許連線時也可向 Gateway 取延遲報價）。模擬盤可以用延遲不超過 30 分鐘的型別 3／4，另外記一行 `delayed_quote`；更舊的是 R22。限價用 `ref_price_at_create` 和 `limit_offset_bps`，並遵守 `limit_price_cap`。`valid_from` 過了該市場當日收市就拒絕並移出佇列。然後經完整風控閘門、`decide_approval_mode`、先寫日記帳，券商的 `orderRef` 用日記帳的 sha256。同一 `intent_id` 不重送。沒有策略識別的開倉是 R25，不進人手批准。`account.json` 超過 90 秒視為未連線。`IB_READ_ONLY_API=yes` 時什麼都不做。E2 把 `ReadOnlyApi` 改成 no 之前不會送單。`mode` 不是 paper，或帶有 `live_size_multiplier`，都不會送出。
- `trading-sync.timer`：每 30 秒。box → VPS 用 `rsync -a --checksum`（沒有 `--update`）拉核准策略、登記表、ADR 規則和意圖，box 的內容一律蓋過 VPS。VPS → box 只用 `--update`，而且只推 VPS 擁有的路徑（日誌、報告、股票池、指定的 `state/` 檔）。`state/adr_premium_state.json` 只從 box 拉到 VPS，不會被 VPS 蓋回去。核准策略不在推出清單裡。沒有 `--delete`。
- `trading-summary.timer`：每日 06:00（澳門）寫摘要。
- `pool-rebuild.timer`：每日 06:00（香港）檢查是否該重建股票池。逾時 45 分鐘。

不要啟用 `trading-main.service`。它和 oms、recon、watchdog、account、submit 衝突，避免同一條迴圈佔用同一個 client id。本機除錯可以 `python -m auto_trading.execution <command> --once`。

`TRADING_SYNC_REMOTE` 是 rsync 目標（例如 `box:/srv/trading`）。未設定時同步行程直接結束，不連線。

`config/confirmed_strategies.json` 只隨 git 部署，不在 box↔VPS 的同步清單裡。新增策略的順序是：使用者確認雜湊，PR 同時更新 pin、`strategies/registry.jsonl` 和 `strategies/approved/`，CI 三方一致，合併，再用 git 部署。啟動時日誌裡的 hash12 要和確認值相同。

部署這台 VPS 時還要做兩件事（repo 裡的單元不會自己做）：

- chrony。Ubuntu 24.04 預設是 `systemd-timesyncd`，看門狗讀的是 `chronyc tracking`。沒有 chrony 就會一直暫停開倉。

```bash
sudo apt-get install -y chrony
sudo systemctl enable --now chrony
chronyc tracking
```

- sudoers。`trader` 只能重啟 `ibgateway.service`，不能跑其他 systemctl。

```bash
sudo visudo -cf vps/deploy/trader-ibgateway.sudoers
sudo install -m 0440 vps/deploy/trader-ibgateway.sudoers /etc/sudoers.d/trader-ibgateway
```
