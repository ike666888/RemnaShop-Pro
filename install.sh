#!/bin/bash
set -euo pipefail

# 定义颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color
WORK_DIR="/opt/RemnaShop"
SERVICE_FILE="/etc/systemd/system/remnashop.service"

# 检查是否为 root
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}请使用 root 权限运行此脚本！${NC}"
  exit
fi

show_menu() {
    clear
    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}        RemnaShop-Pro 管理脚本 V3.4          ${NC}"
    echo -e "${GREEN}=============================================${NC}"
    echo -e "1. 🛠  安装 / 更新 (保留数据库)"
    echo -e "2. 🗑  卸载全部 (删除数据)"
    echo -e "0. 🚪 退出"
    echo -e "${GREEN}=============================================${NC}"
    read -p "请输入选项 [0-2]: " option
}

install_bot() {
    echo -e "${YELLOW}>>> 开始安装流程...${NC}"

    echo -e "${YELLOW}正在检查环境依赖...${NC}"
    if [ ! -f "/var/lib/apt/lists/lock" ]; then
        apt-get update -y
    fi
    if ! command -v python3 &> /dev/null; then
        echo -e "${YELLOW}未检测到 Python3，正在安装...${NC}"
        apt-get install -y python3
    fi
    if ! command -v pip3 &> /dev/null; then
        echo -e "${YELLOW}未检测到 pip3，正在安装...${NC}"
        apt-get install -y python3-pip
    fi

    echo -e "${YELLOW}正在安装/更新 Python 依赖...${NC}"
    pip3 install python-telegram-bot[job-queue] httpx qrcode[pil] --break-system-packages

    if [ ! -d "$WORK_DIR" ]; then
        mkdir -p "$WORK_DIR"
        echo -e "${GREEN}目录已创建: $WORK_DIR${NC}"
    fi

    echo -e "${YELLOW}正在拉取最新代码...${NC}"
    mkdir -p "$WORK_DIR/services" "$WORK_DIR/storage" "$WORK_DIR/utils" "$WORK_DIR/handlers" "$WORK_DIR/jobs"
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/bot.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bot.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/services/orders.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/services/orders.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/services/panel_api.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/services/panel_api.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/storage/db.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/storage/db.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/utils/formatting.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/utils/formatting.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/handlers/bulk_actions.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/handlers/bulk_actions.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/handlers/admin.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/handlers/admin.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/handlers/client.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/handlers/client.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/jobs/anomaly.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/jobs/anomaly.py
    curl -fL --retry 3 --connect-timeout 10 -o "$WORK_DIR/jobs/expiry.py" https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/jobs/expiry.py

    chmod +x "$WORK_DIR/bot.py"
    echo -e "${GREEN}代码文件同步完成。${NC}"

    if [ ! -f "$WORK_DIR/config.json" ]; then
        echo -e "${YELLOW}>>> 检测到首次运行，请配置参数:${NC}"
        read -p "请输入管理员 TG ID (数字): " ADMIN_ID
        read -p "请输入机器人 Token: " BOT_TOKEN

        cat > "$WORK_DIR/config.json" <<EOF
{
    "admin_id": "$ADMIN_ID",
    "bot_token": "$BOT_TOKEN",
    "panel_url": "",
    "panel_token": "",
    "sub_domain": "",
    "group_uuid": "",
    "panel_verify_tls": true
}
EOF
        echo -e "${GREEN}配置文件创建成功。${NC}"
        echo -e "${YELLOW}提示：面板地址/Token/订阅域名/默认组UUID 请在机器人管理菜单【🔌 面板配置】中填写。${NC}"
    else
        echo -e "${YELLOW}检测到配置文件已存在，跳过配置步骤。${NC}"
    fi

    echo -e "${YELLOW}配置后台服务...${NC}"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=RemnaShop-Pro Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$WORK_DIR
ExecStart=/usr/bin/python3 $WORK_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable remnashop
    systemctl restart remnashop

    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}🎉 安装/更新 完成！${NC}"
    echo -e "作者：ike"
    echo -e "交流群组：https://t.me/Remnawarecn"
    echo -e "${GREEN}=============================================${NC}"
}

uninstall_bot() {
    echo -e "${RED}⚠️  警告：此操作将删除所有文件，包括数据库(starlight.db)！${NC}"
    read -p "确定要继续吗？(y/n): " confirm
    if [ "$confirm" != "y" ]; then
        echo "操作已取消。"
        return
    fi

    echo -e "${YELLOW}正在停止服务...${NC}"
    systemctl stop remnashop
    systemctl disable remnashop
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    rm -rf "$WORK_DIR"
    echo -e "${GREEN}✅ 卸载完成。所有痕迹已清理。${NC}"
}

while true; do
    show_menu
    case $option in
        1) install_bot; break ;;
        2) uninstall_bot; break ;;
        0) echo "退出。"; exit 0 ;;
        *) echo -e "${RED}无效选项，请重试。${NC}"; sleep 1 ;;
    esac
done
