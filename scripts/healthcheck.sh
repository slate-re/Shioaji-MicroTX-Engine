#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
PID_FILE=${MICROTX_PID_FILE:-"$PROJECT_DIR/runtime/microtx.pid"}
STATUS_FILE=${MICROTX_STATUS_FILE:-"$PROJECT_DIR/runtime/status.json"}
INTERVAL=${MICROTX_STATUS_INTERVAL_SEC:-5}

if [ ! -r "$PID_FILE" ] || ! kill -0 "$(sed -n '1p' "$PID_FILE")" 2>/dev/null; then
  echo "引擎未運行"
  exit 1
fi
if [ ! -r "$STATUS_FILE" ]; then
  echo "引擎未運行：狀態快照不存在"
  exit 1
fi

RESULT=$(python3 -c 'import json,sys
from datetime import datetime
p=json.load(open(sys.argv[1], encoding="utf-8"))
t=datetime.fromisoformat(p["written_at"])
age=(datetime.now(t.tzinfo)-t).total_seconds()
print("stale" if age > float(sys.argv[2])*3 else "degraded" if p.get("degraded") else "ok")' "$STATUS_FILE" "$INTERVAL") || {
  echo "快照過期：無法解析狀態"
  exit 2
}

case "$RESULT" in
  ok) echo "引擎正常"; exit 0 ;;
  degraded) echo "警告：引擎卡在共用鎖，建議立即執行 microtx panic"; exit 3 ;;
  *) echo "警告：快照過期，引擎無回應"; exit 2 ;;
esac
