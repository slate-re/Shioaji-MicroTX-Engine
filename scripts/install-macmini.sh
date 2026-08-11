#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR="$PROJECT_DIR/.venv"
SOURCE_PLIST="$SCRIPT_DIR/com.jam.microtx.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.jam.microtx.plist"

"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo "需要 Python 3.10 以上版本" >&2
  exit 1
}

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
if [ ! -x "$VENV_DIR/bin/microtx" ]; then
  "$VENV_DIR/bin/python" -m pip install -e "$PROJECT_DIR[live]"
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "缺少 .env；請先複製 .env.example 並填入憑證" >&2
  exit 1
fi
chmod 600 "$PROJECT_DIR/.env"

if command -v systemsetup >/dev/null 2>&1; then
  NETWORK_TIME=$(sudo systemsetup -getusingnetworktime 2>/dev/null || true)
  case "$NETWORK_TIME" in
    *On*) : ;;
    *) echo "警告：請開啟系統時間自動同步，避免 Sign data is timeout" >&2 ;;
  esac
fi
if command -v pmset >/dev/null 2>&1; then
  SLEEP_VALUE=$(pmset -g | awk '$1 == "sleep" {print $2; exit}')
  if [ "${SLEEP_VALUE:-1}" != "0" ]; then
    echo "警告：自動睡眠尚未關閉，請執行 sudo pmset -a sleep 0" >&2
  fi
fi

mkdir -p "$PROJECT_DIR/runtime" "$PROJECT_DIR/logs" "$(dirname "$TARGET_PLIST")"
sed "s#/path/to/Shioaji-MicroTX-Engine#$PROJECT_DIR#g" "$SOURCE_PLIST" >"$TARGET_PLIST"

DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/com.jam.microtx" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN" "$TARGET_PLIST"
fi
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
echo "MicroTX launchd 服務已安裝"
