#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/logs"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Document Search System - Live Logs${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Function to display usage
usage() {
    echo "Usage: $0 [fastapi|seaweedfs|all]"
    echo ""
    echo "Options:"
    echo "  fastapi    - Show FastAPI logs"
    echo "  seaweedfs  - Show SeaweedFS logs"
    echo "  all        - Show all logs (default)"
    exit 1
}

LOG_TYPE=${1:-all}

case $LOG_TYPE in
    fastapi)
        echo -e "${GREEN}Following FastAPI logs...${NC}"
        echo -e "${BLUE}Press Ctrl+C to exit${NC}\n"
        tail -f "$LOG_DIR/fastapi.log"
        ;;
    seaweedfs)
        echo -e "${GREEN}Following SeaweedFS logs...${NC}"
        echo -e "${BLUE}Press Ctrl+C to exit${NC}\n"
        tail -f "$LOG_DIR/seaweedfs.log"
        ;;
    all)
        echo -e "${GREEN}Following all logs...${NC}"
        echo -e "${BLUE}Press Ctrl+C to exit${NC}\n"
        tail -f "$LOG_DIR/fastapi.log" "$LOG_DIR/seaweedfs.log"
        ;;
    *)
        usage
        ;;
esac
