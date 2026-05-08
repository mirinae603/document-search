#!/bin/bash
# scripts/dev.sh

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"
LOG_DIR="$REPO_DIR/logs"
PID_FASTAPI="$REPO_DIR/.pid_fastapi"
PID_SEAWEED="$REPO_DIR/.pid_seaweed"

mkdir -p "$LOG_DIR"

SERVICE="${1:-help}"

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

port_in_use() { lsof -i :$1 >/dev/null 2>&1; }

wait_port() {
    local port=$1 name=$2 max=30
    echo -n "  Waiting for $name on :$port"
    for i in $(seq 1 $max); do
        port_in_use $port && echo -e " ${GREEN}✓${NC}" && return 0
        sleep 1; echo -n "."
    done
    echo -e " ${RED}✗${NC}"; return 1
}

kill_pid_file() {
    local pf=$1
    if [ -f "$pf" ]; then
        local pid=$(cat "$pf")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo -e "  Stopping PID $pid…"
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pf"
    fi
}

kill_port() {
    local port=$1
    local pid=$(lsof -ti :$port 2>/dev/null || true)
    if [ -n "$pid" ]; then
        echo -e "  Killing process on :$port (PID $pid)…"
        kill -9 $pid 2>/dev/null || true
        sleep 0.5
    fi
}

show_status() {
    echo -e "\n${BLUE}── SERVICE STATUS ─────────────────────────────${NC}"
    for svc_port in "FastAPI:8000" "Redis:6379" "SeaweedFS Master:9333" "SeaweedFS Volume:8080" "SeaweedFS Filer:8888"; do
        name="${svc_port%%:*}"
        port="${svc_port##*:}"
        if port_in_use $port; then
            pid=$(lsof -ti :$port 2>/dev/null | head -1)
            echo -e "  ${GREEN}●${NC} $name  :$port  (PID $pid)"
        else
            echo -e "  ${RED}●${NC} $name  :$port  not running"
        fi
    done
    echo ""
}

# ─────────────────────────────────────────────────────────────────
# REDIS
# ─────────────────────────────────────────────────────────────────

start_redis() {
    echo -e "\n${CYAN}── REDIS ───────────────────────────────────────${NC}"

    if port_in_use 6379; then
        echo -e "  ${YELLOW}⚠ Redis already running — skipping${NC}"
        return 0
    fi

    if ! command -v redis-server &> /dev/null; then
        echo -e "  ${RED}✗ redis-server not found — install: brew install redis${NC}"
        return 0   # non-fatal — system degrades gracefully
    fi

    # Guarantee dirs exist before redis parses conf
    mkdir -p "$DATA_DIR/redis"
    mkdir -p "$LOG_DIR"

    redis-server --daemonize  yes \
                 --port        6379 \
                 --dir         "$DATA_DIR/redis" \
                 --logfile     "$LOG_DIR/redis.log" \
                 --appendonly  yes \
                 --appendfilename "redis.aof" \
                 --maxmemory   256mb \
                 --maxmemory-policy allkeys-lru \
                 --save        "900 1" \
                 --save        "300 10"

    sleep 1
    if port_in_use 6379; then
        echo -e "  ${GREEN}✓ Redis running on :6379${NC}"
    else
        echo -e "  ${RED}✗ Redis failed to start — check $LOG_DIR/redis.log${NC}"
    fi
}

# ─────────────────────────────────────────────────────────────────
# FASTAPI
# ─────────────────────────────────────────────────────────────────

start_fastapi() {
    echo -e "\n${CYAN}── FASTAPI ─────────────────────────────────────${NC}"

    kill_pid_file "$PID_FASTAPI"
    kill_port 8000
    sleep 0.5

    source "$REPO_DIR/venv/bin/activate"

    export SEAWEED_MASTER="http://localhost:9333"
    export SEAWEED_VOLUME="http://localhost:8080"
    export SEAWEED_FILER="http://localhost:8888"
    export LANCEDB_PATH="$DATA_DIR/lancedb"

    [ -f "$REPO_DIR/.env" ] && export $(grep -v '^#' "$REPO_DIR/.env" | xargs)

    if [ "${2:-}" = "reload" ] || [ "${HOT_RELOAD:-1}" = "1" ]; then
        echo -e "  ${YELLOW}⚡ Hot-reload mode (uvicorn --reload)${NC}"
        cd "$REPO_DIR"
        exec "$REPO_DIR/venv/bin/uvicorn" app.main:app \
            --host 0.0.0.0 \
            --port 8000 \
            --reload \
            --reload-dir "$REPO_DIR/app" \
            --log-level info
    else
        nohup "$REPO_DIR/venv/bin/python" -u app/main.py \
            > "$LOG_DIR/fastapi.log" 2>&1 &
        echo $! > "$PID_FASTAPI"
        wait_port 8000 "FastAPI" && \
            echo -e "  ${GREEN}✓ FastAPI running (PID $(cat $PID_FASTAPI))${NC}" || \
            { tail -20 "$LOG_DIR/fastapi.log"; exit 1; }
    fi
}

