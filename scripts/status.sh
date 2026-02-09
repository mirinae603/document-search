#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_DIR/.pids"
LOG_DIR="$REPO_DIR/logs"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Document Search System - Status${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Function to check if port is in use
check_port() {
    if lsof -i :$1 >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Running${NC}"
        return 0
    else
        echo -e "${RED}✗ Not running${NC}"
        return 1
    fi
}

# Function to get process info
get_process_info() {
    local pid=$1
    if ps -p $pid > /dev/null 2>&1; then
        local cpu=$(ps -p $pid -o %cpu= | xargs)
        local mem=$(ps -p $pid -o %mem= | xargs)
        local time=$(ps -p $pid -o etime= | xargs)
        echo -e "${GREEN}Running${NC} (CPU: ${cpu}%, MEM: ${mem}%, Up: $time)"
        return 0
    else
        echo -e "${RED}Not running${NC}"
        return 1
    fi
}

# Check PID file
echo -e "${BLUE}PID File Status:${NC}"
if [ -f "$PID_FILE" ]; then
    echo -e "  ${GREEN}✓${NC} Found: $PID_FILE"
    echo -e "  PIDs: $(cat $PID_FILE | tr '\n' ' ')"
else
    echo -e "  ${RED}✗${NC} Not found: $PID_FILE"
    echo -e "  ${YELLOW}Services may not be running${NC}"
fi
echo ""

# Check services by port
echo -e "${BLUE}Service Status (by port):${NC}"

echo -n "  SeaweedFS Master (:9333):  "
check_port 9333

echo -n "  SeaweedFS Volume (:8080):  "
check_port 8080

echo -n "  SeaweedFS Filer  (:8888):  "
check_port 8888

echo -n "  FastAPI          (:8000):  "
check_port 8000

echo ""

# Check processes by PID
if [ -f "$PID_FILE" ]; then
    echo -e "${BLUE}Process Status (by PID):${NC}"
    
    PIDS=($(cat "$PID_FILE"))
    if [ ${#PIDS[@]} -ge 1 ]; then
        echo -n "  SeaweedFS (${PIDS[0]}):  "
        get_process_info ${PIDS[0]}
    fi
    
    if [ ${#PIDS[@]} -ge 2 ]; then
        echo -n "  FastAPI   (${PIDS[1]}):  "
        get_process_info ${PIDS[1]}
    fi
    echo ""
fi

# Health check
echo -e "${BLUE}Health Checks:${NC}"

# FastAPI health
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    HEALTH=$(curl -s http://localhost:8000/health | python3 -m json.tool 2>/dev/null)
    echo -e "  FastAPI API:     ${GREEN}✓ Healthy${NC}"
    if [ ! -z "$HEALTH" ]; then
        echo "$HEALTH" | sed 's/^/    /'
    fi
else
    echo -e "  FastAPI API:     ${RED}✗ Unhealthy${NC}"
fi

# SeaweedFS master
if curl -s http://localhost:9333/cluster/status > /dev/null 2>&1; then
    echo -e "  SeaweedFS API:   ${GREEN}✓ Healthy${NC}"
else
    echo -e "  SeaweedFS API:   ${RED}✗ Unhealthy${NC}"
fi

echo ""

# Log files
echo -e "${BLUE}Recent Logs:${NC}"
if [ -f "$LOG_DIR/fastapi.log" ]; then
    echo -e "  FastAPI (last 5 lines):"
    tail -5 "$LOG_DIR/fastapi.log" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"
else
    echo -e "  FastAPI: ${YELLOW}No log file${NC}"
fi

echo ""

if [ -f "$LOG_DIR/seaweedfs.log" ]; then
    echo -e "  SeaweedFS (last 5 lines):"
    tail -5 "$LOG_DIR/seaweedfs.log" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"
else
    echo -e "  SeaweedFS: ${YELLOW}No log file${NC}"
fi

echo ""

# Disk usage
echo -e "${BLUE}Disk Usage:${NC}"
if [ -d "$REPO_DIR/data" ]; then
    du -sh "$REPO_DIR/data"/* 2>/dev/null | sed 's/^/  /'
fi

echo ""
echo -e "${BLUE}================================================${NC}\n"
