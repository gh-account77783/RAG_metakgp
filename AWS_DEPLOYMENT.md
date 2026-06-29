# 🧠 AWS EC2 Deployment Guide: GraphMind Remote MCP Server

This guide provides a step-by-step walkthrough to deploy the **GraphMind Remote MCP Server** on AWS. It uses a single-instance architecture where uvicorn, Neo4j (Docker), and ChromaDB (local WAL-mode SQLite) run on a single **Ubuntu 22.04 LTS** virtual machine.

---

## 🏛️ Architecture Overview

The system runs entirely on a single **AWS EC2 instance** to minimize architectural complexity and infrastructure costs. 

* **FastAPI + FastMCP**: Serves tools over Server-Sent Events (SSE) on port `8000`.
* **Nginx**: Acts as a reverse proxy, termination endpoint for SSL (HTTPS), and routes traffic to uvicorn.
* **Neo4j**: Runs locally inside a Docker container (bound to localhost for security).
* **ChromaDB**: Runs as a local SQLite backend, configured in Read-Only mode for the web worker and Write-Ahead Logging (WAL) mode for database updates.

---

## 🛠️ Step 1: Provision the AWS EC2 Instance

1. Log in to your [AWS Management Console](https://console.aws.amazon.com/).
2. Navigate to **EC2** and click **Launch Instance**.
3. **Configure the Instance settings**:
   * **Name**: `graphmind-mcp-server`
   * **OS Image (AMI)**: `Ubuntu Server 22.04 LTS (HVM), SSD Volume Type` (64-bit x86).
   * **Instance Type**: Select `t3.medium` (2 vCPUs, 4 GiB RAM) or `t3.large` (2 vCPUs, 8 GiB RAM). *Avoid `t2.micro` or `t3.micro` as building indices and running model queries will run out of memory.*
   * **Key Pair**: Create or select an existing SSH key pair (`.pem` file) to log in.
4. **Configure Network / Security Group**:
   Create a new security group and add the following **Inbound Rules**:

   | Port Range | Protocol | Source | Description |
   | :--- | :---: | :--- | :--- |
   | `22` | TCP | `My IP` (or `0.0.0.0/0`) | SSH Administration access |
   | `80` | TCP | `0.0.0.0/0` | HTTP traffic (needed for Certbot verification) |
   | `443` | TCP | `0.0.0.0/0` | HTTPS traffic (secure MCP server connection) |

5. **Configure Storage**: Set Root volume to **30 GB** (gp3) to accommodate database files and docker containers.
6. Click **Launch Instance**.

---

## 🌐 Step 2: Set Up DNS (A-Record)

To obtain an SSL certificate via Let's Encrypt automatically:
1. Copy the **Public IPv4 address** of your newly launched EC2 instance from the console.
2. Log in to your DNS provider (e.g., Cloudflare, Route53, Namecheap).
3. Create a new **A Record**:
   * **Name/Host**: `graphmind` (or `@` for root domain)
   * **Value/IP**: Paste the EC2 Public IPv4 address.
   * **TTL**: Auto or 3600 seconds.

---

## 📁 Step 3: Transfer DB Embeddings and Environment Configurations

Before configuring the server, you need to copy your local database files and environment configurations from your **local machine** to the server.

On your **local machine**, open a terminal and run:

```bash
# 1. Zip the local VectorStore directory to speed up the transfer
tar -czvf vector_store.tar.gz VectorStore

# 2. Transfer the zipped VectorStore, your local .env, and the neo4j data to the EC2 server
# (Replace 'your-key.pem' and 'your-ec2-ip' with your key path and EC2 public IP)
scp -i your-key.pem vector_store.tar.gz ubuntu@your-ec2-ip:/home/ubuntu/
scp -i your-key.pem .env ubuntu@your-ec2-ip:/home/ubuntu/
```

---

## 🚀 Step 4: Run the Auto-Deployment Script

Now, log in to your EC2 instance using SSH and execute the packaged deployment script.

```bash
# 1. Connect to the EC2 server
ssh -i your-key.pem ubuntu@your-ec2-ip

# 2. Move variables template and unpack database files
mkdir -p /home/ubuntu/RAG_and_MCP_examples/VectorStore
tar -xzvf /home/ubuntu/vector_store.tar.gz -C /home/ubuntu/RAG_and_MCP_examples/
mv /home/ubuntu/.env /home/ubuntu/RAG_and_MCP_examples/

# 3. Clone repository and run the deployment script
# (The script installs Docker, Nginx, Python, sets up systemd, and configures SSL automatically)
cd /home/ubuntu/RAG_and_MCP_examples
chmod +x deploy.sh

# Run the deployment script with your domain name as an argument
sudo ./deploy.sh graphmind.your-domain.com
```

---

## 🔄 Step 5: Post-Deployment Verification

Once the script completes, verify that all services are running correctly:

### A. Check application systemd status
```bash
sudo systemctl status graphmind.service
```
You can read live logs using journalctl:
```bash
sudo journalctl -u graphmind.service -f
```

### B. Verify Docker containers are running (Neo4j)
```bash
docker ps
```
Verify Neo4j is running on port `7687` (localhost only).

### C. Verify Nginx & SSL
Visit `https://graphmind.your-domain.com/login` in your web browser. You should be redirected to the Google login screen. Upon successful authentication, it will render your JWT authorization token on the success callback page.

---

## 🔌 Step 6: Connect to your Client

Configure your local MCP client (such as Cursor or Claude Desktop) using either the static API key or the temporary Google OAuth token.

### Option A: Static API Key (Recommended for IDEs / Headless scripts)
Add the static API key configured in `/home/ubuntu/RAG_and_MCP_examples/.env` under the header `Authorization`:

```json
{
  "mcpServers": {
    "graphmind": {
      "type": "sse",
      "url": "https://graphmind.your-domain.com/mcp/sse",
      "headers": {
        "Authorization": "Bearer <your_static_api_key>"
      }
    }
  }
}
```

### Option B: Google OAuth Token (Browser copy-paste)
Navigate to `https://graphmind.your-domain.com/login`, copy the short-lived JWT token returned, and add it to your configuration file:

```json
{
  "mcpServers": {
    "graphmind": {
      "type": "sse",
      "url": "https://graphmind.your-domain.com/mcp/sse",
      "headers": {
        "Authorization": "Bearer <your_google_oauth_token>"
      }
    }
  }
}
```
