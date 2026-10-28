# 自動交易系統

個人用的股票交易系統。研究團隊每天提供一份估值訊號檔，本系統讀取並驗證它，再結合行情與技術指標、版本化策略、回測與風控，經執行器送到 Interactive Brokers。新聞、事件理解與標的發掘由研究團隊負責，不在這個儲存庫。

市場包含美國、日本、香港、韓國、台灣。台灣（`*.TW`）透過香港 IBKR 帳戶交易，與其他市場相同，載入器不會把它標成不可交易。

這個儲存庫沒有下單用的語言模型。風控閘門是唯一可以放行委託的路徑。

## 架構

```
/workspace/invest/signals.json
        │
        ▼
   signals                 驗證每日訊號，標記已過期的列
        │
        ▼
   market                  行情
   market.indicators       技術指標
        │
        ▼
   strategy                版本化 JSON；雜湊須與已核准版本一致才執行
        │
        ▼
   backtest                回測
        │
        ▼
   risk                    風控閘門（唯一通往下單的入口）
        │
        ▼
   execution               執行器，只在風控放行後呼叫券商
        │
        ▼
   broker                  非同步 Broker 介面
   execution.ibkr          目前唯一的券商佔位（IB Gateway）
        │
        ▼
   portfolio               持倉、損益、交易日誌
   storage                 SQLite repository

   alerts                  價格、技術指標、訊號變化
   reports                 每週／每月績效回顧
   dashboard               持倉、損益、訊號（Plotly）
```

這一版實作了設定、訊號載入、策略雜湊（不含 `mode`）、登記表、指標、日線回測（含手續費、限價成交、整手與零股、分配器、R8／R9）、意圖檔與只寫入 `pending/` 的提醒。執行器仍必須先經過風控閘門。IBKR 下單、儀表板與新聞自由文字解析仍不在這層。

## 目錄

```
src/auto_trading/
  config.py                 環境變數設定
  signals/                  訊號模型與載入器（已實作）
  market/                   行情與 indicators.py
  strategy/                 spec.py 雜湊核准、engine.py
  backtest/
  risk/                     風控閘門
  broker.py                 非同步 Broker 介面與訂單型別
  execution/                executor.py、ibkr.py
  portfolio/                positions.py、journal.py
  storage/                  repository.py、sqlite.py
  alerts/
  reports/
  dashboard/
tests/
```

## 訊號檔

預設路徑是 `/workspace/invest/signals.json`。可以用環境變數 `SIGNALS_PATH` 覆寫，也可以在呼叫 `load_signals(path)` 時直接給路徑。

`schema_version` 必須是 `"1.0"`。檔案編碼為 UTF-8 JSON。頂層欄位：

| 欄位 | 說明 |
| --- | --- |
| `schema_version` | 固定 `"1.0"` |
| `generated_at` | ISO-8601 時間，時區偏移必須是 `+08:00` |
| `generated_by` | 產生者 |
| `price_date` | 價格日期，`YYYY-MM-DD` |
| `signals` | 訊號陣列，允許空陣列 |

每一筆訊號都要有：`signal_id`、`ticker`、`market`、`name`、`asset_type`、`currency`、`side`、`confidence`、`fair_value`、`market_price`、`price_date`、`upside_pct`、`horizon`、`rationale`、`report_path`、`expires_at`。

驗證規則：

- `side` 只能是 `BUY`、`SELL`、`HOLD`
- `confidence` 必須是 0 到 100 的整數
- `generated_at` 與 `expires_at` 必須帶 `+08:00`
- 空的 `signals` 陣列是合法檔案
- 檢查時刻晚於 `expires_at` 的訊號標成 `expired=True`，仍然載入
- JSON 損毀時丟出 `SignalFileError`；缺欄位或規則不符時丟出 `SignalValidationError`。訊息包含路徑，驗證錯誤會逐項列出欄位

上游多出來的欄位會略過。檔案裡如果自帶 `expired`，載入器會忽略並自行計算。

