---
title: 從零部署（模擬帳戶）
lang: zh-Hant
---

# 從零部署（模擬帳戶）

這份教學假設你是第一次在一台新電腦，或一台剛裝好的 Linux 上，把 B 組交易系統跑起來。讀者不需要寫過程式。請用繁體中文對照下面的命令；命令本身是英文，請整行複製。

系統只做模擬。它連到 Interactive Brokers 的模擬 Gateway，不會提供真實帳戶開關。

正在跑模擬的那一台舊機器不要執行這裡的 `install`。這份包裝是給新環境用的。

包裝裡有什麼、沒有什麼，見 `deploy/packaging.md`。

## 最短命令

在 Ubuntu 24.04（或其他同代的 Ubuntu）上，從空機器到 Compose 服務起來：

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates curl
git clone https://github.com/capturesir/auto-trading-system.git
cd auto-trading-system
sudo ./scripts/ats-install.sh install --yes --paper-account 'DU1234567'
./scripts/ats-install.sh status
sudo ./scripts/ats-install.sh doctor
```

把 `DU1234567` 換成你自己的模擬帳戶號碼。格式是 `DU`、可以再加一個大寫英文字母、然後是數字。`DU1234567` 只是形狀範例，不是一個可用帳戶。

上面這組命令會裝 Docker、寫本機設定、建立資料目錄、啟動 Python 服務。它不會寫入密碼，也不會啟動 IB Gateway。Gateway 要等你確認沒有另一處還登著同一個模擬帳戶，再做後面的「啟動 Gateway」。

不想把號碼放在命令列時，用互動安裝：

```bash
sudo ./scripts/ats-install.sh install
```

它會問帳戶號碼。密碼仍然要另外用 `gateway-credentials` 輸入，不會在這一步出現。

## 這幾個詞是什麼

**Docker 映像**把程式和它需要的函式庫包在一起。映像跑起來叫做容器。交易程式在容器裡；會變的資料放在主機資料夾，容器只是把資料夾掛進去讀寫。換電腦時，保留資料夾，並在新機器上重新建立映像。

**Compose**是一份清單，寫明要同時跑哪些容器。`deploy/compose/docker-compose.yml` 就是這份清單。一條命令會依照清單啟動。

**環境變數**是交給程式的設定，例如帳戶號碼和連接埠。程式只看行程環境，不會自己去讀一個叫 `.env` 的檔。Compose 會把本機那份 git 不收的 `deploy/compose/.env` 注入容器。倉庫裡的 `.env.example` 只有變數名，值是空的。

**密鑰**是密碼、權杖、私鑰這一類不能給別人看的東西。模擬登入的密碼用 systemd 的加密憑證存放。它不進 git、不進 Compose、不進這份教學的範例。

**Gateway**是 Interactive Brokers 的本機程式。交易系統只連已經登入的 Gateway，自己不保存登入密碼。這裡用的連接埠是 `127.0.0.1:4002`。`127.0.0.1` 的意思是「只在這台機器內部」。Compose 不把這個埠公布到機器外面。

**port-guard**是宿主上的一道封包規則：從網路卡進來、目標是券商 API 埠的封包會被丟掉。本機自己的程式仍走 loopback，不受影響。

## 你要先準備

- 一台新的 Ubuntu 24.04，或很接近的 Ubuntu。可以是家裡的 Linux 電腦，也可以是新租的 VPS。記憶體建議 4 GB 以上，因為 Gateway 本身要用記憶體。這不是安裝器的硬性門檻。
- 一個 Interactive Brokers **模擬**帳戶，以及它的登入名稱和密碼。密碼只在那台新機器上輸入。
- 能用 `sudo`。
- 能上網，第一次建立映像要下載 Python 套件。

不需要先註冊 Cloudflare 或 Tailscale。那兩項是可選的，放在文末。

## 路徑甲：家裡或辦公室的 Linux

和下面的「路徑乙」用同一組安裝命令。差別是你可以先不做防火牆、Tailscale 和 Cloudflare。

仍建議讓安裝器裝上 port-guard，避免區網裡的其他人連到券商埠。不要把 4002 轉發到路由器的公網。

資料會放在這台電腦的 `/srv/trading-data`（可用 `--data-root` 改）。請挑一個有空間、會備份的磁碟。

## 路徑乙：新租的 VPS

1. 用供應商給的方式，以你自己的 SSH **公鑰**登入。私鑰只留在你原來的電腦，不要複製進這個倉庫，不要複製進 Compose。
2. 確認系統是 Ubuntu 24.04 這一類。
3. 執行上面的最短命令。安裝器會把時區設成 UTC，並啟動 chrony。Gateway 啟動腳本裡的自動重啟時間是依 UTC 寫的（`11:30 AM` 對應澳門 19:30）。時區若改成別的，重啟時點會漂。
4. 雲端防火牆不要放行券商 API 埠。網頁埠也先不要開：這套包裝沒有公開網頁。
5. SSH 建議先保持你登得進去。等 Tailscale 確認可用，再考慮關掉公網 22。安裝器不會替你關 SSH，避免把你鎖在外面。

供應商後台的主機編號、公網位址、金鑰指紋不要寫進 git，也不要寫進這份教學的副本。

## 安裝器做了什麼

`install` 會：

1. 安裝 git、curl、Python 3、iptables、chrony。沒有略過 Gateway 時，一併安裝 Xvfb 等 Gateway 會用到的函式庫。
2. 若還沒有 Docker Engine 和 Compose 外掛，用 Docker 官方的 apt 來源安裝。命令寫在 `scripts/ats-install.sh` 的 `install_docker`，請先打開檔案看過，不要改走「把別人網址的腳本直接灌進 shell」。
3. 建立系統使用者 `trader` 和 `ibgw`。
4. 建立資料目錄，並在還沒有訊號檔時放入一份空白的 `signals.json`。
5. 寫入 `deploy/compose/.env`。這裡有帳戶號碼，沒有密碼。檔案權限是 `0640`。
6. 把 Gateway 單元和 port-guard 裝到 systemd，並啟動 port-guard。**不**啟動 Gateway。
7. 建立映像並啟動 Compose。

只想看它會做什麼、先不改機器：

```bash
./scripts/ats-install.sh install --dry-run --paper-account 'DU1234567'
```

資料目錄不要放在 git 工作目錄裡面。預設 `/srv/trading-data` 是分開的。容器內部仍使用 `/srv/trading`，所以程式看到的路徑和現有單元檔一致。

| 主機目錄 | 容器裡 | 內容 |
| --- | --- | --- |
| `/srv/trading-data/state` | `/srv/trading/state` | 帳戶快照、對帳、看門狗 |
| `/srv/trading-data/data` | `/srv/trading/data` | 日記帳、股票池、K 線快取 |
| `/srv/trading-data/intents` | `/srv/trading/intents` | 策略寫出的意圖 |
| `/srv/trading-data/logs` | `/srv/trading/logs` | 日誌 |
| `/srv/trading-data/reports` | `/srv/trading/reports` | 摘要 |
| `/srv/trading-data/alerts` | `/srv/trading/alerts` | 提醒 |
| `/srv/trading-data/signals.json` | `/srv/trading/signals.json` | 當日訊號 |
| `/srv/trading-data/backups` | `/srv/trading/backups` | 你自己留的備份 |

映像裡的程式、核准策略和 `config/` 不在這些資料夾裡。更新策略的方式是 `git pull` 之後重新建立映像，不是改資料目錄。

## 環境變數怎麼放才安全

倉庫只提交 `.env.example`。名稱可以看，值不要填進 git。

安裝器寫進本機 `deploy/compose/.env` 的是：

- `IB_HOST=127.0.0.1`
- `IB_PORT=4002`
- `IBKR_PAPER_ACCOUNT`（你的模擬帳戶）
- `TRADING_HOME`、`SIGNALS_PATH`、`BASE_CURRENCY=HKD`
- `IB_READ_ONLY_API=yes`
- `ATS_DATA_ROOT`、`ATS_UID`、`ATS_GID`、可能還有 `CHRONY_GID`

Compose 再把容器裡的 `IB_HOST` 和 `IB_PORT` 釘死。就算有人改了 `.env` 裡的位址，清單仍要求容器連 `127.0.0.1:4002`。

預設不寫入這些名稱，行為就和現有單元檔一樣維持關閉：

- `TRADING_FX_PRECONVERT`
- `TWKR_AUTO_FX_VERIFIED`
- `TRADING_SYNC_REMOTE`
- `HEALTHCHECK_PING_URL`

`IB_READ_ONLY_API=yes` 時，送單行程不會送出新單。這是 `trading-submit.service` 的預設，安裝器不會改成 `no`。將來你要自己改，只改這台機器的 `.env`，然後 `sudo ./scripts/ats-install.sh up`。不要把改過的檔提交。

換匯開關的說明在 `docs/fx_preconvert_enable_steps.md`。不在安裝步驟裡打開。

`doctor` 會檢查帳戶格式和埠，不會登入券商，也不會打開 4002 去連線。

## 啟動 Gateway

Gateway 裝在宿主，不裝在 Docker。請使用與 `vps/deploy/ibgw-start.sh` 裡 `TWS_MAJOR_VRSN=1050` 相符的 Gateway 10.50 系列，以及能驅動它的 IBC。

官方下載頁（請自己核對版本與授權條款）：

- Gateway：<https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>
- IBC：<https://github.com/IbcAlpha/IBC/releases>

把安裝檔下載到 `/root` 這類 git 以外的地方。不要把安裝檔提交。若你已有檔案，可以：

```bash
sudo ATS_IBGATEWAY_SH=/root/ibgateway-installer.sh \
  ATS_IBC_ZIP=/root/IBCLinux.zip \
  ./scripts/ats-install.sh gateway-install
