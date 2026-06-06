#!/usr/bin/env bash
# ==============================================================================
# GraphMind Remote MCP Server Auto-Deployment Script
# ==============================================================================
# This script automates the manual deployment guide in mcp_server_plan.md
# Run this on the AWS EC2 instance (Ubuntu 22.04 LTS recommended).
# ==============================================================================

set -euo pipefail

# Configuration
INSTALL_DIR="/home/ubuntu/RAG_and_MCP_examples"
SERVICE_NAME="graphmind"
NGINX_CONF="/etc/nginx/sites-available/graphmind"

# 1. Print header
echo "=============================================================================="
echo "🧠 GraphMind Remote MCP Server Auto-Deployment Script"
echo "=============================================================================="

# Check if run as root/sudo for packages
if [ "$EUID" -ne 0 ]; then
    echo "❌ Please run this script with sudo or as root."
    exit 1
fi

# Get current user who ran sudo
ACTUAL_USER="${SUDO_USER:-ubuntu}"
echo "👤 Current user: $ACTUAL_USER"
echo "📂 Installation directory: $INSTALL_DIR"

# Prompt for domain name if not provided as argument
DOMAIN_NAME="${1:-}"
if [ -z "$DOMAIN_NAME" ]; then
    read -rp "🌐 Enter your domain name (e.g., graphmind.your-domain.com): " DOMAIN_NAME
fi

if [ -z "$DOMAIN_NAME" ]; then
    echo "❌ Error: A valid domain name is required for SSL/Nginx configuration."
    exit 1
fi

# 2. System Dependencies & Docker Setup
echo -e "\n=== 🛠️ Step A: Installing System Dependencies & Docker ==="
apt-get update && apt-get upgrade -y
apt-get install -y git python3-pip python3-venv docker.io nginx certbot python3-certbot-nginx sqlite3

# Start and enable Docker
systemctl enable --now docker
usermod -aG docker "$ACTUAL_USER"
echo "✅ Docker setup complete. (Note: Group changes apply to $ACTUAL_USER on next login)"

# 3. Code Setup & Virtual Environment
echo -e "\n=== 📦 Step B: Code Setup & Virtual Environment ==="
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Cloning repository..."
    git clone https://github.com/gh-account77783/RAG_metakgp.git "$INSTALL_DIR"
    chown -R "$ACTUAL_USER":"$ACTUAL_USER" "$INSTALL_DIR"
else
    echo "Directory $INSTALL_DIR already exists. Pulling latest code..."
    cd "$INSTALL_DIR"
    git pull || echo "Failed to pull git repo, continuing with existing files..."
fi

cd "$INSTALL_DIR"

# Setup virtual environment as actual user to avoid root ownership issues
echo "Setting up virtual environment..."
sudo -u "$ACTUAL_USER" python3 -m venv venv
sudo -u "$ACTUAL_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$ACTUAL_USER" "$INSTALL_DIR/venv/bin/pip" install -r requirements.txt
echo "✅ Python environment and dependencies installed."

# 4. Check Environment Variables
echo -e "\n=== 🔑 Step C: Environment Variables Configuration ==="
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "⚠️ .env file not found in $INSTALL_DIR. Creating template..."
    sudo -u "$ACTUAL_USER" bash -c "cat > '$ENV_FILE' <<EOF
# GraphMind Server Configurations
GROQ_API_KEY=
ollama_api_key=
OLLAMA_BASE_URL=https://ollama.com

# Neo4j Settings
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password

# OAuth & API Key Settings
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://$DOMAIN_NAME/callback
GRAPHMIND_API_KEY=$(openssl rand -hex 16)
EOF"
    echo "✅ Created template .env file at $ENV_FILE"
    echo "🚨 IMPORTANT: Please edit this file to fill in your API keys and Google OAuth secrets."
