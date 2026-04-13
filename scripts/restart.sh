#!/bin/bash

GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Restarting Document Search System${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Stop services
echo -e "${BLUE}Step 1: Stopping services...${NC}"
bash "$REPO_DIR/scripts/stop.sh"

# Wait a moment
echo -e "\n${BLUE}Waiting 3 seconds...${NC}"
sleep 3

# Start services
echo -e "\n${BLUE}Step 2: Starting services...${NC}"
bash "$REPO_DIR/scripts/start.sh"

echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  Restart complete${NC}"
echo -e "${GREEN}================================================${NC}\n"
