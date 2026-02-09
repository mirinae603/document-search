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

# Function to check if port is in use
check_port() {
    lsof -i :$1 >/dev/null 2>&1
}

# Function to wait for service
wait_for_service() {
    local port=$1
    local service=$2
    local max_wait=30
    
    echo -n "Waiting for $service to be ready..."
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

# Check if venv exists
if [ ! -d "$REPO_DIR/venv" ]; then
    echo -e "${RED}✗ Virtual environment not found. Run ./scripts/setup.sh first${NC}"
    exit 1
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
    echo -e "${YELLOW}⚠ Services may already be running${NC}"
    echo -e "Run ${GREEN}./scripts/stop.sh${NC} first or check ${GREEN}./scripts/status.sh${NC}"
    exit 1
fi

# Check for API keys
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo -e "${YELLOW}⚠ Warning: OPENROUTER_API_KEY not set${NC}"
    echo "Export it: export OPENROUTER_API_KEY='your-key'"
    echo ""
fi

# Activate virtual environment
echo -e "${BLUE}[0/3] Activating Python virtual environment...${NC}"
source "$REPO_DIR/venv/bin/activate"

if [ -z "$VIRTUAL_ENV" ]; then
    echo -e "${RED}✗ Failed to activate virtual environment${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Virtual environment active${NC}\n"

# Initialize PID file
> "$PID_FILE"

# Cleanup function for error handling
cleanup_on_error() {
    echo -e "\n${RED}✗ Startup failed, cleaning up...${NC}"
    bash "$REPO_DIR/scripts/stop.sh" 2>/dev/null || true
    exit 1
}

# Set trap for errors
trap cleanup_on_error ERR

# 1. Start SeaweedFS
echo -e "${BLUE}[1/3] Starting SeaweedFS (Master + Volume + Filer)...${NC}"
cd "$DATA_DIR"

# Create filer directory if not exists
mkdir -p ./seaweedfs/filerldb2

# Create filer config if not exists
mkdir -p "$REPO_DIR/config"
if [ ! -f "$REPO_DIR/config/filer.toml" ]; then
    cat > "$REPO_DIR/config/filer.toml" << EOF
[leveldb2]
enabled = true
dir = "$DATA_DIR/seaweedfs/filerldb2"

# Webhook notification for file events
[notification.webhook]
enabled = true
url = "http://localhost:8000/webhook/seaweed"
bearer_token = ""
queue_size = 100
EOF
fi

# Start SeaweedFS with correct syntax for Mac
nohup ./seaweedfs/weed server \
    -dir=./seaweedfs \
    -master.port=9333 \
    -volume.port=8080 \
    -filer=true \
    -filer.port=8888 \
    -s3=false \
    > "$LOG_DIR/seaweedfs.log" 2>&1 &

SEAWEED_PID=$!
echo $SEAWEED_PID >> "$PID_FILE"

# Wait for SeaweedFS services
if ! wait_for_service 9333 "SeaweedFS Master"; then
    echo -e "${RED}✗ SeaweedFS Master failed to start${NC}"
    echo -e "\n${RED}Last 30 lines of log:${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

if ! wait_for_service 8080 "SeaweedFS Volume"; then
    echo -e "${RED}✗ SeaweedFS Volume failed to start${NC}"
    echo -e "\n${RED}Last 30 lines of log:${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

if ! wait_for_service 8888 "SeaweedFS Filer"; then
    echo -e "${RED}✗ SeaweedFS Filer failed to start${NC}"
    echo -e "\n${RED}Last 30 lines of log:${NC}"
    tail -30 "$LOG_DIR/seaweedfs.log"
    cleanup_on_error
fi

echo -e "${GREEN}✓ SeaweedFS running (PID: $SEAWEED_PID)${NC}"
echo -e "  - Master: :9333"
echo -e "  - Volume: :8080"
echo -e "  - Filer:  :8888\n"

# Configure filer webhook (after filer is running)
echo -e "${BLUE}Configuring filer webhook...${NC}"
sleep 2

# Copy filer config to the running filer
curl -X POST "http://localhost:8888/etc/filer.conf" \
    --data-binary @"$REPO_DIR/config/filer.toml" \
    > /dev/null 2>&1 || echo -e "${YELLOW}⚠ Webhook config may need manual setup${NC}"

echo -e "${GREEN}✓ Filer configured${NC}\n"

# 2. Start FastAPI
echo -e "${BLUE}[2/3] Starting FastAPI application...${NC}"
cd "$REPO_DIR"

# Set environment variables
export SEAWEED_MASTER="http://localhost:9333"
export SEAWEED_VOLUME="http://localhost:8080"
export SEAWEED_FILER="http://localhost:8888"
export LANCEDB_PATH="$DATA_DIR/lancedb"

# Start FastAPI with explicit python from venv
nohup "$REPO_DIR/venv/bin/python" -u app/app_production.py > "$LOG_DIR/fastapi.log" 2>&1 &
FASTAPI_PID=$!
echo $FASTAPI_PID >> "$PID_FILE"

if ! wait_for_service 8000 "FastAPI"; then
    echo -e "${RED}✗ FastAPI failed to start${NC}"
    echo -e "${RED}Showing last 30 lines of log:${NC}"
    tail -30 "$LOG_DIR/fastapi.log"
    cleanup_on_error
fi

echo -e "${GREEN}✓ FastAPI running (PID: $FASTAPI_PID)${NC}"
echo -e "  - API: :8000\n"

# 3. Verify all services
echo -e "${BLUE}[3/3] Verifying services...${NC}"

# Health check
sleep 2
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ FastAPI health check passed${NC}"
else
    echo -e "${YELLOW}⚠ FastAPI health check failed (may need more time)${NC}"
fi

# Display final status
echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  All services started successfully!${NC}"
echo -e "${GREEN}================================================${NC}\n"

echo -e "${BLUE}Service Access:${NC}"
echo -e "  🌐 Web UI:        ${GREEN}http://localhost:8000${NC}"
echo -e "  📊 SeaweedFS UI:  http://localhost:9333"
echo -e "  📁 Filer UI:      http://localhost:8888"

echo -e "\n${BLUE}Logs:${NC} $LOG_DIR"
echo -e "  - seaweedfs.log"
echo -e "  - fastapi.log"

echo -e "\n${BLUE}Management:${NC}"
echo -e "  Status:  ${GREEN}./scripts/status.sh${NC}"
echo -e "  Stop:    ${GREEN}./scripts/stop.sh${NC}"
echo -e "  Restart: ${GREEN}./scripts/restart.sh${NC}\n"

echo -e "${BLUE}PIDs stored in:${NC} $PID_FILE"
cat "$PID_FILE" | while read pid; do
    if ps -p $pid > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} PID $pid running"
    fi
done
echo ""
