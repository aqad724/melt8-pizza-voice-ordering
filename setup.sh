#!/bin/bash
set -e

APP_NAME="melt8"
APP_DIR="/opt/$APP_NAME"
DOMAIN="pizza.autoreply.my"
PYTHON_BIN="python3"

echo "🚀 Updating system..."
apt update && apt upgrade -y
apt install -y git $PYTHON_BIN $PYTHON_BIN-venv $PYTHON_BIN-pip nginx certbot python3-certbot-nginx

echo "📂 Cloning repository..."
rm -rf $APP_DIR
git clone https://github.com/aqad724/melt8-pizza-voice-ordering.git $APP_DIR

echo "🐍 Setting up virtual environment..."
cd $APP_DIR
$PYTHON_BIN -m venv venv
source venv/bin/activate

echo "📦 Creating requirements.txt..."
cat > requirements.txt <<EOF
fastapi
uvicorn[standard]
websockets
python-dotenv
twilio
mysql-connector-python
gunicorn
EOF

echo "📦 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "🔑 Creating .env file..."
cat > .env <<EOF
OPENAI_API_KEY=your-openai-key-here

# Remote MySQL (Interserver)
DB_HOST=your-db-hostname
DB_USER=your-db-username
DB_PASS=your-db-password
DB_NAME=your-db-name

CHEF_USERNAME=chef
CHEF_PASSWORD=pizza123
PORT=5000
EOF
echo "⚠️ Remember to edit $APP_DIR/.env with your real keys and MySQL info!"

echo "🛠️ Creating systemd service..."
cat > /etc/systemd/system/$APP_NAME.service <<EOF
[Unit]
Description=$APP_NAME FastAPI app
After=network.target

[Service]
User=root
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/venv/bin"
ExecStart=$APP_DIR/venv/bin/gunicorn -w 2 -k uvicorn.workers.UvicornWorker app:app --bind 127.0.0.1:5000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $APP_NAME
systemctl start $APP_NAME

echo "🌐 Configuring Nginx..."
cat > /etc/nginx/sites-available/$APP_NAME <<EOF
server {
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -s /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/
nginx -t && systemctl restart nginx

echo "🔒 Setting up SSL with Certbot..."
certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m abpro786@gmail.com

echo "✅ Deployment complete!"
echo "Your app should now be live at: https://$DOMAIN"
echo "Edit your environment variables in $APP_DIR/.env"
