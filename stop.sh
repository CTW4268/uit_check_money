#!/usr/bin/env bash
# 停止 start.sh 拉起的所有进程（MySQL、后端 API、前端静态服务）。
# 用法: ./stop.sh          # 停三个
#       ./stop.sh api      # 只停后端
#       ./stop.sh web      # 只停前端
#       ./stop.sh mysql    # 只停 MySQL
# 不影响系统里其它 mysqld / python 进程。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOCK="${MYSQL_SOCK:-/tmp/mysql.sock}"
PORT_API="${PORT_API:-8080}"
PORT_WEB="${PORT_WEB:-3000}"
TARGET="${1:-all}"

kill_port() {
    local port="$1" name="$2"
    local pids
    pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -z "$pids" ]; then
        printf '  %s（:%s）没在跑\n' "$name" "$port"
    else
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null && printf '  %s（:%s）已停止\n' "$name" "$port"
    fi
}

case "$TARGET" in
    all)
        kill_port "$PORT_API" "后端 API"
        kill_port "$PORT_WEB" "前端"
        kill_mysql=1
        ;;
    api) kill_port "$PORT_API" "后端 API"; kill_mysql=0 ;;
    web) kill_port "$PORT_WEB" "前端"; kill_mysql=0 ;;
    mysql) kill_mysql=1 ;;
    *) echo "用法: ./stop.sh [all|api|web|mysql]"; exit 1 ;;
esac

if [ "${kill_mysql:-0}" = "1" ]; then
    if command -v mysqladmin >/dev/null 2>&1 && mysqladmin --socket="$SOCK" -u root ping 2>/dev/null | grep -q alive; then
        mysqladmin --socket="$SOCK" -u root shutdown 2>/dev/null && echo "  MySQL 已停止（数据留在磁盘上，下次 start.sh 直接续用）"
    else
        echo "  MySQL 没在跑"
    fi
fi