```

安靜安裝若失敗，就在這台機器上互動執行 Gateway 安裝檔，目錄選 `/opt/ibgateway`。IBC 解壓後要能看到 `/opt/ibc/scripts/ibcstart.sh` 和 `/opt/ibc/config.ini.sample`。然後再執行：

```bash
sudo ./scripts/ats-install.sh gateway-units
```

這會產生不含密碼的 `/etc/ibc/config.paper.ini.template`，並重新套用 port-guard。

### 寫入密碼

互動（密碼不會顯示在畫面上）：

```bash
sudo ./scripts/ats-install.sh gateway-credentials
```

非互動時，先用 root 建立權限 `600` 的檔案，再用編輯器寫入，不要把密碼放在命令列或 shell 歷史：

```bash
sudo install -m 0600 /dev/null /root/ib-user
sudo install -m 0600 /dev/null /root/ib-pass
sudoedit /root/ib-user
sudoedit /root/ib-pass
sudo ATS_IB_USERNAME_FILE=/root/ib-user ATS_IB_PASSWORD_FILE=/root/ib-pass \
  ./scripts/ats-install.sh gateway-credentials --yes
```

這兩個檔必須在 git 工作目錄之外，擁有者是 root。腳本用 `systemd-creds` 加密到 `/etc/credstore.encrypted/`。服務啟動時才解密到記憶體檔案系統，停止就消失。加密檔可以留在機器上，不要複製進倉庫。

寫完憑證之後，腳本仍不會啟動 Gateway。

### 確認沒有第二處登入，再啟動

同一個模擬登入同一時間只能有一處 Gateway。若舊機器還登著，先在舊機器停下 Gateway，再在新機器執行：

```bash
sudo systemctl enable --now ibgateway.service
sudo systemctl status ibgateway.service --no-pager
sudo journalctl -u ibgateway.service -n 80 --no-pager
```

日誌裡不應該出現你的密碼。啟動腳本會拒絕範本被改掉：必須是模擬模式、API 唯讀、埠 4002、拒絕外來連線。

看門狗若連續三次覺得 Gateway 沒登入，會執行和現有單元相同的命令：`sudo systemctl restart ibgateway.service`。在 Compose 裡，只有 `watchdog` 這個容器能轉送這一件事。它看得到宿主的 systemd，而且轉送腳本拒絕其他命令。其餘容器沒有這個能力。`trader` 的 sudo 也只允許這一行。

## 訊號

交易下單這一段不呼叫語言模型，也不需要模型 API 金鑰。它讀 `signals.json`。研究團隊（A 組）如何寫出這份檔，不在這個倉庫。以後若更換產生訊號的模型或供應商，只要檔案仍是 schema `1.0`，B 組這套部署不用改。

空白範例在 `deploy/signals.example.json`。安裝器只在目標檔還不存在時複製它。空的 `signals` 陣列是合法檔，不會因此下單。

### 放入或更新

1. 向研究端拿到 UTF-8 的 JSON。
2. 覆蓋資料目錄裡的檔，預設是 `/srv/trading-data/signals.json`。不要提交這份填好的檔。
3. 檢查：

```bash
./scripts/ats-install.sh signals-check
```

4. 策略計時在每天澳門時間 07:05 讀取（單元上是 23:05 UTC）。想立刻跑一次：

```bash
sudo ./scripts/ats-install.sh strategy-once
```

這一次會依訊號寫意圖，不會因為這條命令去登入券商。送單仍要等 Gateway、風控，以及你將來自己把唯讀開關打開。

頂層要有 `schema_version`（固定 `"1.0"`）、`generated_at`（須帶 `+08:00`）、`generated_by`、`price_date`、`signals`。每一列的欄位與規則見倉庫 `README.md` 的「訊號檔」一節。多出來的欄位會被略過。

## 服務對照

| Compose | 對應單元 | client id | 預設啟動 |
| --- | --- | --- | --- |
| `account` | `trading-account.service` | 13 | 是 |
| `oms` | `trading-oms.service` | 不連線 | 是 |
| `recon` | `trading-recon.service` | 12 | 是 |
| `watchdog` | `trading-watchdog.service` | 14 | 是 |
| `submit` | `trading-submit.service` | 11 | 是 |
| `strategy` | `trading-strategy.timer` | 不連線 | 是，每天 23:05 UTC |
| `summary` | `trading-summary.timer` | 不連線 | 是，每天澳門 06:00 |
| `pool` | `pool-rebuild.timer` | 不連線 | 是，每天香港 06:00 |
| `sync` | `trading-sync.timer` | 不連線 | 是，每 30 秒；沒有遠端目標就立刻結束 |
| `holdings` | `trading-holdings-monitor.service` | 15 | 否 |
| `reeval` | `trading-intraday-reeval.timer` | 16 | 否，每 2 分鐘；兩個寫入開關是 0 |
| （沒有） | `trading-main.service` | 不要用 | 禁止。它會和上面的行程搶 client id |

`recon` 讀 `state/account.json`，不自己開券商連線，這和現有單元一致。`oms` 也不開連線。

`holdings` 和 `reeval` 只接受程式裡那個 `PAPER_ACCOUNT` 常數（`src/auto_trading/alerts/holdings_monitor.py`、`src/auto_trading/intraday/reeval.py`）。你的模擬帳戶若不是那一個，不要加 `--with-optional`。核心五個長駐服務接受任何格式正確、且和 Gateway 看到的帳戶相同的模擬帳戶。

第一次安裝，每日計時會等到下一個時點，不會在安裝當下補跑昨天。容器若在時點之後才起來，而且以前跑過，會補跑一次。

同步預設沒有 `TRADING_SYNC_REMOTE`，所以不會連到別的電腦。不要為了同步把 SSH 私鑰打進映像。真的要同步時，再在本機 `.env` 寫目標，並另外掛上金鑰；金鑰檔不要放進 git。

## 日常命令

```bash
./scripts/ats-install.sh status
sudo ./scripts/ats-install.sh doctor
sudo ./scripts/ats-install.sh up
sudo ./scripts/ats-install.sh down
```

`down` 只停 Compose。資料目錄還在。Gateway 要另外停：

```bash
sudo systemctl stop ibgateway.service
```

更新程式（在 git 工作目錄）：

```bash
git pull
sudo ./scripts/ats-install.sh up
```

`up` 會重新建立映像。資料目錄裡的狀態會保留。核准策略跟著映像走，所以更新策略也是這兩行，而不是只改資料夾。

股票池若要改回倉庫裡的那一份：

```bash
sudo ./scripts/ats-install.sh seed-data --force-seed
```

沒有 `--force-seed` 時，已有的股票池不會被蓋掉。

## 可選：Cloudflare

這套包裝沒有網頁伺服器，也沒有儀表板行程。現在不需要 Cloudflare，也不要建立任何金鑰檔。

若你以後自己加了只聽本機的網頁，再考慮：

- 用你的網域，在 Cloudflare 開 Proxied 記錄，指到這台新機器的公網位址
- 只放行你實際使用的網頁埠
- 不要把券商 API 埠寫進 DNS，也不要在防火牆放行

帳號、權杖、現有機器的位址都不要寫進倉庫。

## 可選：Tailscale

Tailscale 是私人網路，用來遠端登入，這樣就不必把 SSH 開給整個網際網路。安裝器不會自動安裝它，因為那需要你的帳號。

請先打開下面兩個網址，確認內容是套件庫金鑰和來源清單，再執行。不要把未經閱讀的安裝腳本直接灌進 shell。

- <https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg>
- <https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list>

```bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update
sudo apt-get install -y tailscale
sudo tailscale up
```

`tailscale up` 會請你在瀏覽器登入。若使用 auth key，只放在這台機器上，不要寫進命令歷史、不要寫進 git、不要貼進聊天。

確認你能用私人位址 SSH 之後，才考慮關掉公網 22。節點金鑰要不要過期，在 Tailscale 的管理頁設定。

## 可選：防火牆

安裝器不會執行 `ufw`，避免把你鎖在外面。你自己決定要開時，在 **新** 機器上：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

不要放行 4002。port-guard 是另一層：就算防火牆設錯，從外面進來的券商 API 封包仍應被丟掉。本機服務走 `127.0.0.1`，不受這條規則阻擋。

若改成只允許 Tailscale 介面上的 SSH，請先確認私人網路已經連上。

## 重生 PDF

正文是這份 Markdown。已安裝 pandoc，且有 weasyprint 或 xelatex 時：

```bash
sudo apt-get install -y pandoc weasyprint fonts-noto-cjk
./scripts/ats-install.sh docs-pdf
```

輸出是 `deploy/from-scratch.pdf`。`docs-pdf` 會套用 `deploy/from-scratch.css`，讓中文使用 Noto CJK。工具太重、字型太大，或產生的 PDF 超過倉庫單檔大小限制時，以 Markdown 為準，不影響安裝。

## 檢查時不會做的事

`doctor` 和 `doctor --package` 不連線 Interactive Brokers，不讀取密碼，不啟動 Gateway。`--package` 只檢查倉庫檔案，給沒有 Docker 的環境用：

```bash
./scripts/ats-install.sh doctor --package
```

正式機器用：

```bash
sudo ./scripts/ats-install.sh doctor
```

## 不要做

- 不要在現有那台正在跑的模擬機上執行 `install`
- 不要啟動 `trading-main`
- 不要把券商埠映射到公網，不要在 Compose 加 `ports`
- 不要把密碼、權杖、SSH 私鑰、Healthchecks 網址寫進 git
- 不要把社群的 Gateway 映像當成這套安裝的必經步驟
- 不要在兩台機器同時登入同一個模擬帳戶