# ─────────────────────────────────────────────────────────────────
# SEAWEED
# ─────────────────────────────────────────────────────────────────

start_seaweed() {
    echo -e "\n${CYAN}── SEAWEEDFS ───────────────────────────────────${NC}"

    if port_in_use 9333; then
        echo -e "  ${YELLOW}⚠ SeaweedFS already running — skipping${NC}"
        echo -e "  ${YELLOW}  Use './scripts/dev.sh seaweed force' to restart it${NC}"
        return 0
    fi

    kill_pid_file "$PID_SEAWEED"
    kill_port 9333; kill_port 8080; kill_port 8888
    sleep 1

    mkdir -p "$DATA_DIR/seaweedfs/filerldb2" "$HOME/.seaweedfs"

    cat > "$REPO_DIR/config/filer.toml" << EOF
[leveldb2]
enabled = true
dir = "$DATA_DIR/seaweedfs/filerldb2"

[notification.webhook]
enabled = true
url = "http://localhost:8000/webhook/seaweed"
bearer_token = ""
queue_size = 100
EOF

    cp "$REPO_DIR/config/filer.toml" "$DATA_DIR/seaweedfs/filer.toml"
    cp "$REPO_DIR/config/filer.toml" "$HOME/.seaweedfs/filer.toml"

    cd "$DATA_DIR"
    nohup ./seaweedfs/weed server \
        -dir=./seaweedfs \
        -master.port=9333 \
        -volume.port=8080 \
        -filer=true \
        -filer.port=8888 \
        -s3=false \
        > "$LOG_DIR/seaweedfs.log" 2>&1 &
    echo $! > "$PID_SEAWEED"
    cd "$REPO_DIR"

    wait_port 9333 "SeaweedFS Master" || exit 1
    wait_port 8080 "SeaweedFS Volume" || exit 1
    wait_port 8888 "SeaweedFS Filer"  || exit 1
    echo -e "  ${GREEN}✓ SeaweedFS running (PID $(cat $PID_SEAWEED))${NC}"
}

# ─────────────────────────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────────────────────────

case "$SERVICE" in

    fastapi|api|f)
        if [ "${2:-}" = "bg" ]; then
            HOT_RELOAD=0 start_fastapi
        else
            start_fastapi reload
        fi
        ;;

    seaweed|sw|s)
        start_seaweed
        ;;

    redis|r)
        start_redis
        ;;

    all|a)
        start_seaweed
        start_redis
        HOT_RELOAD=0 start_fastapi
        show_status
        echo -e "${BLUE}Logs:${NC}"
        echo -e "  tail -f $LOG_DIR/fastapi.log"
        echo -e "  tail -f $LOG_DIR/redis.log"
        echo -e "  tail -f $LOG_DIR/seaweedfs.log"
        ;;

    status|st)
        show_status
        ;;

    logs|l)
        echo -e "${BLUE}Tailing all logs (Ctrl+C to stop)…${NC}\n"
        tail -f "$LOG_DIR/fastapi.log" "$LOG_DIR/seaweedfs.log" "$LOG_DIR/redis.log"
        ;;

    *)
        echo -e "\n${BLUE}Usage:${NC}"
        echo -e "  ./scripts/dev.sh ${GREEN}fastapi${NC}       — hot-reload FastAPI in foreground"
        echo -e "  ./scripts/dev.sh ${GREEN}fastapi bg${NC}    — restart FastAPI in background"
        echo -e "  ./scripts/dev.sh ${GREEN}seaweed${NC}       — restart SeaweedFS only"
        echo -e "  ./scripts/dev.sh ${GREEN}redis${NC}         — restart Redis only"
        echo -e "  ./scripts/dev.sh ${GREEN}all${NC}           — start all services"
        echo -e "  ./scripts/dev.sh ${GREEN}status${NC}        — show what's running"
        echo -e "  ./scripts/dev.sh ${GREEN}logs${NC}          — tail all log files\n"
        ;;
esac