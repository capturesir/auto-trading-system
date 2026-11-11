#!/usr/bin/env bash
# Encrypt the paper Gateway login into the host credential store.
# Passwords are read from a prompt or from root-only files. They are not
# arguments, and this script does not print them.
set +x
set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "請用 root 執行。" >&2
  exit 1
fi

repo=${ATS_REPO_ROOT:-}
yes_mode=${ATS_NONINTERACTIVE:-0}
user_file=${ATS_IB_USERNAME_FILE:-}
pass_file=${ATS_IB_PASSWORD_FILE:-}

refuse_repo_path() {
  local file real root_real
  file=$1
  [ -n "$repo" ] || return 0
  real=$(readlink -f "$file")
  root_real=$(readlink -f "$repo")
  case "$real" in
    "$root_real"|"$root_real"/*)
      echo "不要從 git 工作目錄讀取登入檔。" >&2
      exit 1
      ;;
  esac
}

assert_private_file() {
  local file mode owner
  file=$1
  [ -f "$file" ] || { echo "找不到檔案。" >&2; exit 1; }
  refuse_repo_path "$file"
  mode=$(stat -c '%a' "$file")
  owner=$(stat -c '%u' "$file")
  case "$mode" in
    600|400) ;;
    *)
      echo "登入檔權限須為 600 或 400。" >&2
      exit 1
      ;;
  esac
  [ "$owner" = "0" ] || { echo "登入檔須由 root 擁有。" >&2; exit 1; }
}

read_secret_file() {
  local file
  file=$1
  tr -d '\r' < "$file"
}

if [ "$yes_mode" = "1" ]; then
  [ -n "$user_file" ] && [ -n "$pass_file" ] || {
    echo "非互動模式請設定 ATS_IB_USERNAME_FILE 與 ATS_IB_PASSWORD_FILE。" >&2
    exit 1
  }
  assert_private_file "$user_file"
  assert_private_file "$pass_file"
  ib_user=$(read_secret_file "$user_file")
  ib_pass=$(read_secret_file "$pass_file")
else
  read -r -p "模擬帳戶登入名稱（不會寫進 git）: " ib_user
  read -r -s -p "模擬帳戶密碼（不會顯示）: " ib_pass
  printf '\n'
  read -r -s -p "再輸入一次密碼: " ib_pass_again
  printf '\n'
  [ "$ib_pass" = "$ib_pass_again" ] || { echo "兩次密碼不一致。" >&2; exit 1; }
  unset ib_pass_again
fi

ib_user=${ib_user%$'\n'}
ib_pass=${ib_pass%$'\n'}
if [ -z "$ib_user" ] || [ -z "$ib_pass" ]; then
  echo "登入名稱或密碼是空的。" >&2
  exit 1
fi
case "$ib_user" in
  *$'\n'*) echo "登入名稱不能換行。" >&2; exit 1 ;;
esac
case "$ib_pass" in
  *$'\n'*) echo "密碼不能換行。" >&2; exit 1 ;;
esac

if ! command -v systemd-creds >/dev/null 2>&1; then
  echo "這台沒有 systemd-creds。" >&2
  exit 1
fi

install -d -m 0755 /etc/credstore.encrypted
tmp_user=$(mktemp /run/ibgw-user.XXXXXX)
tmp_pass=$(mktemp /run/ibgw-pass.XXXXXX)
cleanup() {
  if command -v shred >/dev/null 2>&1; then
    shred -u "$tmp_user" "$tmp_pass" 2>/dev/null || rm -f "$tmp_user" "$tmp_pass"
  else
    rm -f "$tmp_user" "$tmp_pass"
  fi
}
trap cleanup EXIT
printf '%s\n' "$ib_user" > "$tmp_user"
printf '%s\n' "$ib_pass" > "$tmp_pass"
unset ib_user ib_pass

encrypt_one() {
  local name src dest
  name=$1
  src=$2
  dest="/etc/credstore.encrypted/$name"
  if ! systemd-creds encrypt --with-key=host --name="$name" "$src" "$dest"; then
    systemd-creds encrypt --name="$name" "$src" "$dest"
  fi
  chown root:root "$dest"
  chmod 0640 "$dest"
}

encrypt_one ibkr-paper-username "$tmp_user"
encrypt_one ibkr-paper-password "$tmp_pass"
echo "已寫入加密憑證。明文檔只在記憶體與暫存，腳本結束會清掉暫存。"
echo "這一步沒有啟動 Gateway。"
