#!/bin/bash
set -euo pipefail

# 定义颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color
WORK_DIR="/opt/RemnaShop"
SERVICE_FILE="/etc/systemd/system/remnashop.service"
LEGACY_WEB_SERVICE_FILE="/etc/systemd/system/remnashop-web.service"
LEGACY_WEB_WANTS_LINK="/etc/systemd/system/multi-user.target.wants/remnashop-web.service"

# 检查是否为 root
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}请使用 root 权限运行此脚本！${NC}"
  exit
fi

show_menu() {
    clear
    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}        RemnaShop-Pro 管理脚本 V3.5          ${NC}"
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
    apt-get update -y
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
    TMP_DIR=$(mktemp -d)
    ARCHIVE_URL="https://codeload.github.com/ike666888/RemnaShop-Pro/tar.gz/refs/heads/main"
    curl -fL --retry 3 --retry-delay 2 -o "$TMP_DIR/repo.tar.gz" "$ARCHIVE_URL"
    tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR"

    SRC_DIR=$(find "$TMP_DIR" -maxdepth 1 -type d -name "RemnaShop-Pro-*" | head -n 1)
    if [ -z "${SRC_DIR:-}" ] || [ ! -d "$SRC_DIR" ]; then
        echo -e "${RED}代码解压失败，未找到项目目录。${NC}"
        rm -rf "$TMP_DIR"
        exit 1
    fi

    mkdir -p "$WORK_DIR/handlers" "$WORK_DIR/jobs" "$WORK_DIR/services" "$WORK_DIR/storage" "$WORK_DIR/utils"
    cp -f "$SRC_DIR/bot.py" "$WORK_DIR/bot.py"
    cp -f "$SRC_DIR"/*.py "$WORK_DIR/" 2>/dev/null || true
    cp -f "$SRC_DIR/handlers"/*.py "$WORK_DIR/handlers/" 2>/dev/null || true
    cp -f "$SRC_DIR/jobs"/*.py "$WORK_DIR/jobs/" 2>/dev/null || true
    cp -f "$SRC_DIR/services"/*.py "$WORK_DIR/services/" 2>/dev/null || true
    cp -f "$SRC_DIR/storage"/*.py "$WORK_DIR/storage/" 2>/dev/null || true
    cp -f "$SRC_DIR/utils"/*.py "$WORK_DIR/utils/" 2>/dev/null || true

    rm -rf "$TMP_DIR"

    find "$WORK_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + || true
    chmod +x "$WORK_DIR/bot.py"
    echo -e "${GREEN}代码文件同步完成。${NC}"

    if [ ! -f "$WORK_DIR/config.json" ]; then
        echo -e "${YELLOW}>>> 检测到首次运行，请配置参数:${NC}"
        read -p "请输入管理员 TG ID (数字): " ADMIN_ID
        read -p "请输入机器人 Token: " BOT_TOKEN
        read -p "请输入面板地址 (例如 https://panel.com): " PANEL_URL
        read -p "请输入面板 API Token: " PANEL_TOKEN
        read -p "请输入订阅域名 (例如 https://sub.com): " SUB_DOMAIN
        read -p "请输入默认用户组 UUID: " GROUP_UUID

        cat > "$WORK_DIR/config.json" <<EOF
{
    "admin_id": "$ADMIN_ID",
    "bot_token": "$BOT_TOKEN",
    "panel_url": "$PANEL_URL",
    "panel_token": "$PANEL_TOKEN",
    "sub_domain": "$SUB_DOMAIN",
    "group_uuid": "$GROUP_UUID"
}
EOF
        echo -e "${GREEN}配置文件创建成功。${NC}"
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

    # 兼容历史版本：清理遗留 Web 服务配置，避免出现“enable remnashop-web”相关报错
    systemctl disable remnashop-web 2>/dev/null || true
    systemctl stop remnashop-web 2>/dev/null || true
    rm -f "$LEGACY_WEB_SERVICE_FILE" "$LEGACY_WEB_WANTS_LINK"

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
    systemctl stop remnashop 2>/dev/null || true
    systemctl disable remnashop 2>/dev/null || true
    systemctl stop remnashop-web 2>/dev/null || true
    systemctl disable remnashop-web 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    rm -f "$LEGACY_WEB_SERVICE_FILE" "$LEGACY_WEB_WANTS_LINK"
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
