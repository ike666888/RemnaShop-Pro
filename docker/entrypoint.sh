#!/usr/bin/env sh
set -eu

umask 027

CONFIG_PATH="${REMNASHOP_CONFIG:-/app/config.json}"
DB_PATH="${REMNASHOP_DB:-/app/starlight.db}"

mkdir -p "$(dirname "$CONFIG_PATH")" "$(dirname "$DB_PATH")"

if [ ! -f "$CONFIG_PATH" ]; then
  : "${ADMIN_ID:=}"
  : "${BOT_TOKEN:=}"

  if [ -z "$ADMIN_ID" ] || [ -z "$BOT_TOKEN" ]; then
    echo "[entrypoint] $CONFIG_PATH 不存在，且未提供 ADMIN_ID/BOT_TOKEN，无法自动生成配置。"
    echo "[entrypoint] 请挂载 config.json，或设置环境变量后重试。"
    exit 1
  fi

  cat > "$CONFIG_PATH" <<JSON
{
  "admin_id": "$ADMIN_ID",
  "bot_token": "$BOT_TOKEN",
  "panel_url": "${PANEL_URL:-}",
  "panel_token": "${PANEL_TOKEN:-}",
  "sub_domain": "${SUB_DOMAIN:-}",
  "group_uuid": "${GROUP_UUID:-}",
  "panel_verify_tls": ${PANEL_VERIFY_TLS:-true}
}
JSON
  echo "[entrypoint] 已生成配置文件: $CONFIG_PATH"
fi

python - "$CONFIG_PATH" <<'PY'
import json
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
cfg = json.loads(config_path.read_text(encoding="utf-8"))
required = ["admin_id", "bot_token"]
missing = [key for key in required if not str(cfg.get(key, "")).strip()]
if missing:
    raise SystemExit(f"[entrypoint] 配置缺少字段: {', '.join(missing)}")
print(f"[entrypoint] 配置校验通过: {config_path}")
PY

exec python bot.py
