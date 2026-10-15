# research/universe：`ta_pool` 股票池產生器（ta_breakout52 v1）

依據：策略規格 v0.9 §16.2（`universe.source = ta_pool`）、§3.4（規範化雜湊）；執行規格 v0.8 §3.1 第 5 點（`trigger_ref` 核對：`data/universe/archive/<pool_id>.csv` 的 sha256 = `pool_hash`）。規格 v0.9 只說由 B 組 `pool_builder` 產生，沒有指定程式位置，所以放在這裏。

## 檔案
| 檔案 | 作用 |
|---|---|
| `ta_pool.py` | 函式庫：成分解析、價格清洗與指標、篩選、規範化 CSV、雜湊、不可覆寫存檔 |
| `build_pool.py` | 產生 `data/universe/ta_pool.csv`、`ta_pool.meta.json`、`archive/ta_pool_<YYYYMM>.csv` |
| `verify_pool.py` | 由存檔重算 `pool_hash`，核對 meta、`ta_pool.csv`、（可選）`--expect`，並重算 `filters_hash` 對策略檔；退出碼 0／1 |
| `fetch_sources.py` | 每月刷新來源與 yfinance 價格快取（2026-10 這一期**沒有跑**，見下） |
| `tests/test_ta_pool.py` | 18 個測試：雜湊決定性、CSV 格式、§3.4 向量、篩選邊界（2330.TW 整張超上限、HK 每手、2% 緩衝邊界、門檻含等號、缺數據）、存檔不可覆寫、實檔重建一致 |

## 用法（venv：`research/adr_premium/.venv/bin/python`）
```
python build_pool.py                 # 預設用 research/opportunity_ta 的來源和價格快取，NLV HKD 7.8M
python verify_pool.py                # 或 python verify_pool.py data/universe/archive/ta_pool_202610.csv --expect sha256:...
python -m unittest discover -s tests -v
```
每月第一個營業日（澳門 06:00 前）：`python fetch_sources.py && python build_pool.py --raw cache/<YYYYMM>/raw --px cache/<YYYYMM>/px`。

## 演算法
1. **成分**（`pool.base_indices`）：US = S&P 500 ∪ Nasdaq-100；JP = 日經 225；HK = 恒指 ∪ 國企；TW = 台灣 50；KR = KOSPI 200。解析與 `research/opportunity_ta/universe.py` 相同；同一 ticker 合併，`index_membership` = 指數代號 ASCII 排序後以 `+` 連接（代號照策略檔：SPX、NDX、N225、HSI、HSCEI、TW50、KOSPI200）。ticker 用 Yahoo 格式（規格 §2 標的對照表的主鍵）。
2. **每手**：US 1、JP 100、KR 1、TW 1000（只買整張，零股開關 false）、HK = HKEX `ListOfSecurities.xlsx` 的 Board Lot（Equity、REIT）。
3. **價格清洗**（同 `opportunity_ta/prep.py`）：只用 ≤ asof、volume > 0、close > 0、OHLC 齊全的行；市場 session = 當日有成交的成分 ≥ 過去 250 日最大值的 30%；單日 ±60% 而次日反轉 > 35% 的點設為缺失。
4. **指標**：`close_local` = 最後有效未還原收市價；`adv60_base` = 60 個 session 的 (close × volume × 匯率→HKD) 平均（至少 40 個有效值）；`history_sessions` = 有效 session 數；匯率同 prep（Yahoo `HKD=X` 等，偏離 21 日中位數 > 5% 的點用前值）。
5. **篩選**（數值讀自策略檔 `universe.filters`）：`pass_adv` = adv60 ≥ 門檻；`pass_px` = close ≥ 門檻；`pass_lot` = 每手 × close × 匯率 × (1 + 2%) ≤ `min(R1 × NLV, R2)`（R1、R2 讀自 `config/risk_limits_expected.json`，NLV 預設 HKD 7.8M → 上限 HKD 200,000）；另需 `history_sessions` ≥ 260、最後成交距 asof ≤ 10 個 session、有每手股數。全部門檻含等號。`in_pool` = 全部通過；`excluded_reason` 以 `;` 連接，次序固定：`no_price_data; stale_price; no_lot_size; history; adv; price; lot`。
6. **CSV 規範**（決定位元組）：UTF-8 無 BOM、LF、逗號、表頭照 §16.2 欄位順序、最後一行後有 LF、不加引號（欄位含逗號／引號／換行即報錯）；列按 market（US, JP, HK, TW, KR）再按 ticker 的 ASCII 升序；`close_local` 4 位小數，`lot_cost_base`、`adv60_base` 2 位小數（HKD），整數欄位為整數，布林 `true`／`false`，缺值為空字串；`asof` 每列相同（YYYY-MM-DD）。
7. **`pool_hash`** = `"sha256:" + sha256(CSV 檔位元組).hexdigest()`（小寫）。`ta_pool.csv` 與存檔逐位元組相同。**`filters_hash`** = §3.4 規範化（`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`，UTF-8）的 sha256，同一前綴。
8. **`pool_id`** = `ta_pool_<YYYYMM>`，YYYYMM = 刷新月份（執行時澳門月份，可用 `--pool-month` 指定），不是 asof 的月份（月初 06:00 重建時 asof 仍是上月最後一個 session）。
9. **寫檔**：存檔以 `O_EXCL` 建立、權限 0444；已存在且內容不同 → 拒絕（內容相同 → 不動）；之後原子寫 `ta_pool.csv`、`ta_pool.meta.json`。

## 2026-10 這一期
- 用 `research/opportunity_ta` 2026-10-06 的快取（成分來源 2026-10-06 21:22 HKT 擷取；價格到 2026-10-05，KR 到 2026-10-02），asof 2026-10-05；在 2026-10-08 補建（不是月初）。
- 與 ta_breakout52 回測用的股票池逐檔比對：只差 5 檔上市不足 260 session 的新股（回測股票池沒有套用這條），adv60 與回測面板相同（相對差 < 1e-9）。
