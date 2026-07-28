#!/bin/bash
# ============================================================
#  OKX TradFi 监控系统 — 云服务器一键部署脚本
#  
#  适用：腾讯云/阿里云 轻量服务器 (CentOS 7/8, Ubuntu 20/22)
#  安装 Python + 依赖 + systemd 服务（开机自启, 断线自动重启）
# ============================================================

set -e
echo "========================================="
echo " OKX TradFi 监控系统 部署"
echo "========================================="

# 1. 安装 Python 3
echo "[1/5] 安装 Python ..."
if command -v apt &> /dev/null; then
    apt update -qq && apt install -y -qq python3 python3-pip python3-venv
elif command -v yum &> /dev/null; then
    yum install -y -q python3 python3-pip python3-devel
fi
echo "  Python: $(python3 --version)"

# 2. 安装依赖
echo "[2/5] 安装 Python 依赖 ..."
pip3 install -q pandas numpy pyyaml python-dotenv requests urllib3

# 3. 复制项目文件
echo "[3/5] 配置项目 ..."
PROJECT_DIR="/opt/okx_tradfi"
mkdir -p "$PROJECT_DIR"

# 复制核心文件（手动上传或 git clone 后执行此脚本）
if [ -d "./core" ]; then
    cp -r core notify scripts monitor.py config.yaml .env "$PROJECT_DIR/"
fi

# 4. 创建 systemd 服务（开机自启 + 崩溃自动重启）
echo "[4/5] 创建 systemd 服务 ..."
cat > /etc/systemd/system/okx-monitor.service << 'SERVICE'
[Unit]
Description=OKX TradFi Fractal Strategy Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/okx_tradfi
ExecStart=/usr/bin/python3 -u monitor.py
Restart=always
RestartSec=30
StandardOutput=append:/opt/okx_tradfi/logs/monitor.log
StandardError=append:/opt/okx_tradfi/logs/monitor.log

[Install]
WantedBy=multi-user.target
SERVICE

mkdir -p "$PROJECT_DIR/logs"

# 5. 启动服务
echo "[5/5] 启动服务 ..."
systemctl daemon-reload
systemctl enable okx-monitor
systemctl restart okx-monitor

echo ""
echo "========================================="
echo " 部署完成！"
echo "========================================="
echo ""
echo " 常用命令:"
echo "   查看状态:  systemctl status okx-monitor"
echo "   查看日志:  tail -f /opt/okx_tradfi/logs/monitor.log"
echo "   重启服务:  systemctl restart okx-monitor"
echo "   停止服务:  systemctl stop okx-monitor"
echo "   手动运行:  cd /opt/okx_tradfi && python3 monitor.py --once"
echo ""
