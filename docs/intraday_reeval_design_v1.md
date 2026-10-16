# 盤中再評估（scheme B，paper DUR243736）

日期：2026-10-16（澳門）。只限模擬帳戶 `DUR243736`。風控、KILL_SWITCH、paused、R9 停新倉都不改。A 組訊號仍是日更。07:05 的 `trading-strategy.timer` 仍是唯一的全日 runner。

所謂盤中達標，是把即時 last 當成當日未收市 bar 的 provisional close，再跑同一套日線 confirm。沒有另寫分鐘線策略。

## 管線

| 層 | 節奏 | 職責 |
|---|---|---|
| A 組 signals | 日更 | 估值、信心、方向 |
| `trading-strategy.timer` | 07:05 澳門 | signals 加日線，寫 intents，並寫 watchlist |
| `trading-intraday-reeval.timer` | RTH 內每 2 分鐘（可改 1–5 分） | 重跑 confirm；旗標打開才追加 intents |
| `trading-submit` | 約 10 秒 | 讀 intents，過風控，送模擬單 |
| holdings-monitor | 30 秒 | 只發持倉提醒。不寫 intents，不下單 |

## Scheme B 包含的階段

1. Phase 0。07:05 把「trigger 已過、confirm 未過」寫進 `state/intraday_watchlist.json`，並記下哪個指標還差多少。`INTRADAY_REEVAL_INCLUDE_VALID_BUYS=1` 時，當日仍有效、confirm 已過的 BUY 也留在名單裡。不因此寫新 intents。
2. Phase 1。`trading-intraday-reeval` 只讀 watchlist、報價（client id 16）和日線 cache，寫 `state/intraday_reeval.json`。`would_entry` 表示 provisional confirm 已過。
3. Phase 2。`INTRADAY_REEVAL_WRITE_INTENTS=1` 且去抖允許時，用同一個 `plan_session` 追加 ENTRY，revision 加一。Submit 下一輪自然送出。意圖列帶 `source=intraday_reeval`。旗標預設 0。
4. Phase 3。holdings-monitor 只做提醒。已持倉用同一套策略出場規則；`INTRADAY_REEVAL_WRITE_EXITS=1` 才寫 EXIT 或 REDUCE。旗標預設 0。

同一 `strategy_id|ticker|session` 最多一張新 ENTRY。完整 EXIT 同一鍵也只寫一次；同一個出場原因不重複。REDUCE（指標止盈）之後仍可再寫一次不同原因的 EXIT。

非 `DUR243736`、其他 `DU*`、或 `mode` 不是 paper，服務拒絕寫 intents。

明確不做：在 holdings-monitor 裡寫 intents 或下單；第二套 07:05 runner；放寬 risk limits；每 10 秒掃全宇宙。
