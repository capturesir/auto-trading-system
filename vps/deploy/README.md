# 常駐服務

生產環境只啟用下面這些單元，使用者是 `trader`，不要用 root。長駐行程用 `Restart=always`。一次性行程由 timer 叫起，不設 `Restart=always`，否則會和 timer 疊在一起空轉。單元檔不放登入名稱或密碼。

啟用：

- `trading-account.service`：連線已登入的模擬 Gateway（client id 13），做 R0 檢查，把淨值和持倉寫入 `state/account.json`。
- `trading-oms.service`：每 10 秒跑心跳、R9、critical 轉送、R29/R30。不重建股票池，也不寫每日摘要。
- `trading-recon.service`：用 Gateway 快照的持倉和委託，對本地日記帳與未完成訂單。不一致就凍結。
- `trading-watchdog.service`：每 30 秒查 Gateway 登入、R0 帳戶、SQLite、磁碟使用率（超過 85% 暫停開倉）、chrony 的 `System time` 和 `Last offset`（取較大值；超過 1 秒警告、超過 5 秒暫停開倉）。`Leap status` 不是 Normal，或 Reference ID 是 0，也暫停開倉。時鐘、磁碟和 SQLite 只暫停開倉。只有 Gateway 登入連續失敗 3 次才重啟 `ibgateway.service`，而且 `systemctl` 退出碼是 0 才記下已重啟。
- `trading-submit.service`：讀 `intent/1.0`（`ticker`／`qty`）。報價由送單行程寫入 `state/quotes.json`。收到時間超過 10 秒的代號會向 Gateway 重取（`reqMarketDataType(3)`），等到 tick 45（`lastTimestamp`）或 tick 88（`delayedLastTimestamp`），或等到逾時。`delay_sec` 是收到時間減資料時間；算不出來是 R22，不會當成 0。收到時間記在 tick 回呼當下，而不是等逾時之後。模擬盤接受型別 3／4，另記一行 `delayed_quote`；`delay_sec` 超過 1800 秒也是 R22。收到時間超過 10 秒同樣是 R22。限價用 `ref_price_at_create` 和 `limit_offset_bps`，對到該價位帶的 tick 倍數（買進向下、賣出向上），並遵守 `limit_price_cap`。開倉的 `valid_from` 在風控閘門之前檢查：缺了、解析不了，或過了該市場當日收市，就寫成終態並移出佇列。只有永久拒絕（R25、`R0_FORBIDDEN_PORT`、`R0_NOT_PAPER_ACCOUNT`、`R0_ACCOUNTS_CHANGED`、`LIVE_NOT_AUTHORIZED`、R1 到 R6、`LOT_TOO_LARGE`、`INTENT_EXPIRED`）才把開倉寫成終態，同一 `intent_id` 不重試。其餘代碼留在佇列再試。出場和保護性停損不會因閘門拒絕而移出佇列，連續被擋三次寫一筆 critical。午市、競價和休市的 R10 不計入這三次。保護性停損不因 `valid_from` 或該節收市而移出佇列；引擎對保護性單不寫 `valid_from`。出場仍在該節收市到期。持倉數量用券商快照，策略拆分用日記帳，`700` 對上 `0700.HK`。未成交單含方向和數量，`orderRef` 寫進 `order_refs`。市值是報價乘匯率。現金讀 `TotalCashValue`。匯率讀 `ExchangeRate` 或 `state/fx.json`；非 HKD 缺匯率是 R17，不會當成 1。然後經完整風控閘門、`decide_approval_mode`、先寫日記帳，券商的 `orderRef` 用日記帳的 sha256。同一 `intent_id` 不重送。沒有策略識別的開倉是 R25，不進人手批准。pin 檔讀不到時開倉是 R25 並寫 critical，出場和保護性停損仍送出。`account.json` 超過 90 秒視為未連線。`IB_READ_ONLY_API=yes` 時什麼都不做。E2 把 `ReadOnlyApi` 改成 no 之前不會送單。`mode` 不是 paper，或帶有 `live_size_multiplier`，都不會送出。
- `trading-sync.timer`：每 30 秒。box → VPS 用 `rsync -a --checksum`（沒有 `--update`）拉核准策略、登記表和 ADR 規則，box 的內容一律蓋過 VPS。意圖由 VPS 的 `trading-strategy.timer` 寫入，不在 box→VPS 清單；VPS → box 可鏡像 `intents/` 供檢視。VPS → box 只用 `--update`，而且只推 VPS 擁有的路徑（日誌、報告、股票池、指定的 `state/` 檔）。`state/adr_premium_state.json` 只從 box 拉到 VPS，不會被 VPS 蓋回去。核准策略不在推出清單裡。沒有 `--delete`。
- `trading-strategy.timer`：每日 07:05（澳門，unit 上是 23:05 UTC）跑一次 `scripts/run_paper_intents_daily.sh`，裡面只呼叫 `auto-trading run-session`。倉庫不執行 enable，也不執行 disable。機器上若已經 enable，合併後只替換這個 unit，不要再加第二個 timer。
- `trading-summary.timer`：每日 06:00（澳門）寫摘要。
- `pool-rebuild.timer`：每日 06:00（香港）檢查是否該重建股票池。逾時 45 分鐘。

新倉在該市場開盤後 5 分鐘的 `open_buffer` 會被 R10 拒絕，檔案留在佇列，5 分鐘過後的下一輪再送，直到通過或該節收市。收盤前 10 分鐘的新倉同樣是 R10 並重試。沒有另加開盤後約 15 分鐘的延遲。引擎的 `valid_from` 是該市場開盤時間，所以開倉會先碰到 R10。出場和保護性停損不等這一段，閘門拒絕也不會把它們移出佇列。午市、競價和休市擋下的 R10 不計入連續三次的 critical。保護性停損留在佇列過夜，下一節若仍需要會再送；出場過了該節收市才移出。

不要啟用 `trading-main.service`。它和 oms、recon、watchdog、account、submit 衝突，避免同一條迴圈佔用同一個 client id。本機除錯可以 `python -m auto_trading.execution <command> --once`。

`TRADING_SYNC_REMOTE` 是 rsync 目標（例如 `box:/srv/trading`）。未設定時同步行程直接結束，不連線。

`config/confirmed_strategies.json` 只隨 git 部署，不在 box↔VPS 的同步清單裡。清單裡的 `config` 或 `config/` 也會帶上這個檔，CI 和同步都會拒絕。新增策略的順序是：使用者確認雜湊，PR 同時更新 pin、`strategies/registry.jsonl` 和 `strategies/approved/`，CI 三方一致，合併，再用 git 部署。啟動時日誌裡的 hash12 要和確認值相同。pin 檔缺失或損壞時，送單行程不崩潰：開倉拒絕（R25）並寫 critical，出場和保護性停損照常。

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
