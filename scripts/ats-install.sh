#!/usr/bin/env bash
# Paper-only installer. Review this file, then run it. It does not phone a
# broker and it does not start the Gateway.
set -euo pipefail

# shellcheck disable=SC1007
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
ENV_FILE="$ROOT/deploy/compose/.env"
DATA_ROOT="/srv/trading-data"
COMMAND=""
YES=0
DRY=0
PAPER_ACCOUNT="${ATS_PAPER_ACCOUNT:-}"
SKIP_GATEWAY=0
SKIP_DOCKER=0
PACKAGE_DOCTOR=0
WITH_OPTIONAL=0
FORCE_SEED=0

die() {
  echo "錯誤: $*" >&2
  exit 1
}

warn() {
  echo "提醒: $*" >&2
}

usage() {
  cat <<'EOF'
用法: sudo ./scripts/ats-install.sh <命令> [選項]

命令:
  install                 檢查並安裝 Docker、寫入本機 env、建立資料目錄、啟動 Compose
  up                      重新建立並啟動 Compose
  down                    停下 Compose（不刪資料、不停 Gateway）
  status                  看 Compose 與宿主單元狀態
  doctor                  檢查設定。不會連線券商
  gateway-units           安裝宿主 Gateway / port-guard 單元，不啟動 Gateway
  gateway-credentials     把模擬登入寫進 systemd 加密憑證
  gateway-install         用你已下載的安裝檔安裝 IBC 或 Gateway
  seed-data               補上資料目錄與空白訊號檔
  signals-check           檢查訊號檔的基本格式
  strategy-once           立刻跑一次策略行程（讀訊號、寫意圖）
  docs-pdf                用 pandoc 從教學產生 PDF

選項:
  --yes                   不詢問。install 須同時給 --paper-account
  --paper-account DU…     你的模擬帳戶號碼
  --data-root PATH        資料目錄，預設 /srv/trading-data
  --skip-docker           不安裝 Docker
  --skip-gateway          不安裝宿主 Gateway 單元
  --with-optional         同時啟動 holdings 與 reeval（預設不啟動）
  --force-seed            用倉庫裡的股票池覆蓋資料目錄裡的副本
  --dry-run               install 只印計畫
  --package               doctor 只檢查倉庫裡的檔案
  -h, --help              這段說明

密碼不要放在命令列。gateway-credentials 會詢問，或讀取 root 擁有、權限 600 的檔案:
  ATS_IB_USERNAME_FILE  ATS_IB_PASSWORD_FILE
EOF
}

paper_account_ok() {
  printf '%s\n' "$1" | grep -Eq '^DU[A-Z]?[0-9]+$'
}

systemd_running() {
  [ -d /run/systemd/system ]
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "請用 sudo 執行這條命令。"
}

