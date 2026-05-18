#!/bin/bash
# ContractSentinel — One-command Vultr deploy
# Usage: bash deploy.sh YOUR_VULTR_IP YOUR_GEMINI_API_KEY

set -e

VULTR_IP=$1
GEMINI_KEY=$2

if [ -z "$VULTR_IP" ] || [ -z "$GEMINI_KEY" ]; then
  echo "Usage: bash deploy.sh YOUR_VULTR_IP YOUR_GEMINI_API_KEY"
  exit 1
fi

echo "🚀 Deploying ContractSentinel to Vultr ($VULTR_IP)..."

# Prepare static folder
mkdir -p backend/static
cp frontend/index.html backend/static/

# SSH and setup on Vultr VM
ssh -o StrictHostKeyChecking=no root@$VULTR_IP << EOF
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose git
  systemctl start docker
  
  # Clean previous deploy
  rm -rf /opt/contractsentinel
  mkdir -p /opt/contractsentinel
EOF

# Copy files to Vultr
echo "📦 Copying files..."
scp -o StrictHostKeyChecking=no -r \
  backend/ frontend/ Dockerfile docker-compose.yml README.md \
  root@$VULTR_IP:/opt/contractsentinel/

# Build and run on Vultr
ssh -o StrictHostKeyChecking=no root@$VULTR_IP << EOF
  cd /opt/contractsentinel
  
  # Prepare static
  mkdir -p backend/static
  cp frontend/index.html backend/static/
  
  # Set API key
  export GEMINI_API_KEY="$GEMINI_KEY"
  echo "GEMINI_API_KEY=$GEMINI_KEY" > .env
  
  # Stop old container if running
  docker-compose down 2>/dev/null || true
  
  # Build and start
  docker-compose up -d --build
  
  echo "✅ Deployed! App running at http://$VULTR_IP"
EOF

echo ""
echo "✅ ContractSentinel is live at: http://$VULTR_IP"
echo "📋 Test the API: curl http://$VULTR_IP/health"