else
    echo "✅ Found existing .env file. Updating GOOGLE_REDIRECT_URI..."
    # Ensure GOOGLE_REDIRECT_URI is set correctly
    if grep -q "GOOGLE_REDIRECT_URI" "$ENV_FILE"; then
        sed -i "s|GOOGLE_REDIRECT_URI=.*|GOOGLE_REDIRECT_URI=https://$DOMAIN_NAME/callback|g" "$ENV_FILE"
    else
        echo "GOOGLE_REDIRECT_URI=https://$DOMAIN_NAME/callback" >> "$ENV_FILE"
    fi
fi

# 5. Run Neo4j Database
echo -e "\n=== 💾 Step D: Starting Neo4j Container ==="
mkdir -p "$INSTALL_DIR/neo4j/data"
chown -R 7474:7474 "$INSTALL_DIR/neo4j/data"

# Extract Neo4j password from env file (defaulting to 'password' if not set)
NEO4J_PASS=$(grep "NEO4J_PASSWORD" "$ENV_FILE" | cut -d'=' -f2- || echo "password")
if [ -z "$NEO4J_PASS" ]; then NEO4J_PASS="password"; fi

if docker ps -a --format '{{.Names}}' | grep -Eq "^neo4j$"; then
    echo "Neo4j container already exists. Restarting..."
    docker restart neo4j
else
    echo "Starting new Neo4j docker container..."
    docker run -d --name neo4j \
      -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
      -v "$INSTALL_DIR/neo4j/data":/data \
      --env NEO4J_AUTH="neo4j/$NEO4J_PASS" \
      --env NEO4J_server_memory_heap_initial__size=512m \
      --env NEO4J_server_memory_heap_max__size=1g \
      --env NEO4J_server_memory_pagecache_size=512m \
      --restart always \
      neo4j:latest
fi
echo "✅ Neo4j database is starting in background."

# 6. SQLite WAL Mode
echo -e "\n=== 🗃️ Step E: Database WAL Mode Setup ==="
mkdir -p "$INSTALL_DIR/VectorStore"
chown -R "$ACTUAL_USER":"$ACTUAL_USER" "$INSTALL_DIR/VectorStore"

SQLITE_DB="$INSTALL_DIR/VectorStore/chroma.sqlite3"
if [ -f "$SQLITE_DB" ]; then
    echo "Enabling SQLite Write-Ahead Logging (WAL) for ChromaDB..."
    sqlite3 "$SQLITE_DB" "PRAGMA journal_mode=WAL;"
    echo "✅ SQLite WAL mode configured."
else
    echo "ℹ️ chroma.sqlite3 not found yet. It will be initialized on first run."
fi

# 7. Systemd Service Setup
echo -e "\n=== ⚙️ Step F: Systemd Service Setup ==="
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=GraphMind MCP Server (FastAPI Daemon)
After=network.target docker.service

[Service]
User=$ACTUAL_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/uvicorn mcp_server:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
EnvironmentFile=$INSTALL_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
echo "✅ Systemd service installed and started."

# 8. Nginx Reverse Proxy Setup
echo -e "\n=== 🌐 Step G: Nginx & HTTPS Configuration ==="
cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN_NAME;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /mcp {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        keepalive_timeout 3600s;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "✅ Nginx reverse proxy configured."

# SSL Setup with Certbot
echo "Starting Certbot SSL registration..."
if certbot --nginx -d "$DOMAIN_NAME" --non-interactive --agree-tos -m "admin@$DOMAIN_NAME"; then
    echo "✅ SSL Certificate successfully acquired and Nginx restarted with HTTPS."
else
    echo "❌ Certbot registration failed. Ensure your domain '$DOMAIN_NAME' DNS A-Record points to this server's public IP, and port 80/443 are open."
fi

echo -e "\n=============================================================================="
echo "🎉 Deployment Setup Script Complete!"
echo "=============================================================================="
echo "Next Steps:"
echo "1. Edit the env file at $ENV_FILE to update your API keys & OAuth secrets:"
echo "   sudo nano $ENV_FILE"
echo "2. Restart the GraphMind service after editing env:"
echo "   sudo systemctl restart $SERVICE_NAME"
echo "3. Copy your local VectorStore/ directory if not already populated."
echo "4. Use your static API key or log in at: https://$DOMAIN_NAME/login"
echo "=============================================================================="
