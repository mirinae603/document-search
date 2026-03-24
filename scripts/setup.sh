#!/bin/bash

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Document Search - Setup${NC}"
echo -e "${BLUE}================================================${NC}\n"

# Create all required directories
mkdir -p "$DATA_DIR/seaweedfs/filerldb2" \
         "$DATA_DIR/lancedb" \
         "$DATA_DIR/redis" \
         "$REPO_DIR/logs" \
         "$REPO_DIR/config" \
         "$HOME/.seaweedfs"

# Check for environment variables
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo -e "${YELLOW}⚠ OPENROUTER_API_KEY not set${NC}"
    echo "Export it: export OPENROUTER_API_KEY='your-key'"
fi

# ── 1. Python dependencies ────────────────────────────────────────
echo -e "${BLUE}[1/4] Installing Python dependencies...${NC}"
cd "$REPO_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo -e "${GREEN}✓ Python dependencies installed${NC}\n"

# ── 2. SeaweedFS ──────────────────────────────────────────────────
echo -e "${BLUE}[2/4] Setting up SeaweedFS...${NC}"

SEAWEED_VERSION="3.95"
mkdir -p "$DATA_DIR/seaweedfs"

OS=$(uname -s)
ARCH=$(uname -m)

if [ "$OS" = "Darwin" ]; then
    if [ "$ARCH" = "arm64" ]; then
        SEAWEED_URL="https://github.com/seaweedfs/seaweedfs/releases/download/${SEAWEED_VERSION}/darwin_arm64.tar.gz"
    else
        SEAWEED_URL="https://github.com/seaweedfs/seaweedfs/releases/download/${SEAWEED_VERSION}/darwin_amd64.tar.gz"
    fi
elif [ "$OS" = "Linux" ]; then
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then
        SEAWEED_URL="https://github.com/seaweedfs/seaweedfs/releases/download/${SEAWEED_VERSION}/linux_arm64.tar.gz"
    else
        SEAWEED_URL="https://github.com/seaweedfs/seaweedfs/releases/download/${SEAWEED_VERSION}/linux_amd64.tar.gz"
    fi
else
    echo -e "${RED}Unsupported OS: $OS${NC}"
    exit 1
fi

CURRENT_VERSION=""
if [ -f "$DATA_DIR/seaweedfs/weed" ]; then
    CURRENT_VERSION=$("$DATA_DIR/seaweedfs/weed" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "")
fi

if [ "$CURRENT_VERSION" = "$SEAWEED_VERSION" ]; then
    echo -e "${GREEN}✓ SeaweedFS v${SEAWEED_VERSION} already installed${NC}"
else
    if [ -n "$CURRENT_VERSION" ]; then
        echo -e "${YELLOW}⚠ Upgrading SeaweedFS: ${CURRENT_VERSION} → ${SEAWEED_VERSION}${NC}"
    else
        echo -e "Downloading SeaweedFS v${SEAWEED_VERSION} (${OS} ${ARCH})..."
    fi

    rm -f "$DATA_DIR/seaweedfs/weed"
    rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"

    curl -L "$SEAWEED_URL" -o "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"

    FILE_SIZE=$(wc -c < "$DATA_DIR/seaweedfs/seaweedfs.tar.gz")
    if [ "$FILE_SIZE" -lt 1000 ]; then
        echo -e "${RED}✗ Download failed — file too small (${FILE_SIZE} bytes).${NC}"
        rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"
        exit 1
    fi

    tar -xzf "$DATA_DIR/seaweedfs/seaweedfs.tar.gz" -C "$DATA_DIR/seaweedfs/"
    rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"
    chmod +x "$DATA_DIR/seaweedfs/weed"

    INSTALLED=$("$DATA_DIR/seaweedfs/weed" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    if [ "$INSTALLED" = "$SEAWEED_VERSION" ]; then
        echo -e "${GREEN}✓ SeaweedFS v${INSTALLED} installed successfully${NC}"
    else
        echo -e "${YELLOW}⚠ Installed version ${INSTALLED} (expected ${SEAWEED_VERSION})${NC}"
    fi
fi

echo -e "${GREEN}✓ SeaweedFS ready${NC}\n"

# ── 3. Redis ──────────────────────────────────────────────────────
echo -e "${BLUE}[3/4] Checking Redis...${NC}"

if command -v redis-server &> /dev/null; then
    REDIS_VERSION=$(redis-server --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    echo -e "${GREEN}✓ Redis v${REDIS_VERSION} already installed${NC}"
else
    echo -e "${YELLOW}⚠ Redis not found — installing...${NC}"
    if [ "$OS" = "Darwin" ]; then
        if command -v brew &> /dev/null; then
            brew install redis
            echo -e "${GREEN}✓ Redis installed via Homebrew${NC}"
        else
            echo -e "${RED}✗ Homebrew not found. Install manually: brew install redis${NC}"
        fi
    elif [ "$OS" = "Linux" ]; then
        if command -v apt-get &> /dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y redis-server
            echo -e "${GREEN}✓ Redis installed via apt${NC}"
        elif command -v yum &> /dev/null; then
            sudo yum install -y redis
            echo -e "${GREEN}✓ Redis installed via yum${NC}"
        else
            echo -e "${RED}✗ Cannot auto-install Redis: https://redis.io/docs/getting-started/installation/${NC}"
        fi
    fi
fi

# ── Write redis.conf with fully-expanded absolute paths ──────────
# IMPORTANT: dir and logfile must be absolute — Redis 7+ rejects
# relative paths and missing dirs at config parse time.
# We mkdir here AND write the absolute path to be safe.
mkdir -p "$DATA_DIR/redis"
mkdir -p "$REPO_DIR/logs"

REDIS_DATA_DIR="$DATA_DIR/redis"
REDIS_LOG_FILE="$REPO_DIR/logs/redis.log"

cat > "$REPO_DIR/config/redis.conf" << EOF
port 6379
daemonize yes
dir ${REDIS_DATA_DIR}
logfile ${REDIS_LOG_FILE}
appendonly yes
appendfilename "redis.aof"
maxmemory 256mb
maxmemory-policy allkeys-lru
save 900 1
save 300 10
EOF

echo -e "${GREEN}✓ redis.conf written → ${REDIS_DATA_DIR}${NC}\n"

# ── 4. Configs ────────────────────────────────────────────────────
echo -e "${BLUE}[4/4] Writing configs...${NC}"

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
echo -e "${GREEN}✓ filer.toml deployed${NC}"

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

if [ ! -f "$REPO_DIR/config/config.yaml" ]; then
    cat > "$REPO_DIR/config/config.yaml" << 'EOF'
# Add your config.yaml content here
EOF
    echo -e "${GREEN}✓ config.yaml created${NC}"
else
    echo -e "${GREEN}✓ config.yaml already exists${NC}"
fi

# ── Done ──────────────────────────────────────────────────────────
echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}================================================${NC}\n"

echo -e "${BLUE}Installed versions:${NC}"
echo -e "  SeaweedFS: $("$DATA_DIR/seaweedfs/weed" version 2>/dev/null | head -1 || echo 'unknown')"
echo -e "  Redis:     $(redis-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo 'not found')"
echo -e "  Python:    $(python3 --version 2>/dev/null)"

echo -e "\n${BLUE}Set your API keys:${NC}"
echo -e "  export OPENROUTER_API_KEY='your-key'"
echo -e "  export JINA_API_KEY='your-key'  # Optional\n"

echo -e "${BLUE}Then start with:${NC}"
echo -e "  ${GREEN}./scripts/start.sh${NC}\n"
