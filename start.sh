#!/usr/bin/env bash
# 按需启动脚本 —— 不注册任何开机自启/常驻服务，关掉即停。
#
# 用法:
#   ./start.sh <学号>          启动全部：MySQL → 采集一次 → 后端 → 前端
#   ./start.sh <学号> --no-collect   只启服务、不采集
#   ./start.sh                 不带学号：只启动服务（沿用库里已有数据）
#
# 学号只用于本次采集（GET /external/appUser/{学号} 免密取 appUserId/roleId），
# 不写入任何文件；也可以先 export UIT_USER=<学号> 再执行。

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
MYSQLD="$(command -v mysqld || echo /opt/homebrew/opt/mysql/bin/mysqld)"
MYSQL="$(command -v mysql || echo /opt/homebrew/opt/mysql/bin/mysql)"
MYSQLADMIN="$(command -v mysqladmin || echo /opt/homebrew/opt/mysql/bin/mysqladmin)"
SOCK="${MYSQL_SOCK:-/tmp/mysql.sock}"
DATA_DIR="${MYSQL_DATA_DIR:-/opt/homebrew/var/mysql}"
PORT_API="${PORT_API:-8080}"
PORT_WEB="${PORT_WEB:-3000}"
LOG_DIR="${LOG_DIR:-/tmp}"

# 库名优先从 mysql.ini 读，读不到用默认
DB_SCHEMA="$(sed -n 's/^db_schema *= *//p' "$ROOT/debug_utils/data2sql/config/mysql.ini" 2>/dev/null | head -1)"
DB_SCHEMA="${DB_SCHEMA:-uit_check_money}"

# 参数解析：把 --no-collect 之类的开关与学号分开，开关放哪个位置都行
# （原来用 ${1} 当学号、${2} 当开关，于是 `./start.sh --no-collect` 会把开关当成学号）
USER_ID=""
NO_COLLECT=""
for _arg in "$@"; do
    case "$_arg" in
        --no-collect) NO_COLLECT="--no-collect" ;;
        -*)           echo "  未知参数: $_arg（可用: --no-collect）" ;;
        *)            [ -z "$USER_ID" ] && USER_ID="$_arg" ;;
    esac
done
[ -z "$USER_ID" ] && USER_ID="${UIT_USER:-}"

say() { printf '  %s\n' "$*"; }
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

echo "== 1/4 MySQL =="
if "$MYSQLADMIN" --socket="$SOCK" -u root ping 2>/dev/null | grep -q alive; then
    say "已在运行（socket ${SOCK}），跳过"
else
    say "未运行 → 临时拉起（${DATA_DIR}）"
    nohup "$MYSQLD" --datadir="$DATA_DIR" --socket="$SOCK" --port=3306 --mysqlx=OFF \
        --log-error="$LOG_DIR/mysqld-panel.log" >>"$LOG_DIR/mysqld-panel.out" 2>&1 &
    for _ in $(seq 1 30); do
        "$MYSQLADMIN" --socket="$SOCK" -u root ping 2>/dev/null | grep -q alive && break
        sleep 1
    done
    "$MYSQLADMIN" --socket="$SOCK" -u root ping 2>/dev/null | grep -q alive \
        && say "启动成功" || { say "启动失败，看 $LOG_DIR/mysqld-panel.log"; exit 1; }
fi

echo "== 2/4 采集一次 =="
if [ "$NO_COLLECT" = "--no-collect" ]; then
    say "按参数要求跳过"
elif [ -z "$USER_ID" ]; then
    say "没给学号 → 跳过（用库里已有数据）"
else
    LOGIN_JSON="$("$PY" "$ROOT/debug_utils/login.py" "$USER_ID" 2>/dev/null)" || true
    A="$(printf '%s' "$LOGIN_JSON" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("appUserId",""))' 2>/dev/null)"
    R="$(printf '%s' "$LOGIN_JSON" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("roleId",""))' 2>/dev/null)"
    if [ -z "$A" ] || [ -z "$R" ]; then
        say "取 appUserId/roleId 失败，登录接口返回：$(printf '%s' "$LOGIN_JSON" | head -c 200)"
    else
        say "appUserId=$A roleId=$R"
        "$PY" "$ROOT/debug_utils/data2sql/data2sql.py" "$A" "$R" 2>/dev/null | sed 's/^/     /'
    fi
fi

echo "== 3/4 后端 API（:${PORT_API}）=="
if port_busy "$PORT_API"; then
    say "已在监听，跳过"
else
    ( cd "$ROOT/server" && nohup "$PY" server.py >>"$LOG_DIR/hnuit-api.log" 2>&1 & )
    for _ in $(seq 1 20); do port_busy "$PORT_API" && break; sleep 1; done
    port_busy "$PORT_API" && say "已启动（日志 $LOG_DIR/hnuit-api.log）" || say "没起来，看 $LOG_DIR/hnuit-api.log"
fi

echo "== 4/4 前端（:${PORT_WEB}）=="
if port_busy "$PORT_WEB"; then
    say "已在监听，跳过"
else
    ( cd "$ROOT/web" && nohup "$PY" -m http.server "$PORT_WEB" >>"$LOG_DIR/hnuit-web.log" 2>&1 & )
    for _ in $(seq 1 10); do port_busy "$PORT_WEB" && break; sleep 1; done
    port_busy "$PORT_WEB" && say "已启动" || say "没起来，看 $LOG_DIR/hnuit-web.log"
fi

echo
echo "面板地址: http://127.0.0.1:${PORT_WEB}     （后端 http://127.0.0.1:${PORT_API}）"
echo "停止全部: ./stop.sh"