```python
from datetime import datetime, timedelta, timezone

from auto_trading.signals import load_signals

hkt = timezone(timedelta(hours=8))
document = load_signals(now=datetime(2026, 10, 1, 12, 0, tzinfo=hkt))
for signal in document.signals:
    if not signal.expired:
        print(signal.ticker, signal.market, signal.side, signal.confidence)
```

## 策略

策略是一份版本化 JSON。`spec_hash` 是 `sha256:` 加上規範化 JSON 的 SHA-256：鍵排序、無空白、拿掉頂層 `mode`。`mode` 只接受 `paper` 或 `disabled`；寫入其他值會被拒絕，也不會改變雜湊。改任何其他欄位都必須重新核准。核准紀錄由人寫入，引擎自己不會核准，也不會呼叫券商。生命週期停在模擬盤。`news_event` 現行版本是 v4（雜湊前綴 `9767e8571eea`），v2 與 v3 只留存檔，不能登記運行。

台股零股開關預設關閉（整張 1,000 股）。示範淨值 HKD 1,000,000、目標 5% 時，單筆上限先除以 1.02 的價格緩衝，2330.TW 約 2,445 元、匯率 0.244 時可買 82 股；這只是對照，開關預設仍是關閉。整張超過單筆上限時，若 `config/adr_map.csv` 有有效對照、ADR 自己的訊號是 BUY、而且換算後的理論價差距不超過 1%，才改走 ADR。風控上限讀執行端 `state/risk_limits.json`（`risk_limits/1.1`，`trading_scope` 必須是 `paper_only`），雜湊只覆蓋 `limits` 物件，並與 `config/risk_limits_expected.json` 核對（現行向量前綴 `02460f0f`，`tw_odd_lot_enabled` 為 false）。把 `trading_scope` 放進 `limits` 會得到另一個雜湊（前綴 `5bc6cf96`），視為不符。檔案過期、缺失、範圍不對或雜湊不符時，只產生減倉與出場意圖。

提醒只寫入 `alerts/pending/`。搬到 `sent/` 或 `failed/` 是執行端的工作，這個套件不會讀寫那兩個目錄。

回測的 R9 全停預設不重設淨值高點。至少經過 5 個交易日，而且回撤回到 −10% 以上，才恢復。報告同時列出重設高點與永不恢復兩個對照。

四份現行草稿用到的確認與出場條件都在日線迴圈裡計算；不認識的條件在驗證策略時直接失敗。整張台股買不進、且零股關閉時，回測會改試 ADR。`news_event` v4 接受 `status` 為 new 或 update，可見時間用 `judged_at`，並以去掉 `-u` 後綴的原始 `news_id` 去重。再沿該市場行事曆找 T0 與下一個進場日，假期會往後推。日線下載走 Yahoo chart，先讀本機 parquet，失敗會重試。樣本外切分、試驗登記與偏差歸因寫在回測報告裡。核准雜湊吻合且模式是模擬時，`StrategyEngine.run` 會寫意圖檔，不會直接下單。G1–G4 只產出模擬盤評估報告，不改變模式。下單只走風控閘門，再由模擬帳戶的 IBKR 轉接器送出。本系統沒有任何開關可以改接真實帳戶。

新策略要使用者先確認雜湊，然後用一個 PR 同時更新三處：`config/confirmed_strategies.json`（pin）、`strategies/registry.jsonl` 的 `event=approved`／`actor=user` 列，以及 `strategies/approved/` 裡的檔案。CI 檢查這三處的 id、版本和雜湊完全一致（`adr_premium_veto` 是規則，不是可交易策略，明確排除）。CI 通過、合併、再用 git 部署之後，啟動日誌裡的 hash12 必須和使用者確認的值相同。pin 檔只隨 git 部署，不在任何 rsync 清單裡。只在登記表加一行、或只在核准目錄放一個檔，開倉都會被 R25 拒絕。

## 設定與憑證

帳號與連線資訊只從行程環境變數讀取，不寫進原始碼，程式也不會自動載入 `.env`。

```bash
cp .env.example .env
# 只填非機密的連線設定後再匯出。登入名稱和密碼不得寫進 .env。
# VPS 用 systemd-creds。本機只經安全輸入放進程環境變數，不要先寫檔再改權限。
set -a
source .env
set +a
```

