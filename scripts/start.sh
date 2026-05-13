#!/bin/bash

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"
LOG_DIR="$REPO_DIR/logs"
PID_FILE="$REPO_DIR/.pids"

mkdir -p "$LOG_DIR"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Starting Document Search System${NC}"
echo -e "${BLUE}================================================${NC}\n"

# ── Helpers ───────────────────────────────────────────────────────
check_port() {
    lsof -i :$1 >/dev/null 2>&1
}

wait_for_service() {
    local port=$1
    local service=$2
    local max_wait=30
    echo -n "Waiting for $service..."
    for i in $(seq 1 $max_wait); do
        if check_port $port; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        fi
        sleep 1
        echo -n "."
    done
    echo -e " ${RED}✗${NC}"
    return 1
}

# ── Pre-flight ────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/venv" ]; then
    echo -e "${RED}✗ venv not found. Run ./scripts/setup.sh first${NC}"
    exit 1
fi

if [ ! -f "$DATA_DIR/seaweedfs/weed" ]; then
    echo -e "${RED}✗ SeaweedFS binary not found. Run ./scripts/setup.sh first${NC}"
    exit 1
fi

# Force-clear stale PID file — don't block on it
if [ -f "$PID_FILE" ]; then
    echo -e "${YELLOW}⚠ Stale PID file found — clearing it${NC}"
    rm -f "$PID_FILE"
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    echo -e "${YELLOW}⚠ OPENROUTER_API_KEY not set${NC}"
fi

# ── Activate venv ─────────────────────────────────────────────────
echo -e "${BLUE}[0/3] Activating virtual environment...${NC}"
source "$REPO_DIR/venv/bin/activate"
echo -e "${GREEN}✓ Virtual environment active${NC}\n"

# ── Error trap ────────────────────────────────────────────────────
> "$PID_FILE"

cleanup_on_error() {
    echo -e "\n${RED}✗ Startup failed, cleaning up...${NC}"
    bash "$REPO_DIR/scripts/stop.sh" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
}
trap cleanup_on_error ERR

# ── 1. SeaweedFS ──────────────────────────────────────────────────
echo -e "${BLUE}[1/3] Starting SeaweedFS...${NC}"

mkdir -p "$DATA_DIR/seaweedfs/filerldb2" "$HOME/.seaweedfs"

# Write filer.toml with correct absolute paths every time
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

# Copy to every path SeaweedFS auto-discovers
cp "$REPO_DIR/config/filer.toml" "$DATA_DIR/seaweedfs/filer.toml"
cp "$REPO_DIR/config/filer.toml" "$HOME/.seaweedfs/filer.toml"

echo -e "${GREEN}✓ filer.toml deployed${NC}"

# notification.toml — webhook config
cat > "$REPO_DIR/config/notification.toml" << EOF
[notification.webhook]
enabled = true
endpoint = "http://localhost:8000/webhook/seaweed"
bearer_token = ""
queue_size = 100
EOF

cp "$REPO_DIR/config/notification.toml" "$DATA_DIR/seaweedfs/notification.toml"
cp "$REPO_DIR/config/notification.toml" "$HOME/.seaweedfs/notification.toml"
echo -e "${GREEN}✓ notification.toml deployed${NC}"

# Launch from DATA_DIR so weed finds filer.toml in its working dir
cd "$DATA_DIR"

nohup ./seaweedfs/weed server \
    -dir=./seaweedfs \
    -master.port=9333 \
    -volume.port=8080 \
    -filer=true \
    -filer.port=8888 \
    -s3=true \
    -s3.port=8333
    > "$LOG_DIR/seaweedfs.log" 2>&1 &

SEAWEED_PID=$!
echo $SEAWEED_PID >> "$PID_FILE"

if ! wait_for_service 9333 "SeaweedFS Master"; then
    echo -e "${RED}✗ SeaweedFS Master failed${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

if ! wait_for_service 8080 "SeaweedFS Volume"; then
    echo -e "${RED}✗ SeaweedFS Volume failed${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

if ! wait_for_service 8888 "SeaweedFS Filer"; then
    echo -e "${RED}✗ SeaweedFS Filer failed${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

echo -e "${GREEN}✓ SeaweedFS running (PID: $SEAWEED_PID)${NC}"
echo -e "  Master :9333  Volume :8080  Filer :8888\n"

# ── 2. FastAPI ────────────────────────────────────────────────────
echo -e "${BLUE}[2/3] Starting FastAPI...${NC}"
cd "$REPO_DIR"

export SEAWEED_MASTER="http://localhost:9333"
export SEAWEED_VOLUME="http://localhost:8080"
export SEAWEED_FILER="http://localhost:8888"
export LANCEDB_PATH="$DATA_DIR/lancedb"

nohup "$REPO_DIR/venv/bin/python" -u app/main.py \
    > "$LOG_DIR/fastapi.log" 2>&1 &

FASTAPI_PID=$!
echo $FASTAPI_PID >> "$PID_FILE"

if ! wait_for_service 8000 "FastAPI"; then
    echo -e "${RED}✗ FastAPI failed${NC}"
    tail -30 "$LOG_DIR/fastapi.log"
    cleanup_on_error
fi

echo -e "${GREEN}✓ FastAPI running (PID: $FASTAPI_PID)${NC}"
echo -e "  API :8000\n"

# ── 3. Verify ─────────────────────────────────────────────────────
echo -e "${BLUE}[3/3] Verifying...${NC}"
sleep 2

if curl -sf http://localhost:8000/health > /dev/null; then
    echo -e "${GREEN}✓ FastAPI health check passed${NC}"
else
    echo -e "${YELLOW}⚠ FastAPI health check pending${NC}"
fi

if grep -qi "notification\|webhook\|leveldb" "$LOG_DIR/seaweedfs.log" 2>/dev/null; then
    echo -e "${GREEN}✓ filer.toml loaded by SeaweedFS${NC}"
else
    echo -e "${YELLOW}⚠ filer.toml not confirmed yet — check: tail -f $LOG_DIR/seaweedfs.log${NC}"
fi

# ── Done ──────────────────────────────────────────────────────────
echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  All services started!${NC}"
echo -e "${GREEN}================================================${NC}\n"

echo -e "${BLUE}Access:${NC}"
echo -e "  🌐 Web UI:    ${GREEN}http://localhost:8000${NC}"
echo -e "  📚 API Docs:  ${GREEN}http://localhost:8000/docs${NC}"
echo -e "  📊 SeaweedFS: http://localhost:9333"
echo -e "  📁 Filer:     http://localhost:8888"

echo -e "\n${BLUE}Live logs:${NC}"
echo -e "  tail -f $LOG_DIR/fastapi.log"
echo -e "  tail -f $LOG_DIR/seaweedfs.log"

echo -e "\n${BLUE}Management:${NC}"
echo -e "  Stop:    ${GREEN}./scripts/stop.sh${NC}"
echo -e "  Status:  ${GREEN}./scripts/status.sh${NC}"
echo -e "  Restart: ${GREEN}./scripts/restart.sh${NC}\n"

cat "$PID_FILE" | while read pid; do
    if ps -p $pid > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} PID $pid running"
    fi
done
echo ""
