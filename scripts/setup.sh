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
         "$REPO_DIR/logs" \
         "$REPO_DIR/config" \
         "$HOME/.seaweedfs"

# Check for environment variables
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo -e "${YELLOW}⚠ OPENROUTER_API_KEY not set${NC}"
    echo "Export it: export OPENROUTER_API_KEY='your-key'"
fi

# ── 1. Python dependencies ────────────────────────────────────────
echo -e "${BLUE}[1/3] Installing Python dependencies...${NC}"
cd "$REPO_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn python-multipart requests psutil \
    kreuzberg lancedb tantivy pyyaml openai httpx

echo -e "${GREEN}✓ Python dependencies installed${NC}\n"

# ── 2. SeaweedFS ──────────────────────────────────────────────────
echo -e "${BLUE}[2/3] Setting up SeaweedFS...${NC}"

SEAWEED_VERSION="3.95"
mkdir -p "$DATA_DIR/seaweedfs"

# Detect OS and ARCH
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

# Check current installed version
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

    # Remove old binary and any stale tar
    rm -f "$DATA_DIR/seaweedfs/weed"
    rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"

    # Download
    curl -L "$SEAWEED_URL" -o "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"

    # Verify it's actually a tar (not a 404 HTML/redirect)
    FILE_SIZE=$(wc -c < "$DATA_DIR/seaweedfs/seaweedfs.tar.gz")
    if [ "$FILE_SIZE" -lt 1000 ]; then
        echo -e "${RED}✗ Download failed — file too small (${FILE_SIZE} bytes). Check the URL:${NC}"
        echo -e "  $SEAWEED_URL"
        rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"
        exit 1
    fi

    tar -xzf "$DATA_DIR/seaweedfs/seaweedfs.tar.gz" -C "$DATA_DIR/seaweedfs/"
    rm -f "$DATA_DIR/seaweedfs/seaweedfs.tar.gz"
    chmod +x "$DATA_DIR/seaweedfs/weed"

    # Confirm
    INSTALLED=$("$DATA_DIR/seaweedfs/weed" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    if [ "$INSTALLED" = "$SEAWEED_VERSION" ]; then
        echo -e "${GREEN}✓ SeaweedFS v${INSTALLED} installed successfully${NC}"
    else
        echo -e "${YELLOW}⚠ Installed version ${INSTALLED} (expected ${SEAWEED_VERSION}) — may still work${NC}"
    fi
fi

echo -e "${GREEN}✓ SeaweedFS ready${NC}\n"

# ── 3. Configs ────────────────────────────────────────────────────
echo -e "${BLUE}[3/3] Writing configs...${NC}"

# Always regenerate filer.toml with correct absolute paths
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

# Deploy filer.toml to every path SeaweedFS auto-discovers
cp "$REPO_DIR/config/filer.toml" "$DATA_DIR/seaweedfs/filer.toml"
cp "$REPO_DIR/config/filer.toml" "$HOME/.seaweedfs/filer.toml"

echo -e "${GREEN}✓ filer.toml deployed to:${NC}"
echo -e "  $REPO_DIR/config/filer.toml"
echo -e "  $DATA_DIR/seaweedfs/filer.toml"
echo -e "  $HOME/.seaweedfs/filer.toml"

# notification.toml — webhook config (separate file from filer.toml)
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


# config.yaml — only create if missing
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
echo -e "  Python:    $(python3 --version 2>/dev/null)"

echo -e "\n${BLUE}Set your API keys:${NC}"
echo -e "  export OPENROUTER_API_KEY='your-key'"
echo -e "  export JINA_API_KEY='your-key'  # Optional\n"

echo -e "${BLUE}Then start with:${NC}"
echo -e "  ${GREEN}./scripts/start.sh${NC}\n"
