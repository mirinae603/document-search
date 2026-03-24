#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_DIR/.pids"
REDIS_CONF="$REPO_DIR/config/redis.conf"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Stopping Document Search System${NC}"
echo -e "${BLUE}================================================${NC}\n"

# ── Graceful stop helper ──────────────────────────────────────────
stop_process() {
    local pid=$1
    local name=$2
    local timeout=${3:-10}

    if ! ps -p $pid > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠ $name (PID $pid) already stopped${NC}"
        return 0
    fi

    echo -n "Stopping $name (PID: $pid)..."
    kill -TERM $pid 2>/dev/null

    for i in $(seq 1 $timeout); do
        if ! ps -p $pid > /dev/null 2>&1; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        fi
        sleep 1
    done

    echo -e " ${YELLOW}timeout — force killing...${NC}"
    kill -9 $pid 2>/dev/null
    sleep 1

    if ! ps -p $pid > /dev/null 2>&1; then
        echo -e "${GREEN}✓ $name force stopped${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to stop $name${NC}"
        return 1
    fi
}

# ── Redis graceful shutdown ───────────────────────────────────────
# Uses SHUTDOWN SAVE so AOF/RDB is flushed before exit — no data loss
stop_redis() {
    if command -v redis-cli &> /dev/null; then
        echo -n "Stopping Redis (SHUTDOWN SAVE)..."
        redis-cli shutdown save > /dev/null 2>&1 || true
        sleep 2
        if ! pgrep -f "redis-server" > /dev/null 2>&1; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        fi
    fi

    # Fallback — kill by PID file redis writes
    local redis_pid_file="$REPO_DIR/data/redis/redis.pid"
    if [ -f "$redis_pid_file" ]; then
        local rpid=$(cat "$redis_pid_file")
        stop_process $rpid "Redis" 10
        rm -f "$redis_pid_file"
        return 0
    fi

    # Last resort — pkill
    if pgrep -f "redis-server" > /dev/null 2>&1; then
        echo -n "Stopping Redis (pkill)..."
        pkill -TERM -f "redis-server" 2>/dev/null
        sleep 3
        pkill -9 -f "redis-server" 2>/dev/null
        echo -e " ${GREEN}✓${NC}"
    else
        echo -e "${YELLOW}⚠ Redis not running${NC}"
    fi
}

# ── No PID file fallback ──────────────────────────────────────────
if [ ! -f "$PID_FILE" ]; then
    echo -e "${YELLOW}⚠ No PID file found — scanning for processes...${NC}\n"

    stop_redis

    pkill -TERM -f "main.py"   2>/dev/null && echo -e "${GREEN}✓ FastAPI stopped${NC}"   || true
    pkill -TERM -f "weed server" 2>/dev/null && echo -e "${GREEN}✓ SeaweedFS stopped${NC}" || true

    sleep 3

    pkill -9 -f "main.py"     2>/dev/null || true
    pkill -9 -f "weed server" 2>/dev/null || true

    echo -e "\n${GREEN}Cleanup complete${NC}\n"
    exit 0
fi

# ── Read PIDs ─────────────────────────────────────────────────────
PIDS=($(cat "$PID_FILE"))
NUM_PIDS=${#PIDS[@]}

if [ $NUM_PIDS -eq 0 ]; then
    echo -e "${YELLOW}⚠ PID file empty${NC}"
    rm -f "$PID_FILE"
    exit 0
fi

echo -e "${BLUE}Found $NUM_PIDS process(es) to stop${NC}\n"

# Stop order: FastAPI → Redis → SeaweedFS
# FastAPI must go first so no new DB/Redis writes land mid-shutdown

# FastAPI — last entry in PID file
if [ $NUM_PIDS -ge 2 ]; then
    FASTAPI_PID=${PIDS[$((NUM_PIDS-1))]}
    stop_process $FASTAPI_PID "FastAPI" 15
fi

# Redis — graceful SHUTDOWN SAVE (always, not by PID)
stop_redis

# SeaweedFS — first entry in PID file
if [ $NUM_PIDS -ge 1 ]; then
    SEAWEED_PID=${PIDS[0]}
    stop_process $SEAWEED_PID "SeaweedFS" 20
fi

# ── Orphan sweep ──────────────────────────────────────────────────
echo -e "\n${BLUE}Checking for orphaned processes...${NC}"

if pgrep -f "weed server" > /dev/null 2>&1; then
    echo -n "Cleaning orphaned SeaweedFS..."
    pkill -TERM -f "weed server" 2>/dev/null; sleep 3
    pkill -9    -f "weed server" 2>/dev/null || true
    echo -e " ${GREEN}✓${NC}"
fi

if pgrep -f "main.py" > /dev/null 2>&1; then
    echo -n "Cleaning orphaned FastAPI..."
    pkill -TERM -f "main.py" 2>/dev/null; sleep 3
    pkill -9    -f "main.py" 2>/dev/null || true
    echo -e " ${GREEN}✓${NC}"
fi

if pgrep -f "redis-server" > /dev/null 2>&1; then
    echo -n "Cleaning orphaned Redis..."
    pkill -TERM -f "redis-server" 2>/dev/null; sleep 2
    pkill -9    -f "redis-server" 2>/dev/null || true
    echo -e " ${GREEN}✓${NC}"
fi

# ── Cleanup ───────────────────────────────────────────────────────
rm -f "$PID_FILE"

echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  All services stopped${NC}"
echo -e "${GREEN}================================================${NC}\n"
