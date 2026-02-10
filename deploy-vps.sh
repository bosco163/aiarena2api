#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 改成你的仓库
REPO_URL="https://github.com/tawer-blog/lmarena-2api.git"

echo -e "${BLUE}=== LMArena Proxy 512MB VPS 部署 ===${NC}"

# 检查资源
MEM_MB=$(free -m | awk 'NR==2{print $2}')
DISK_MB=$(df / | tail -1 | awk '{print $4}')
echo "内存: ${MEM_MB}MB | 可用磁盘: $((DISK_MB/1024))MB"

if [ "$MEM_MB" -lt 400 ]; then
    echo -e "${RED}⚠️ 内存不足 400MB，建议添加 Swap${NC}"
    # 自动创建 512MB swap
    if [ ! -f /swapfile ]; then
        echo "创建 512MB Swap..."
        fallocate -l 512M /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=512
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        echo "/swapfile none swap sw 0 0" >> /etc/fstab
        echo -e "${GREEN}✓ Swap 创建完成${NC}"
    fi
fi

# 清理空间
echo "清理系统..."
apt-get update -qq
apt-get autoremove -y -qq
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* 2>/dev/null || true

# 安装最小依赖（无 Docker）
echo "安装 Python..."
apt-get install -y -qq python3 python3-pip python3-venv git curl

# 克隆代码
if [ ! -d "lmarena-2api" ]; then
    git clone --depth 1 "$REPO_URL"
    cd lmarena-2api
else
    cd lmarena-2api
    git pull
fi

# 创建虚拟环境（比系统 pip 省空间）
python3 -m venv venv --system-site-packages --without-pip
source venv/bin/activate
curl -s https://bootstrap.pypa.io/get-pip.py | python3 - --no-cache-dir

# 安装依赖（精简）
pip install --no-cache-dir fastapi uvicorn pydantic websockets

# 创建启动脚本
cat > run.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
mkdir -p logs
exec python3 proxy_server.py 2>&1 | tee -a logs/server.log
EOF
chmod +x run.sh

# systemd 服务
sudo tee /etc/systemd/system/lmarena-proxy.service > /dev/null << EOF
[Unit]
Description=LMArena Proxy (512MB Optimized)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/run.sh
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# 内存限制保护
MemoryMax=400M
OOMScoreAdjust=100

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable lmarena-proxy
sudo systemctl restart lmarena-proxy

sleep 2

# 检查状态
if systemctl is-active --quiet lmarena-proxy; then
    SERVER_IP=$(curl -s -m 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    
    echo ""
    echo -e "${GREEN}✓ 部署成功！${NC}"
    echo ""
    echo -e "API:       ${BLUE}http://${SERVER_IP}:9080/v1${NC}"
    echo -e "监控:      ${BLUE}http://${SERVER_IP}:9080/monitor${NC}"
    echo -e "健康检查:  ${BLUE}http://${SERVER_IP}:9080/health${NC}"
    echo ""
    echo -e "${YELLOW}浏览器脚本配置:${NC}"
    echo "  SERVER_URL: ws://${SERVER_IP}:9080/ws"
    echo ""
    echo "命令:"
    echo "  日志: sudo journalctl -u lmarena-proxy -f"
    echo "  重启: sudo systemctl restart lmarena-proxy"
else
    echo -e "${RED}✗ 启动失败，查看日志:${NC}"
    sudo journalctl -u lmarena-proxy -n 20 --no-pager
    exit 1
fi