docker_cli() {
  if [ "$(id -u)" -eq 0 ]; then
    docker "$@"
  elif docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

compose_cli() {
  docker_cli compose --project-name ats-paper -f "$COMPOSE_FILE" "$@"
}

assert_data_root() {
  case "$DATA_ROOT" in
    /*) ;;
    *) die "資料目錄須為絕對路徑。" ;;
  esac
  local root_real data_real
  root_real=$(readlink -f "$ROOT")
  if [ -e "$DATA_ROOT" ]; then
    data_real=$(readlink -f "$DATA_ROOT")
  else
    data_real=$DATA_ROOT
  fi
  case "$data_real" in
    "$root_real"|"$root_real"/*)
      die "資料目錄不要放在 git 工作目錄裡。"
      ;;
  esac
}

ensure_account() {
  if [ -z "$PAPER_ACCOUNT" ]; then
    if [ "$YES" -eq 1 ]; then
      die "非互動安裝請加上 --paper-account。格式是 DU、可選一個大寫字母、然後數字。"
    fi
    if [ ! -t 0 ]; then
      die "沒有終端機可詢問。請改用 --yes --paper-account。"
    fi
    read -r -p "模擬帳戶號碼: " PAPER_ACCOUNT
  fi
  paper_account_ok "$PAPER_ACCOUNT" || die "帳戶號碼格式不對。須為 DU、可選一個大寫字母、然後數字。"
}

APT_UPDATED=0
apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  if [ "$APT_UPDATED" -eq 0 ]; then
    apt-get update
    APT_UPDATED=1
  fi
  apt-get install -y "$@"
}

install_base_packages() {
  apt_install ca-certificates curl git python3 iptables sudo
  apt_install chrony || warn "chrony 沒有裝上。看門狗會一直暫停新開倉。"
  if systemd_running; then
    timedatectl set-timezone UTC || warn "沒有把時區改成 UTC。Gateway 的重啟時間是以 UTC 撰寫的。"
    systemctl enable --now chrony || warn "chrony 沒有啟動。"
  fi
  if [ "$SKIP_GATEWAY" -eq 0 ]; then
    apt_install xvfb xauth libxtst6 libxrender1 libxi6 fonts-dejavu-core unzip
  fi
}

install_docker() {
  if docker compose version >/dev/null 2>&1; then
    echo "Docker Compose 已存在。"
    return 0
  fi
  if [ "$SKIP_DOCKER" -eq 1 ]; then
    die "找不到 Docker Compose，而且指定了 --skip-docker。"
  fi
  local id codename arch
  # shellcheck disable=SC1091
  . /etc/os-release
  id=${ID:-}
  case "$id" in
    ubuntu|debian) ;;
    *)
      die "自動安裝 Docker 只支援 Ubuntu 或 Debian。請先自行安裝 Docker Engine 與 Compose 外掛，再執行 up。"
      ;;
  esac
  codename=${VERSION_CODENAME:-}
  [ -n "$codename" ] || die "讀不到 VERSION_CODENAME。"
  apt_install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${id}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  arch=$(dpkg --print-architecture)
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${id} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  APT_UPDATED=0
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  if systemd_running; then
    systemctl enable --now docker
  fi
  docker compose version >/dev/null
}

ensure_users() {
  if ! id trader >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin trader
  fi
  if ! id ibgw >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/ibgw --create-home --shell /usr/sbin/nologin ibgw
  fi
  if getent group chrony >/dev/null 2>&1; then
    usermod -aG chrony trader || true
  fi
  if getent group docker >/dev/null 2>&1; then
    usermod -aG docker trader || true
  fi
}

write_env() {
  local uid gid chrony_gid
  uid=$(id -u trader)
  gid=$(id -g trader)
  chrony_gid=""
  if getent group chrony >/dev/null 2>&1; then
    chrony_gid=$(getent group chrony | cut -d: -f3)
  fi
  install -d -m 0755 "$(dirname "$ENV_FILE")"
  python3 - "$ENV_FILE" "$PAPER_ACCOUNT" "$DATA_ROOT" "$uid" "$gid" "$chrony_gid" <<'PY'
import sys
path, account, data_root, uid, gid, chrony = sys.argv[1:7]
lines = [
    "# Generated on this machine. Do not commit.",
    "IB_HOST=127.0.0.1",
    "IB_PORT=4002",
    f"IBKR_PAPER_ACCOUNT={account}",
    "TRADING_HOME=/srv/trading",
    "SIGNALS_PATH=/srv/trading/signals.json",
    "BASE_CURRENCY=HKD",
    "IB_READ_ONLY_API=yes",
    f"ATS_DATA_ROOT={data_root}",
    f"ATS_UID={uid}",
    f"ATS_GID={gid}",
]
if chrony:
    lines.append(f"CHRONY_GID={chrony}")
lines.append("")
with open(path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines))
PY
  if getent group trader >/dev/null 2>&1; then
    chown root:trader "$ENV_FILE" || chown root:root "$ENV_FILE"
  else
    chown root:root "$ENV_FILE"
  fi
  chmod 0640 "$ENV_FILE"
  echo "已寫入 $ENV_FILE（沒有密碼）。"
}

seed_data() {
  local dir
  assert_data_root
  install -d -m 0750 -o trader -g trader "$DATA_ROOT"
  for dir in state data logs intents reports alerts backups approvals notifications exports signals_archive state/timers data/universe; do
    install -d -m 0750 -o trader -g trader "$DATA_ROOT/$dir"
  done
  if [ -d "$DATA_ROOT/signals.json" ]; then
    die "signals.json 是目錄。請移走它。Docker 在檔案不存在時會建成目錄。"
  fi
  if [ ! -e "$DATA_ROOT/signals.json" ]; then
    install -m 0640 -o trader -g trader "$ROOT/deploy/signals.example.json" "$DATA_ROOT/signals.json"
    echo "已放入空白訊號檔。之後用研究端的檔案覆蓋它。"
  fi
  if [ "$FORCE_SEED" -eq 1 ] || [ ! -f "$DATA_ROOT/data/universe/ta_pool.csv" ]; then
    if [ -d "$ROOT/data/universe" ]; then
      cp -a "$ROOT/data/universe/." "$DATA_ROOT/data/universe/"
      chown -R trader:trader "$DATA_ROOT/data/universe"
    fi
  fi
}

gateway_binaries() {
  local tmp start src
  if [ -n "${ATS_IBC_ZIP:-}" ]; then
    [ -f "$ATS_IBC_ZIP" ] || die "找不到 IBC 壓縮檔。"
    tmp=$(mktemp -d)
    unzip -q "$ATS_IBC_ZIP" -d "$tmp"
    start=$(find "$tmp" -type f -name ibcstart.sh -print -quit)
    [ -n "$start" ] || die "壓縮檔裡沒有 ibcstart.sh。"
    src=$(dirname "$(dirname "$start")")
    rm -rf /opt/ibc
    mkdir -p /opt
    cp -a "$src" /opt/ibc
    rm -rf "$tmp"
    echo "IBC 已放到 /opt/ibc。"
  fi
  if [ -n "${ATS_IBGATEWAY_SH:-}" ]; then
    [ -f "$ATS_IBGATEWAY_SH" ] || die "找不到 Gateway 安裝檔。"
    chmod 0755 "$ATS_IBGATEWAY_SH"
    sh "$ATS_IBGATEWAY_SH" -q -dir /opt/ibgateway \
      || die "Gateway 安裝檔沒有完成。請在這台機器上互動執行它，目錄用 /opt/ibgateway。"
    echo "Gateway 檔案已放到 /opt/ibgateway。"
  fi
}

gateway_units() {
  if [ "$SKIP_GATEWAY" -eq 1 ]; then
    echo "已略過 Gateway 單元。"
    return 0
  fi
  if ! systemd_running; then
    warn "這台沒有 systemd，略過 Gateway 單元。"
    return 0
  fi
  install -d -m 0755 /usr/local/lib/ibgw /usr/local/sbin /etc/ibc /etc/credstore.encrypted
  install -d -m 0750 -o ibgw -g ibgw /var/lib/ibgw /var/lib/ibgw/Jts /var/lib/ibgw/logs
  install -m 0755 "$ROOT/vps/deploy/ibgw-start.sh" /usr/local/lib/ibgw/ibgw-start.sh
  install -m 0755 "$ROOT/deploy/host/ibkr-port-guard.sh" /usr/local/sbin/ibkr-port-guard
  install -m 0644 "$ROOT/deploy/host/ibgateway.service" /etc/systemd/system/ibgateway.service
  install -m 0644 "$ROOT/deploy/host/ibkr-port-guard.service" /etc/systemd/system/ibkr-port-guard.service
  visudo -cf "$ROOT/vps/deploy/trader-ibgateway.sudoers"
  install -m 0440 "$ROOT/vps/deploy/trader-ibgateway.sudoers" /etc/sudoers.d/trader-ibgateway
  if [ -f /opt/ibc/config.ini.sample ]; then
    python3 "$ROOT/vps/deploy/render_template.py"
  else
    warn "尚未安裝 IBC。裝好後再執行 gateway-units，才會產生設定範本。"
  fi
  systemctl daemon-reload
  systemctl enable --now ibkr-port-guard.service
  echo "Gateway 單元已安裝。本命令沒有啟動 Gateway。"
}

compose_up() {
  local -a args=()
  if [ "$WITH_OPTIONAL" -eq 1 ]; then
    args+=(--profile optional)
  fi
  args+=(up -d --build --remove-orphans)
  compose_cli "${args[@]}"
}

package_doctor() {
  local script ports
  [ -f "$COMPOSE_FILE" ] || die "缺少 Compose 檔。"
  [ -f "$ROOT/deploy/from-scratch.md" ] || die "缺少教學。"
  [ -f "$ROOT/deploy/packaging.md" ] || die "缺少打包清單。"
  [ -f "$ROOT/.env.example" ] || die "缺少 .env.example。"
  bash -n "$ROOT/scripts/ats-install.sh"
  bash -n "$ROOT/deploy/host/ibgw-set-credentials.sh"
  sh -n "$ROOT/deploy/compose/entrypoint.sh"
  sh -n "$ROOT/deploy/compose/timer-loop.sh"
  sh -n "$ROOT/deploy/compose/systemctl-host.sh"
  sh -n "$ROOT/deploy/host/ibkr-port-guard.sh"
  if grep -nE '^[[:space:]]*ports:' "$COMPOSE_FILE"; then
    die "Compose 不應發佈埠。"
  fi
  if grep -n 'trading-main' "$COMPOSE_FILE"; then
    die "Compose 不應包含 trading-main。"
  fi
  if grep -n 'gnzsnz' "$COMPOSE_FILE"; then
    die "Compose 不應依賴 Gateway 映像。"
  fi
  if grep -q 'auto_trading.execution", "main"' "$COMPOSE_FILE"; then
    die "Compose 不應啟動 execution main。"
  fi
  grep -q 'IB_HOST: "127.0.0.1"' "$COMPOSE_FILE" || die "IB_HOST 沒有釘在 127.0.0.1。"
  grep -q 'IB_PORT: "4002"' "$COMPOSE_FILE" || die "IB_PORT 沒有釘在 4002。"
  grep -q 'network_mode: host' "$COMPOSE_FILE" || die "服務須走宿主網路，才能使用 127.0.0.1。"
  mapfile -t ports < <(bash "$ROOT/deploy/host/ibkr-port-guard.sh" --print)
  [ "${#ports[@]}" -eq 5 ] || die "port-guard --print 應有 5 行。"
  [ "${ports[0]}" = "4002" ] || die "port-guard 的模擬 Gateway 埠不對。"
  [ "${ports[1]}" = "7497" ] || die "port-guard 的模擬 TWS 埠不對。"
  [ "${ports[2]}" = "7462" ] || die "port-guard 的命令埠不對。"
  [ "${ports[3]}" = "$((4002 - 1))" ] || die "衍生的 Gateway 拒絕埠不對。"
  [ "${ports[4]}" = "$((7497 - 1))" ] || die "衍生的 TWS 拒絕埠不對。"
  python3 - "$ROOT/deploy/signals.example.json" "$ROOT/.env.example" <<'PY'
import json
import sys

signals, example = sys.argv[1], sys.argv[2]
doc = json.load(open(signals, encoding="utf-8"))
if doc.get("schema_version") != "1.0":
    sys.exit("signals example schema_version")
if not str(doc.get("generated_at", "")).endswith("+08:00"):
    sys.exit("signals example generated_at")
if not isinstance(doc.get("signals"), list):
    sys.exit("signals example signals")
for raw in open(example, encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line or line.split("=", 1)[1].strip():
        sys.exit("env example must keep values empty")
print("package files ok")
PY
  for script in \
    "$ROOT/deploy/compose/timer-loop.sh" \
    "$ROOT/scripts/run_paper_intents_daily.sh" \
    "$ROOT/scripts/pool_rebuild.sh"
  do
    [ -x "$script" ] || warn "$script 沒有執行權限。git 應保存這個權限。"
  done
}

env_get() {
  python3 - "$ENV_FILE" "$1" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
found = ""
try:
    handle = open(path, encoding="utf-8")
except OSError as exc:
    print(f"讀不到本機 env（可改用 sudo）: {exc}", file=sys.stderr)
    sys.exit(2)
with handle:
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            found = value.strip()
print(found)
PY
}

check_signals_file() {
  local path=$1
  python3 - "$path" <<'PY'
import json
import sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    doc = json.load(handle)
need = ("schema_version", "generated_at", "generated_by", "price_date", "signals")
missing = [key for key in need if key not in doc]
if missing:
    sys.exit("缺少欄位: " + ",".join(missing))
if doc["schema_version"] != "1.0":
    sys.exit("schema_version 須為 1.0")
if not str(doc["generated_at"]).endswith("+08:00"):
    sys.exit("generated_at 須以 +08:00 結尾")
if not isinstance(doc["signals"], list):
    sys.exit("signals 須為陣列")
print(f"signals ok: {len(doc['signals'])} rows in {path}")
PY
}

full_doctor() {
  local account port host warnings=0
  package_doctor
  if [ ! -f "$ENV_FILE" ]; then
    die "還沒有 $ENV_FILE。請先執行 install。"
  fi
  account=$(env_get IBKR_PAPER_ACCOUNT)
  paper_account_ok "$account" || die "env 裡的模擬帳戶格式不對。"
  port=$(env_get IB_PORT)
  [ "$port" = "4002" ] || die "env 裡的 IB_PORT 須為 4002。"
  host=$(env_get IB_HOST)
  [ "$host" = "127.0.0.1" ] || die "env 裡的 IB_HOST 須為 127.0.0.1。"
  local data_root
  data_root=$(env_get ATS_DATA_ROOT)
  [ -n "$data_root" ] || data_root=/srv/trading-data
  [ -d "$data_root" ] || die "資料目錄不存在: $data_root"
  [ -f "$data_root/signals.json" ] || die "缺少 $data_root/signals.json"
  check_signals_file "$data_root/signals.json"
  if ! docker_cli compose version >/dev/null 2>&1; then
    die "找不到 Docker Compose。"
  fi
  compose_cli config >/dev/null
  if [ ! -x /opt/ibgateway/jre/bin/java ] || [ ! -x /opt/ibc/scripts/ibcstart.sh ]; then
    warn "Gateway 或 IBC 尚未裝到 /opt。Compose 可以先起來，但還不會連上券商。"
    warnings=$((warnings + 1))
  fi
  if systemd_running; then
    if [ ! -s /etc/credstore.encrypted/ibkr-paper-username ] || [ ! -s /etc/credstore.encrypted/ibkr-paper-password ]; then
      warn "還沒有 Gateway 加密憑證。請執行 gateway-credentials。"
      warnings=$((warnings + 1))
    fi
    if ! systemctl is-active --quiet ibkr-port-guard.service; then
      warn "port-guard 沒有在跑。"
      warnings=$((warnings + 1))
    fi
    if ! systemctl is-active --quiet chrony; then
      warn "chrony 沒有在跑。看門狗會暫停新開倉。"
      warnings=$((warnings + 1))
    fi
  fi
  echo "doctor: 設定檢查完成，提醒 ${warnings} 項。沒有連線券商。"
}

cmd_status() {
  compose_cli ps || true
  if systemd_running; then
    systemctl is-active ibkr-port-guard.service || true
    systemctl is-active ibgateway.service || true
    systemctl is-enabled ibgateway.service || true
  fi
}

cmd_signals_check() {
  local data_root path
  if [ -f "$ENV_FILE" ]; then
    data_root=$(env_get ATS_DATA_ROOT)
  else
    data_root=$DATA_ROOT
  fi
  [ -n "$data_root" ] || data_root=/srv/trading-data
  path=$data_root/signals.json
  [ -f "$path" ] || die "找不到 $path"
  check_signals_file "$path"
}

cmd_docs_pdf() {
  local out=$ROOT/deploy/from-scratch.pdf
  command -v pandoc >/dev/null 2>&1 || die "請先安裝 pandoc。教學以 Markdown 為正文。"
  if command -v weasyprint >/dev/null 2>&1; then
    pandoc "$ROOT/deploy/from-scratch.md" --from gfm --pdf-engine=weasyprint \
      --css "$ROOT/deploy/from-scratch.css" -o "$out"
  elif command -v xelatex >/dev/null 2>&1; then
    pandoc "$ROOT/deploy/from-scratch.md" --from gfm --pdf-engine=xelatex \
      -V mainfont="Noto Sans CJK TC" -o "$out"
  else
    die "需要 weasyprint 或 xelatex 才能產生 PDF。Markdown 仍是正文。"
  fi
  echo "已寫入 $out"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --skip-docker) SKIP_DOCKER=1 ;;
    --skip-gateway) SKIP_GATEWAY=1 ;;
    --with-optional) WITH_OPTIONAL=1 ;;
    --force-seed) FORCE_SEED=1 ;;
    --package) PACKAGE_DOCTOR=1 ;;
    --paper-account)
      shift
      [ "$#" -gt 0 ] || die "--paper-account 需要號碼。"
      PAPER_ACCOUNT=$1
      ;;
    --paper-account=*) PAPER_ACCOUNT=${1#*=} ;;
    --data-root)
      shift
      [ "$#" -gt 0 ] || die "--data-root 需要路徑。"
      DATA_ROOT=$1
      ;;
    --data-root=*) DATA_ROOT=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    --*) die "未知選項: $1" ;;
    *)
      if [ -z "$COMMAND" ]; then
        COMMAND=$1
      else
        die "未知參數: $1"
      fi
      ;;
  esac
  shift
done

[ -n "$COMMAND" ] || { usage; exit 1; }

case "$COMMAND" in
  install)
    if [ "$DRY" -eq 1 ]; then
      echo "dry-run: 不會安裝套件、不會寫入密碼、不會啟動 Gateway。"
      echo "dry-run: data=${DATA_ROOT}"
      if [ -n "$PAPER_ACCOUNT" ]; then
        paper_account_ok "$PAPER_ACCOUNT" || die "帳戶號碼格式不對。"
        echo "dry-run: account format ok"
      else
        echo "dry-run: 未提供帳戶號碼。正式安裝時會詢問，或用 --paper-account。"
      fi
      exit 0
    fi
    require_root
    assert_data_root
    ensure_account
    install_base_packages
    install_docker
    ensure_users
    seed_data
    write_env
    gateway_binaries
    gateway_units
    compose_up
    cat <<EOF
Compose 已啟動。沒有寫入登入密碼，也沒有啟動 Gateway。
資料目錄: ${DATA_ROOT}
本機 env: ${ENV_FILE}
下一步請看 deploy/from-scratch.md 的「啟動 Gateway」與「放入訊號」。
檢查: ./scripts/ats-install.sh doctor
狀態: ./scripts/ats-install.sh status
EOF
    ;;
  up)
    require_root
    [ -f "$ENV_FILE" ] || die "請先執行 install。"
    install_docker
    compose_up
    ;;
  down)
    require_root
    compose_cli down
    echo "Compose 已停下。資料目錄還在。Gateway 沒有被這條命令停下。"
    ;;
  status)
    cmd_status
    ;;
  doctor)
    if [ "$PACKAGE_DOCTOR" -eq 1 ]; then
      package_doctor
      echo "doctor --package: 倉庫檔案檢查通過。沒有連線券商。"
    else
      full_doctor
    fi
    ;;
  gateway-units)
    require_root
    ensure_users
    gateway_units
    ;;
  gateway-credentials)
    require_root
    if [ "$YES" -eq 1 ]; then
      ATS_NONINTERACTIVE=1
      export ATS_NONINTERACTIVE
    fi
    ATS_REPO_ROOT=$ROOT
    export ATS_REPO_ROOT
    bash "$ROOT/deploy/host/ibgw-set-credentials.sh"
    ;;
  gateway-install)
    require_root
    ensure_users
    gateway_binaries
    gateway_units
    ;;
  seed-data)
    require_root
    ensure_users
    seed_data
    ;;
  signals-check)
    cmd_signals_check
    ;;
  strategy-once)
    [ -f "$ENV_FILE" ] || die "請先執行 install。"
    compose_cli run --rm --no-deps strategy /srv/trading/scripts/run_paper_intents_daily.sh
    ;;
  docs-pdf)
    cmd_docs_pdf
    ;;
  *)
    die "未知命令: $COMMAND"
    ;;
esac