`.env` 已列入 `.gitignore`。`.env.example` 只有變數名稱，沒有登入密碼。密碼若先寫進檔案再改權限，仍會在那一瞬間以明文落地，所以啟動流程不採用這種寫法。

| 變數 | 用途 | 未設定時 |
| --- | --- | --- |
| `SIGNALS_PATH` | 訊號檔路徑 | `/workspace/invest/signals.json` |
| `DATABASE_PATH` | SQLite 檔案 | `data/auto_trading.sqlite` |
| `IB_HOST` | IB Gateway 位址。`IBKR_HOST` 是同一個設定 | `127.0.0.1` |
| `IB_PORT` | IB Gateway 埠。`IBKR_PORT` 是同一個設定 | `4002`（只接受模擬埠 4002 與 7497） |
| `IB_CLIENT_ID` | API client id。`IBKR_CLIENT_ID` 是同一個設定 | `1` |
| `IBKR_PAPER_ACCOUNT` | 必填的模擬帳戶釘選。看到的帳戶必須正好是這一個，否則 R0 拒絕、斷線並全停 | 無預設 |
| `TRADING_HOME` | 意圖、提醒、風控快照的根目錄 | `/workspace/trading` |
| `BASE_CURRENCY` | 基準幣別，`HKD` 或 `USD` | `HKD` |
| `TRADING_SYNC_REMOTE` | rsync 目標。未設定就不同步 | 無 |
| `IB_READ_ONLY_API` | `yes` 時下單迴圈不送單。E2 之前維持 yes，不是真實交易開關 | `yes` |
| `TRADING_GATEWAY_CONNECT` | 設為 `1` 時帳戶、看門狗和送單行程才連線到已登入的模擬 Gateway。未設定時測試和 CI 不開 socket | 不連線 |
| `TRADING_FX_PRECONVERT` | 設為 `1` 時，模擬盤買進 HKD／JPY（IdealPro）股票前先從 USD 換成目標幣。不夠或換匯未確認則 R31，不送股票單。不適用 TWD／KRW。未設定時港／日不預換 | 不預換 |
| `TWKR_AUTO_FX_VERIFIED` | 設為 `1` 時啟用台／韓模式 A：目標幣不足但 USD（及允許的 HKD）夠，就送股並依賴 IB 買股自動換匯，不送獨立 FX。未設定時該路徑以 `R31_TWKR_AUTO_FX_UNVERIFIED` 擋住 | 不放行 |

這個系統只做模擬。沒有環境變數或設定可以把連線改成真實帳戶；其他埠在載入設定時直接拒絕。模擬帳戶在每一項 R0 檢查都通過時，由唯一的批准函式自動批准；其餘情況一律人手批准，非模擬帳戶不能改成自動。唯一的批准開關是 `FORCE_MANUAL_APPROVAL`，設為 true 時全部改回人手。已刪除的自動批准名稱若仍出現在環境或設定裡，程式會拒絕啟動。關鍵事件會寫入 `logs/critical_*.jsonl` 和 `alerts/pending/`，再轉到通知 outbox。每日 06:00（澳門）產生成交摘要。連續異常（R29）和通知失效（R30）只暫停新開倉，出場和停損照常。模擬帳戶代碼必須是 `DU`、可選一個大寫字母、再接數字，而且 `IBKR_PAPER_ACCOUNT` 必填，看到的帳戶必須正好是這一個。不符合就寫下全停、斷開連線，並記一筆異常。交易程式不讀登入名稱和密碼，只連接已經登入的 Gateway（預設 `127.0.0.1:4002`）。IBC 範本和啟動檢查以 VPS 上的 `vps/deploy/render_template.py` 與 `vps/deploy/ibgw-start.sh` 為準，帳密由 systemd-creds 寫進 tmpfs。`ReadOnlyApi` 維持 yes，要等 E2 並經使用者同意才改。風控閘門是唯一會叫到券商 `place` 的路徑。新開倉會自檢 `state/risk_limits.json`（`risk_limits/1.1`），`trading_scope` 必須是 `paper_only`，而且雜湊必須和檔內 `limits` 以及核准向量一致。出場和保護性停損不因這份檔案不符而停住。全停會撤銷未成交的一般單、把 `strategy_runtime` 設為 halted，並寫入日誌。解除要在這台主機的互動終端輸入確認短語；R0 違規仍在時不能解除。只有 `--reset-hwm` 才會重設淨值高點，並寫入 `hwm_history`。信心度不足用 `LOW_CONFIDENCE`，不和 R19 混用。

