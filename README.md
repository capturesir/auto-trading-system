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

這一版實作了設定、訊號載入與驗證、策略雜湊核准，以及「執行器必須先經過風控」這條路徑。行情、指標、回測、風控規則、IBKR 連線、SQLite 結構、警示、報告與儀表板仍是佔位，匯入時不會連線到券商。

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

策略是一份版本化 JSON。`spec_hash` 是排序後 JSON 的 SHA-256。引擎只接受雜湊、`id` 與 `version` 都和已核准紀錄一致的文件；改任何欄位（包含把 `mode` 改成 `live`）都會變成另一份文件，必須重新核准。核准紀錄由人寫入，引擎自己不會核准，也不會呼叫券商。

## 設定與憑證

帳號與連線資訊只從行程環境變數讀取，不寫進原始碼，程式也不會自動載入 `.env`。

```bash
cp .env.example .env
# 在本機填值後再匯出。空值會被忽略，沿用程式內建預設。
set -a
source .env
set +a
```

`.env` 已列入 `.gitignore`。`.env.example` 只有變數名稱。

| 變數 | 用途 | 未設定時 |
| --- | --- | --- |
| `SIGNALS_PATH` | 訊號檔路徑 | `/workspace/invest/signals.json` |
| `DATABASE_PATH` | SQLite 檔案 | `data/auto_trading.sqlite` |
| `IBKR_HOST` | IB Gateway 位址 | `127.0.0.1` |
| `IBKR_PORT` | IB Gateway 埠 | `4002`（Gateway 模擬帳戶常用埠） |
| `IBKR_CLIENT_ID` | API client id | `1` |
| `IBKR_ACCOUNT` | 帳戶代碼 | 空字串 |
| `IBKR_PAPER` | 是否為模擬帳戶 | `true` |

預設是模擬帳戶。要改接正式帳戶時，須明確把 `IBKR_PAPER` 設為 `false`。

持倉、損益與交易日誌會經 repository 介面寫入本機 SQLite。資料表尚未建立。

## 開發與測試

需要 Python 3.12。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
```

儀表板與 IBKR 連線的套件是選擇性依賴，這一版還沒有用到：

```bash
python -m pip install -e ".[dashboard,ibkr]"
```

GitHub Actions（`.github/workflows/ci.yml`）會在 pull request 上執行 ruff 與 pytest。

## 工具選擇

- **hatchling**：打包。套件放在 `src/auto_trading`，和測試目錄分開。
- **pydantic**：描述訊號 schema 1.0，並把缺欄位、錯誤的 side、confidence、時區收成可讀的驗證錯誤。
- **pydantic-settings**：設定只來自環境變數，`env_file` 關閉，空的環境變數沿用預設值。
- **ruff**：同時負責 lint 與格式，CI 只裝一套工具。
- **pytest**：訊號檔的有效、空白、損壞、過期，以及策略雜湊與風控閘門都有測試。
- **SQLite**（標準庫）：本機持倉與日誌，包在 repository 後面，之後可以換實作而不改呼叫端。
- **Plotly**（extra `dashboard`）：之後的持倉、損益、訊號圖表。
- **ib_async**（extra `ibkr`）：之後連線 IB Gateway。佔位模組目前不會匯入它。
