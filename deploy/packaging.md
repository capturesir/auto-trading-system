# 打包清單

這份清單說明「從零部署包」裡有什麼、沒有什麼。教學在 `deploy/from-scratch.md`。安裝器是 `scripts/ats-install.sh`。

以倉庫裡的程式、`vps/deploy/` 單元和 `.env.example` 為準。外部遷移筆記若與倉庫衝突，以倉庫為準。這份包裝不要求、也不會改你現在正在跑的那台模擬機。

## 倉庫裡有

| 路徑 | 作用 |
| --- | --- |
| `scripts/ats-install.sh` | 可審閱的安裝器。檢查先決條件、必要時安裝 Docker、寫本機 env、啟動 Compose、`status` / `doctor` |
| `deploy/from-scratch.md` | 繁體中文從零教學（正文） |
| `deploy/from-scratch.css` | 產生 PDF 時用的字型設定 |
| `deploy/from-scratch.pdf` | 由教學產生的 PDF。可用 `docs-pdf` 重生 |
| `deploy/packaging.md` | 本清單 |
| `deploy/signals.example.json` | 空白但格式合法的訊號檔。安裝器只在資料目錄還沒有訊號檔時複製它 |
| `deploy/compose/docker-compose.yml` | Python 長駐服務與計時行程。不發佈埠 |
| `deploy/compose/Dockerfile` | 應用映像。不含券商登入 |
| `deploy/compose/entrypoint.sh` | 以資料目錄擁有者的 uid 跑行程 |
| `deploy/compose/timer-loop.sh` | 對應現有 timer 的時鐘 |
| `deploy/compose/systemctl-host.sh` | 看門狗專用。只轉送「重啟 ibgateway」 |
| `deploy/host/ibgateway.service` | 宿主 Gateway 單元。呼叫現有 `vps/deploy/ibgw-start.sh` |
| `deploy/host/ibkr-port-guard.service` | 宿主封包過濾單元 |
| `deploy/host/ibkr-port-guard.sh` | 丟掉非 loopback 進來的券商 API 封包 |
| `deploy/host/ibgw-set-credentials.sh` | 用 systemd-creds 加密登入。密碼不進參數 |
| `.env.example` | 只有變數名，值是空的 |
| `.dockerignore` | 建映像時排除 git、本機 env、資料庫 |
| `tests/test_deploy_package.py` | 語法、doctor、Compose 約束。不連券商 |
| `vps/deploy/ibgw-start.sh` | 既有 Gateway 啟動腳本，安裝器複製到宿主 |
| `vps/deploy/render_template.py` | 既有 IBC 範本產生器。範本不含密碼 |
| `vps/deploy/*.service` 與 `*.timer` | 既有單元，留給現機。新機器改走 Compose，不要求你改現機 |

Compose 預設會起的服務：`account`、`oms`、`recon`、`watchdog`、`submit`、`strategy`、`summary`、`pool`、`sync`。

預設不起、要另加 `--with-optional` 才起：`holdings`、`reeval`。

沒有 `trading-main`。沒有 Gateway 容器。沒有把 4002 映射到主機以外。

## 安裝之後才出現在機器上、不進 git

- `deploy/compose/.env`：帳戶號碼、資料目錄、uid。由安裝器產生
- `/srv/trading-data/`（或你指定的資料目錄）：`state/`、`data/`、`logs/`、`intents/`、`reports/`、`alerts/`、`backups/`、訊號檔
- `/etc/credstore.encrypted/ibkr-paper-username` 與 `ibkr-paper-password`：加密後的模擬登入
- `/etc/ibc/config.paper.ini.template`：在裝好 IBC 之後才產生
- `/opt/ibgateway`、`/opt/ibc`、`/var/lib/ibgw`：你下載並安裝的 Gateway 與 IBC
- `/etc/sudoers.d/trader-ibgateway`：只允許 `trader` 重啟 `ibgateway.service`

## 不在倉庫裡

這些東西不要補進 git，教學也不提供範例值。

- 密碼、權杖、SSH 私鑰、API 金鑰
- Healthchecks 網址全文。變數名 `HEALTHCHECK_PING_URL` 在 `.env.example` 留空。倉庫沒有單獨的定時 ping 單元
- 物件儲存備份帳號。倉庫沒有對應單元，本包裝不包含
- 現機的 `paper_home` 狀態：帳戶快照、日記帳、意圖、日誌、報告
- A 組投資研究資料。B 組只吃 `signals.json`
- Cloudflare 帳號、Tailscale 帳號、節點金鑰、指紋
- 現機公網位址、雲端主機編號、現用儀表板密碼
- 填好的 `.env`
- 真實帳戶登入，以及任何會打開真實帳戶的開關

儀表板套件還沒有可執行的網頁。`auto_trading.dashboard.render_dashboard` 會直接拒絕。Compose 因此沒有儀表板服務，也沒有儀表板密碼。

下單路徑不呼叫語言模型。沒有模型金鑰要打包。訊號怎麼產生是訊號檔上游的事，見教學的「訊號」一節。

## 和現有單元對齊、刻意不改的預設

- 只做模擬。Compose 把 `IB_HOST` 釘成 `127.0.0.1`、`IB_PORT` 釘成 `4002`
- `submit` 的 `IB_READ_ONLY_API` 預設 `yes`，和 `trading-submit.service` 一樣
- 不寫入 `TRADING_FX_PRECONVERT`、`TWKR_AUTO_FX_VERIFIED`。要開請另讀 `docs/fx_preconvert_enable_steps.md`
- 盤中重評的兩個寫入開關預設 `0`
- 看門狗仍只會要求重啟 `ibgateway.service`
- Gateway 維持宿主上的 IBC 與 systemd-creds。不把社群 Gateway 映像當成必經步驟
- 安裝器安裝 Gateway 單元，但不啟動它。同一個模擬登入不能兩處同時在線