交易日誌寫在 `TRADING_HOME/data/trading.db`（SQLite WAL），並追加 JSONL 與每月 CSV。`trade why` 由成交追到意圖、訊號和策略雜湊。送單迴圈每一輪把 Gateway executions 交給既有的 `record_fill`：寫成交、訂單改 `FILLED` 或 `PARTIALLY_FILLED`、`book_fill` 進策略持倉。同一個 execId 再讀一次不會重複記。OMS 讀 `state/executions.json`，不另開 socket。2026-10-16 那筆 SPY 的補記步驟在 `docs/backfill_spy_20261016.md`。

報價先問 IBKR，失敗才用本機已有的 Yahoo chart 當公開價，不另加資料套件。會改走公開價的情況是錯誤 10197、354、10167，或請求逾時。每筆報價記下來源、整數行情型別、資料型別、資料時間和收到時間。新鮮度一律用收到時間，超過 10 秒是 R22。送單前會重取收到時間超過 10 秒的代號。模擬盤接受型別 3 和 4；型別 2 拒絕。延遲報價的 `delay_sec` 來自 tick 45（`lastTimestamp`）或 tick 88（`delayedLastTimestamp`）；超過 1800 秒，或無法計算，是 R22，不會當成 0，並另記 `delayed_quote`。即時報價的資料落後超過 1800 秒記為 `QUOTE_DELAY_EXCEEDED`。公開價不能拿來下單。官方開盤價缺了就不成交，不再改用其他報價。收到時間記在 tick 回呼當下，而不是等行情逾時之後。收到時間舊過 10 秒時，新開倉、減倉和出場都拒絕；保護性停損不受這條限制。開倉的 `valid_from` 在風控閘門之前檢查：缺了、解析不了，或過了該節收市，就寫成終態並移出佇列。只有永久拒絕才會把開倉寫成終態並移出佇列，同一 `intent_id` 不再重試，也不會送到券商。永久拒絕是明確的允許清單：R25、`R0_FORBIDDEN_PORT`、`R0_NOT_PAPER_ACCOUNT`、`R0_ACCOUNTS_CHANGED`、`LIVE_NOT_AUTHORIZED`、數量或名目超過上限的 R1（單筆名目）、R2（單筆上限）、R3（持倉）、R4（市場）、R5（幣別）、R6（總曝險）和 `LOT_TOO_LARGE`（一手超過未縮放的單筆上限），以及 `INTENT_EXPIRED`。R7（現金不足）和 `R9_HALF_SIZE_LIMIT`（半倉期間的名目上限）不在清單裡，之後可能恢復，所以會重試。清單以外的閘門代碼一律留在佇列，下一輪再試，直到通過或該節收市到期。這包含開盤後 5 分鐘 `open_buffer` 的 R10、收盤前 10 分鐘的 R10、R22、R17 和 `ENTRIES_PAUSED`。出場和保護性停損不會因為任何閘門拒絕而變成終態，會一直重試；連續被擋三次就寫一筆 critical。午市、競價和休市的 R10 不計入這三次。保護性停損即使檔案裡帶著 `valid_from`，也不會在該節收市時移出佇列；引擎對保護性單不寫 `valid_from`。出場仍會在該節收市到期。風控持倉以券商數量為準，策略拆分讀日記帳 `strategy_positions`；`700` 對上 `0700.HK`。未成交單合併日記帳 working orders 和券商快照（含方向和數量），`orderRef` 寫進 `order_refs`。持倉市值是報價乘匯率。現金讀 `TotalCashValue`；這一行缺了就當未連線，不用淨值充當現金。匯率用帳戶 `ExchangeRate`（每單位外幣折合 HKD），沒有快照時用 `state/fx.json`，再沒有才用離線表（USD 7.8、JPY 0.052、KRW 0.0056、TWD 0.244）。非 HKD 缺匯率是 R17，不會當成 1。限價對到該價位帶的 tick 倍數，買進向下、賣出向上。沒有另加開盤後約 15 分鐘的延遲。引擎把新倉的 `valid_from` 設成該市場開盤時間，所以開倉會先碰到這 5 分鐘的 R10，開盤緩衝結束後的下一輪才送出。10197 一開始和恢復時各寫一筆 `alerts/pending/`，讓使用者知道手機登入佔用了行情。ADR 溢價暫停（R28）只擋模擬盤的買進開倉和加倉，出場和停損不擋；對照表壞掉時受控名單至少含 TSM。

