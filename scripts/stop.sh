#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_DIR/.pids"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Stopping Document Search System${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Function to gracefully stop a process
stop_process() {
    local pid=$1
    local name=$2
    local timeout=${3:-10}
    
    if ! ps -p $pid > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠ Process $pid ($name) not running${NC}"
        return 0
    fi
    
    echo -n "Stopping $name (PID: $pid)..."
    
    # Send SIGTERM for graceful shutdown
    kill -TERM $pid 2>/dev/null
    
    # Wait for process to exit
    for i in $(seq 1 $timeout); do
        if ! ps -p $pid > /dev/null 2>&1; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        fi
        sleep 1
    done
    
    # If still running, send SIGKILL
    echo -e " ${YELLOW}timeout${NC}"
    echo -n "Force killing $name (PID: $pid)..."
    kill -9 $pid 2>/dev/null
    sleep 1
    
    if ! ps -p $pid > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        return 0
    else
        echo -e " ${RED}✗ Failed${NC}"
        return 1
    fi
}

# Check if PID file exists
if [ ! -f "$PID_FILE" ]; then
    echo -e "${YELLOW}⚠ No PID file found${NC}"
    echo "Checking for running processes anyway..."
    
    # Try to find and kill processes by name
    pkill -TERM -f "weed server" 2>/dev/null && echo -e "${GREEN}✓ Killed SeaweedFS${NC}"
    pkill -TERM -f "main.py" 2>/dev/null && echo -e "${GREEN}✓ Killed FastAPI${NC}"
    
    sleep 2
    
    # Force kill if still running
    pkill -9 -f "weed server" 2>/dev/null
    pkill -9 -f "main.py" 2>/dev/null
    
    echo -e "\n${GREEN}Cleanup complete${NC}\n"
    exit 0
fi

# Read PIDs and stop in reverse order (FastAPI first, then SeaweedFS)
PIDS=($(cat "$PID_FILE"))
NUM_PIDS=${#PIDS[@]}

if [ $NUM_PIDS -eq 0 ]; then
    echo -e "${YELLOW}⚠ No PIDs found in file${NC}"
    rm -f "$PID_FILE"
    exit 0
fi

echo -e "${BLUE}Found $NUM_PIDS process(es) to stop${NC}\n"

# Stop FastAPI first (usually last PID)
if [ $NUM_PIDS -ge 2 ]; then
    FASTAPI_PID=${PIDS[$((NUM_PIDS-1))]}
    stop_process $FASTAPI_PID "FastAPI" 15
fi

# Stop SeaweedFS (usually first PID)
if [ $NUM_PIDS -ge 1 ]; then
    SEAWEED_PID=${PIDS[0]}
    stop_process $SEAWEED_PID "SeaweedFS" 20
fi

# Additional cleanup - find any orphaned processes
echo -e "\n${BLUE}Checking for orphaned processes...${NC}"

# Check for any remaining weed processes
if pgrep -f "weed server" > /dev/null 2>&1; then
    echo "Found orphaned SeaweedFS processes"
    pkill -TERM -f "weed server" 2>/dev/null
    sleep 3
    pkill -9 -f "weed server" 2>/dev/null
    echo -e "${GREEN}✓ Cleaned up orphaned SeaweedFS${NC}"
fi

# Check for any remaining FastAPI processes
if pgrep -f "main.py" > /dev/null 2>&1; then
    echo "Found orphaned FastAPI processes"
    pkill -TERM -f "main.py" 2>/dev/null
    sleep 3
    pkill -9 -f "main.py" 2>/dev/null
    echo -e "${GREEN}✓ Cleaned up orphaned FastAPI${NC}"
fi

# Remove PID file
rm -f "$PID_FILE"
echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  All services stopped${NC}"
echo -e "${GREEN}================================================${NC}\n"
