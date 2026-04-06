#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# User Data Script — installs Docker + nginx + certbot, runs the app container
# Sets up systemd timer for 24hr follow-up email cron job
# ---------------------------------------------------------------------------

# Install SSM agent first so instance is always reachable via SSM
dnf install -y amazon-ssm-agent
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# Install Docker, nginx, certbot, and cronie
dnf install -y docker nginx python3-certbot-nginx cronie
systemctl enable docker nginx crond
systemctl start docker

# Authenticate with ECR
aws ecr get-login-password --region ${aws_region} | docker login --username AWS --password-stdin ${ecr_repository_url}

# Pull the latest image
docker pull ${ecr_repository_url}:latest

# Write environment file
mkdir -p /opt/${service_name}

cat > /opt/${service_name}/.env <<'ENVFILE'
${env_file_content}
ENVFILE

# Run the main application container
docker run -d \
  --name ${service_name} \
  --restart always \
  --env-file /opt/${service_name}/.env \
  -p 127.0.0.1:8000:8000 \
  ${ecr_repository_url}:latest

# ---------------------------------------------------------------------------
# Follow-up email cron job — runs every 30 min, sends 24hr follow-up emails
# ---------------------------------------------------------------------------

cat > /etc/cron.d/${service_name}-followup <<'CRONFILE'
*/30 * * * * root docker exec ${service_name} python -m app.cron.followup_worker >> /var/log/${service_name}-followup.log 2>&1
CRONFILE

chmod 644 /etc/cron.d/${service_name}-followup

# ---------------------------------------------------------------------------
# nginx reverse proxy config with WebSocket support
# ---------------------------------------------------------------------------

cat > /etc/nginx/conf.d/${service_name}.conf <<'NGINXCONF'
server {
    listen 80;
    server_name ${domain};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # WebSocket support
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Long timeouts for WebSocket/session connections
        proxy_read_timeout  3600s;
        proxy_send_timeout  3600s;
    }
}
NGINXCONF

rm -f /etc/nginx/conf.d/default.conf
mkdir -p /var/www/certbot

systemctl start nginx

# Obtain TLS certificate via Let's Encrypt
certbot --nginx \
  -d ${domain} \
  --non-interactive \
  --agree-tos \
  --email ${certbot_email} \
  --redirect

systemctl reload nginx
systemctl enable --now certbot-renew.timer