`TRADING_FX_PRECONVERT=1` 時，模擬帳戶在買進 HKD、JPY 等 IdealPro 幣別股票前，先看 `cash_by_ccy`。短少就從 USD 換入目標幣（名目加佣金與 1.20% 緩衝，走 IdealPro），餘額確認後才送股票單。USD 不夠、餘額未知或換匯沒確認是 R31，不送股票單；R31 與 R7 一樣留在佇列重試。未設定這個變數時港／日不預換，也不套用這條 R31。啟用步驟在 `docs/fx_preconvert_enable_steps.md`。合併不會自動打開 `trading-submit.service` 的這個開關。

台股與韓股不走上面的預換，也不送 FXCONV、IdealPro 或 Convert Currency。目標幣現金夠就直接送股。不夠時，只有 `TWKR_AUTO_FX_VERIFIED=1` 且 USD（加上既有可動用的 HKD）蓋得住估計成本，才送股並依賴 IB 成交時自動換匯；否則 `R31_TWKR_AUTO_FX_UNVERIFIED` 或 `R7_BASE_CASH`，單留在佇列。成交後 TWD／KRW 現金為負只告警 `fx_target_cash_deficit` 並停新開倉，不用 API 清負。帳戶基準不是 USD 只寫 ops 警告，不再永久拒絕。

韓國股票代碼維持六位數字，幣別 `KRW`，交易所送 `KRX`。2026-10-06 用紙上帳戶 qualify `005930` / `KRX` / `KRW`，得到 conId 17382528，primaryExchange 為 `KRX`。SMART 也會解析到 `KRX`。`KSE` 回錯誤 200，合約對照不再送這個代碼。帳戶若沒有韓國股票權限（Client Portal → Settings → Trading Permissions → Stocks → Korea），qualify 仍可能是空的。韓國居民以及 IBIN、IBSJ 的客戶不能開這個權限。

## 開發與測試

需要 Python 3.12。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python scripts/check_paper_only.py
```

儀表板套件仍是選擇性依賴，這一版沒有介面。`ib_async` 只在真正連上 IB Gateway 時才載入；測試和 CI 用模擬客戶端，不會開網路連線：

```bash
python -m pip install -e ".[dashboard,ibkr]"
```

GitHub Actions（`.github/workflows/ci.yml`）會在 pull request 上執行 ruff、pytest，以及拒絕真實帳戶開關的掃描。

## 工具選擇

- **hatchling**：打包。套件放在 `src/auto_trading`，和測試目錄分開。
- **pydantic**：描述訊號 schema 1.0，並把缺欄位、錯誤的 side、confidence、時區收成可讀的驗證錯誤。
- **pydantic-settings**：設定只來自環境變數，`env_file` 關閉，空的環境變數沿用預設值。
- **ruff**：同時負責 lint 與格式，CI 只裝一套工具。
- **pytest**：訊號檔的有效、空白、損壞、過期，以及策略雜湊與風控閘門都有測試。
- **SQLite**（標準庫）：本機持倉與日誌，包在 repository 後面，之後可以換實作而不改呼叫端。
- **Plotly**（extra `dashboard`）：之後的持倉、損益、訊號圖表。
- **ib_async**（extra `ibkr`）：之後連線 IB Gateway。佔位模組目前不會匯入它。
