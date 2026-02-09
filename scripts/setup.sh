#!/bin/bash

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Document Search - Modular Setup${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Create directories
mkdir -p "$DATA_DIR/seaweedfs" "$DATA_DIR/lancedb" "$REPO_DIR/logs" "$REPO_DIR/config"

# Check for environment variables
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo -e "${RED}⚠ OPENROUTER_API_KEY not set${NC}"
    echo "Export it: export OPENROUTER_API_KEY='your-key'"
fi

# 1. Install Python dependencies
echo -e "${BLUE}[1/3] Installing Python dependencies...${NC}"
cd "$REPO_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn python-multipart requests psutil \
    kreuzberg lancedb tantivy pyyaml

echo -e "${GREEN}✓ Python dependencies installed${NC}\n"

# 2. Setup SeaweedFS (same as before)
echo -e "${BLUE}[2/3] Setting up SeaweedFS...${NC}"
cd "$DATA_DIR"

if [ ! -f "seaweedfs/weed" ]; then
    echo "Downloading SeaweedFS..."
    
    OS=$(uname -s)
    ARCH=$(uname -m)
    
    if [ "$OS" = "Darwin" ]; then
        if [ "$ARCH" = "arm64" ]; then
            URL="https://github.com/seaweedfs/seaweedfs/releases/download/3.65/darwin_arm64.tar.gz"
        else
            URL="https://github.com/seaweedfs/seaweedfs/releases/download/3.65/darwin_amd64.tar.gz"
        fi
    elif [ "$OS" = "Linux" ]; then
        URL="https://github.com/seaweedfs/seaweedfs/releases/download/3.65/linux_amd64.tar.gz"
    else
        echo -e "${RED}Unsupported OS: $OS${NC}"
        exit 1
    fi
    
    curl -L "$URL" -o seaweedfs.tar.gz
    tar -xzf seaweedfs.tar.gz -C seaweedfs/
    rm seaweedfs.tar.gz
    chmod +x seaweedfs/weed
fi

echo -e "${GREEN}✓ SeaweedFS ready${NC}\n"

# 3. Create config if not exists
echo -e "${BLUE}[3/3] Creating config...${NC}"

if [ ! -f "$REPO_DIR/config/config.yaml" ]; then
    cat > "$REPO_DIR/config/config.yaml" << 'EOF'
# Copy the config.yaml content from above
EOF
    echo -e "${GREEN}✓ Config created${NC}"
else
    echo -e "${GREEN}✓ Config already exists${NC}"
fi

echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  Setup completed!${NC}"
echo -e "${GREEN}================================================${NC}\n"
echo -e "${BLUE}Set your API keys:${NC}"
echo -e "  export OPENROUTER_API_KEY='your-key'"
echo -e "  export JINA_API_KEY='your-key'  # Optional\n"
