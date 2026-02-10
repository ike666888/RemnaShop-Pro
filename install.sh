#!/bin/bash

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
    echo -e "${GREEN}       RemnaShop-Pro 管理脚本 V2.0           ${NC}"
    echo -e "${GREEN}=============================================${NC}"
    echo -e "1. 🛠  安装 / 更新 (保留数据库)"
    echo -e "2. 🗑  卸载全部 (删除数据)"
    echo -e "0. 🚪 退出"
    echo -e "${GREEN}=============================================${NC}"
    read -p "请输入选项 [0-2]: " option
}

install_bot() {
    echo -e "${YELLOW}>>> 开始安装流程...${NC}"

    # 1. 环境检查
    if ! command -v python3 &> /dev/null; then
        echo -e "${YELLOW}正在安装 Python3...${NC}"
        apt-get update && apt-get install -y python3 python3-pip
    fi

    # 2. 依赖安装
    echo -e "${YELLOW}正在安装/更新 Python 依赖...${NC}"
    pip3 install python-telegram-bot[job-queue] requests --break-system-packages

    # 3. 创建目录
    if [ ! -d "$WORK_DIR" ]; then
        mkdir -p "$WORK_DIR"
        echo -e "${GREEN}目录已创建: $WORK_DIR${NC}"
    fi

    # 4. 下载代码
    echo -e "${YELLOW}正在拉取最新代码...${NC}"
    curl -o $WORK_DIR/bot.py https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bot.py

    # 5. 自动赋权
    chmod +x "$WORK_DIR/bot.py"
    echo -e "${GREEN}已赋予脚本执行权限。${NC}"

    # 6. 配置录入
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

    # 7. 配置 Systemd
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

    # 8. 启动服务
    systemctl daemon-reload
    systemctl enable remnashop
    systemctl restart remnashop

    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}🎉 安装/更新 完成！${NC}"
    echo -e "机器人状态: $(systemctl is-active remnashop)"
    echo -e "查看日志命令: journalctl -u remnashop -f"
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

    echo -e "${YELLOW}正在删除服务文件...${NC}"
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload

    echo -e "${YELLOW}正在删除项目文件...${NC}"
    rm -rf "$WORK_DIR"

    echo -e "${GREEN}✅ 卸载完成。所有痕迹已清理。${NC}"
}

# 主逻辑
while true; do
    show_menu
    case $option in
        1)
            install_bot
            break
            ;;
        2)
            uninstall_bot
            break
            ;;
        0)
            echo "退出。"
            exit 0
            ;;
        *)
            echo -e "${RED}无效选项，请重试。${NC}"
            sleep 1
            ;;
    esac
done
