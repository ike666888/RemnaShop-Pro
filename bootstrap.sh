#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ike666888/RemnaShop-Pro.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/remnashop-pro}"
BRANCH="${BRANCH:-main}"
PROJECT_NAME="${PROJECT_NAME:-remnashop}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() {
  echo -e "${GREEN}[remnashop-bootstrap]${NC} $*"
}

warn() {
  echo -e "${YELLOW}[warn]${NC} $*"
}

err() {
  echo -e "${RED}[error]${NC} $*" >&2
}

usage() {
  cat <<USAGE
用法:
  bash bootstrap.sh install
  bash bootstrap.sh uninstall
  bash bootstrap.sh              # 有 TTY 时显示交互菜单；否则默认执行 install

公网一键安装:
  curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bootstrap.sh | bash
公网一键卸载:
  curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bootstrap.sh | bash -s -- uninstall
USAGE
}

need_sudo() {
  if [ "${EUID}" -eq 0 ]; then
    SUDO=""
  else
    SUDO="sudo"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1
}

detect_package_manager() {
  if require_cmd apt-get; then
    PACKAGE_MANAGER="apt"
  elif require_cmd dnf; then
    PACKAGE_MANAGER="dnf"
  elif require_cmd yum; then
    PACKAGE_MANAGER="yum"
  else
    PACKAGE_MANAGER=""
  fi
}

pkg_update_if_needed() {
  if [ "${PKG_UPDATED:-0}" -eq 1 ]; then
    return
  fi

  case "${PACKAGE_MANAGER}" in
    apt)
      log "正在刷新 apt 软件包索引..."
      ${SUDO} apt-get update -y
      ;;
    dnf|yum)
      ;;
    *)
      err "未找到受支持的软件包管理器，无法自动安装依赖。"
      return 1
      ;;
  esac

  PKG_UPDATED=1
}

install_packages() {
  if [ "$#" -eq 0 ]; then
    return 0
  fi

  case "${PACKAGE_MANAGER}" in
    apt)
      pkg_update_if_needed
      ${SUDO} apt-get install -y "$@"
      ;;
    dnf)
      ${SUDO} dnf install -y "$@"
      ;;
    yum)
      ${SUDO} yum install -y "$@"
      ;;
    *)
      err "无法自动安装软件包：当前系统的软件包管理器不受支持。"
      return 1
      ;;
  esac
}

ensure_cmd_with_package() {
  local cmd="$1"
  local pkg="$2"

  if require_cmd "${cmd}"; then
    log "依赖检查：${cmd} 已安装。"
    return 0
  fi

  warn "缺少依赖：${cmd}。正在尝试自动安装（软件包：${pkg}）。"
  install_packages "${pkg}"
  if require_cmd "${cmd}"; then
    log "依赖安装完成：${cmd}。"
    return 0
  fi

  err "依赖 '${cmd}' 自动安装失败。"
  return 1
}

install_base_dependencies() {
  detect_package_manager
  if [ -z "${PACKAGE_MANAGER}" ]; then
    err "当前系统不受支持：自动安装依赖仅支持 apt、dnf 或 yum。"
    exit 1
  fi

  log "正在检查并准备 bootstrap 所需基础依赖。"
  ensure_cmd_with_package bash bash
  ensure_cmd_with_package tar tar
  ensure_cmd_with_package gzip gzip
  ensure_cmd_with_package unzip unzip
  ensure_cmd_with_package jq jq
  ensure_cmd_with_package sed sed
  ensure_cmd_with_package grep grep
  ensure_cmd_with_package awk gawk
  ensure_cmd_with_package ls coreutils
  ensure_cmd_with_package curl curl
  ensure_cmd_with_package git git

  if ! require_cmd update-ca-certificates; then
    warn "缺少依赖：ca-certificates。正在尝试自动安装。"
    install_packages ca-certificates
    if ! require_cmd update-ca-certificates; then
      err "ca-certificates 自动安装失败。"
      exit 1
    fi
  else
    log "依赖检查：ca-certificates 已安装。"
  fi
}

install_docker() {
  if require_cmd docker; then
    log "Docker 已安装：$(docker --version)"
    return
  fi

  log "未检测到 Docker，开始安装..."
  curl -fsSL https://get.docker.com | sh
  ${SUDO} systemctl enable docker >/dev/null 2>&1 || true
  ${SUDO} systemctl start docker >/dev/null 2>&1 || true

  if ! require_cmd docker; then
    err "Docker 安装失败。"
    exit 1
  fi

  log "Docker 安装完成：$(docker --version)"
}

install_docker_compose() {
  if docker compose version >/dev/null 2>&1; then
    log "Docker Compose 已可用：$(docker compose version | head -n 1)"
    return
  fi

  log "未检测到 Docker Compose 插件，开始安装..."

  if require_cmd apt-get; then
    ${SUDO} apt-get update -y
    ${SUDO} apt-get install -y docker-compose-plugin
  elif require_cmd dnf; then
    ${SUDO} dnf install -y docker-compose-plugin
  elif require_cmd yum; then
    ${SUDO} yum install -y docker-compose-plugin
  else
    err "当前软件包管理器不受支持，请手动安装 Docker Compose 插件。"
    exit 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    err "Docker Compose 安装失败。"
    exit 1
  fi

  log "Docker Compose 安装完成：$(docker compose version | head -n 1)"
}

prepare_repo() {
  log "正在准备仓库目录：${INSTALL_DIR}"
  log "说明：安装目录会保留完整 git 仓库（含 docs/tests/README 等文件）；这些文件是否进入生产镜像由 Dockerfile + .dockerignore 决定。"

  if [ -d "${INSTALL_DIR}/.git" ]; then
    log "检测到已有 git 仓库，正在更新..."
    git -C "${INSTALL_DIR}" fetch origin
    git -C "${INSTALL_DIR}" checkout "${BRANCH}"
    git -C "${INSTALL_DIR}" pull --ff-only origin "${BRANCH}"
  else
    ${SUDO} mkdir -p "$(dirname "${INSTALL_DIR}")"
    if [ -d "${INSTALL_DIR}" ]; then
      warn "${INSTALL_DIR} 已存在但不是 git 仓库。为避免冲突将先删除该目录。"
      ${SUDO} rm -rf "${INSTALL_DIR}"
    fi
    git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
  fi
}

prepare_env() {
  cd "${INSTALL_DIR}"

  if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    log "已根据 .env.example 创建 .env"
  elif [ -f .env ]; then
    log ".env 已存在，将保留现有配置并仅处理必填项。"
  else
    err "缺少 .env.example，无法初始化环境变量文件。"
    exit 1
  fi
}

get_env_value() {
  local key="$1"
  local env_file="${INSTALL_DIR}/.env"
  [ -f "${env_file}" ] || return 0
  awk -F= -v k="${key}" '$1==k {sub(/^[^=]*=/, "", $0); print; exit}' "${env_file}"
}

upsert_env_value() {
  local key="$1"
  local value="$2"
  local env_file="${INSTALL_DIR}/.env"
  local escaped
  escaped="$(printf '%s' "${value}" | sed 's/[\/&]/\\&/g')"

  if grep -qE "^${key}=" "${env_file}"; then
    sed -i "s/^${key}=.*/${key}=${escaped}/" "${env_file}"
  else
    printf '\n%s=%s\n' "${key}" "${value}" >>"${env_file}"
  fi
}

prompt_required_env() {
  if [ ! -r /dev/tty ]; then
    err "设置 ADMIN_ID 和 BOT_TOKEN 需要交互输入，但当前没有可用的 TTY。"
    err "请在终端中运行（例如：curl -fsSL <raw-script-url> | bash），或先在 ${INSTALL_DIR}/.env 中预设这两个变量。"
    exit 1
  fi

  local admin_current bot_current admin_new bot_new keep
  admin_current="$(get_env_value "ADMIN_ID")"
  bot_current="$(get_env_value "BOT_TOKEN")"

  echo
  log "正在配置必填环境变量（仅 ADMIN_ID 与 BOT_TOKEN）。"

  if [ -n "${admin_current}" ]; then
    printf "检测到现有 ADMIN_ID='%s'，是否保留？[Y/n]: " "${admin_current}" >/dev/tty
    read -r keep </dev/tty
    if [[ ! "${keep:-Y}" =~ ^[Yy]$ ]]; then
      while true; do
        printf "请输入 ADMIN_ID: " >/dev/tty
        read -r admin_new </dev/tty
        if [ -n "${admin_new}" ]; then
          upsert_env_value "ADMIN_ID" "${admin_new}"
          break
        fi
        warn "ADMIN_ID 不能为空。"
      done
    fi
  else
    while true; do
      printf "请输入 ADMIN_ID: " >/dev/tty
      read -r admin_new </dev/tty
      if [ -n "${admin_new}" ]; then
        upsert_env_value "ADMIN_ID" "${admin_new}"
        break
      fi
      warn "ADMIN_ID 不能为空。"
    done
  fi

  if [ -n "${bot_current}" ]; then
    printf "检测到现有 BOT_TOKEN='%s'，是否保留？[Y/n]: " "${bot_current}" >/dev/tty
    read -r keep </dev/tty
    if [[ ! "${keep:-Y}" =~ ^[Yy]$ ]]; then
      while true; do
        printf "请输入 BOT_TOKEN: " >/dev/tty
        read -r bot_new </dev/tty
        if [ -n "${bot_new}" ]; then
          upsert_env_value "BOT_TOKEN" "${bot_new}"
          break
        fi
        warn "BOT_TOKEN 不能为空。"
      done
    fi
  else
    while true; do
      printf "请输入 BOT_TOKEN: " >/dev/tty
      read -r bot_new </dev/tty
      if [ -n "${bot_new}" ]; then
        upsert_env_value "BOT_TOKEN" "${bot_new}"
        break
      fi
      warn "BOT_TOKEN 不能为空。"
    done
  fi

  if [ -z "$(get_env_value "ADMIN_ID")" ] || [ -z "$(get_env_value "BOT_TOKEN")" ]; then
    err "${INSTALL_DIR}/.env 中必须同时设置 ADMIN_ID 和 BOT_TOKEN。"
    exit 1
  fi
}

start_stack() {
  cd "${INSTALL_DIR}"
  log "正在启动 Docker Compose 服务栈..."
  docker compose -p "${PROJECT_NAME}" up -d --build
}

print_failure_hint() {
  cat <<MSG

❌ 安装失败：remnashop 容器未进入 healthy 状态。
排查建议：
  cd ${INSTALL_DIR}
  docker compose -p ${PROJECT_NAME} ps
  docker compose -p ${PROJECT_NAME} logs --tail=200 remnashop

MSG
}

wait_for_health() {
  cd "${INSTALL_DIR}"

  local timeout="${HEALTH_TIMEOUT_SECONDS:-300}"
  local elapsed=0
  local interval=5
  local container_id state health restart_count

  while [ "${elapsed}" -lt "${timeout}" ]; do
    container_id="$(docker compose -p "${PROJECT_NAME}" ps -q remnashop || true)"
    if [ -z "${container_id}" ]; then
      err "未找到 remnashop 容器。"
      print_failure_hint
      return 1
    fi

    state="$(docker inspect --format='{{.State.Status}}' "${container_id}" 2>/dev/null || echo "unknown")"
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${container_id}" 2>/dev/null || echo "unknown")"
    restart_count="$(docker inspect --format='{{.RestartCount}}' "${container_id}" 2>/dev/null || echo "0")"

    if [ "${state}" = "running" ] && [ "${health}" = "healthy" ]; then
      log "remnashop 容器已进入 healthy 状态。"
      return 0
    fi

    if [ "${state}" = "exited" ] || [ "${state}" = "dead" ] || [ "${health}" = "unhealthy" ] || [ "${state}" = "restarting" ] || [ "${restart_count}" -gt 3 ]; then
      err "容器状态异常，判定安装失败（state=${state}, health=${health}, restarts=${restart_count}）。"
      print_failure_hint
      return 1
    fi

    echo "[等待中] remnashop state=${state}, health=${health}, restarts=${restart_count} (${elapsed}s/${timeout}s)"
    sleep "${interval}"
    elapsed=$((elapsed + interval))
  done

  err "等待超时：${timeout}s 内 remnashop 容器仍未达到 healthy。"
  print_failure_hint
  return 1
}

verify_stack() {
  cd "${INSTALL_DIR}"

  log "校验：Docker"
  docker --version

  log "校验：Docker Compose"
  docker compose version

  log "校验：环境变量文件"
  if [ -f .env ]; then
    echo "[ok] ${INSTALL_DIR}/.env exists"
  else
    err ".env 文件不存在"
    exit 1
  fi

  log "校验：服务栈状态"
  docker compose -p "${PROJECT_NAME}" ps

  log "校验：服务健康状态"
  container_id="$(docker compose -p "${PROJECT_NAME}" ps -q remnashop || true)"
  if [ -n "${container_id}" ]; then
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${container_id}")"
    echo "[ok] remnashop container health: ${health}"
  else
    err "未找到 remnashop 容器"
    exit 1
  fi
}

uninstall_stack() {
  if ! require_cmd docker; then
    warn "未检测到 Docker，跳过容器/镜像/数据卷清理。"
    return
  fi

  if [ -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    log "正在停止并删除 RemnaShop-Pro 的 Compose 服务栈..."
    docker compose -f "${INSTALL_DIR}/docker-compose.yml" -p "${PROJECT_NAME}" down -v --rmi local --remove-orphans || true
  else
    warn "未找到 ${INSTALL_DIR}/docker-compose.yml，将仅尝试按项目标签清理。"
  fi

  local ids
  ids="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${ids}" ]; then
    log "正在清理项目 ${PROJECT_NAME} 的残留容器。"
    docker rm -f ${ids} || true
  fi

  local volume_ids
  volume_ids="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${volume_ids}" ]; then
    log "正在清理项目 ${PROJECT_NAME} 的残留数据卷。"
    docker volume rm ${volume_ids} || true
  fi

  local image_ids
  image_ids="$(docker image ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${image_ids}" ]; then
    log "正在清理项目 ${PROJECT_NAME} 的残留镜像。"
    docker image rm ${image_ids} || true
  fi
}

remove_project_directory() {
  if [ -d "${INSTALL_DIR}" ]; then
    log "正在删除项目目录：${INSTALL_DIR}"
    ${SUDO} rm -rf "${INSTALL_DIR}"
  else
    warn "项目目录不存在：${INSTALL_DIR}（可能已被删除）"
  fi
}

confirm_uninstall_if_needed() {
  if [ ! -r /dev/tty ]; then
    warn "未检测到交互式 TTY，将直接继续卸载（仅影响 RemnaShop-Pro 项目资源）。"
    return
  fi

  echo
  warn "即将删除（仅限 RemnaShop-Pro 资源）："
  warn "- Compose 项目：${PROJECT_NAME}"
  warn "- 该项目创建的容器/镜像/数据卷"
  warn "- 项目目录：${INSTALL_DIR}"
  printf "请输入确认（YES/yes/Y/y）以继续卸载，其他任意输入将取消: " >/dev/tty
  read -r confirm </dev/tty
  confirm="$(printf '%s' "${confirm}" | tr -d '[:space:]')"
  confirm="$(printf '%s' "${confirm}" | tr '[:upper:]' '[:lower:]')"
  if [ "${confirm}" != "yes" ] && [ "${confirm}" != "y" ]; then
    log "已取消卸载。"
    exit 0
  fi
}

install_flow() {
  install_base_dependencies
  install_docker
  install_docker_compose
  prepare_repo
  prepare_env
  prompt_required_env
  start_stack
  wait_for_health
  verify_stack

  cat <<MSG

✅ RemnaShop-Pro 安装完成。

安装目录: ${INSTALL_DIR}
成功判定条件已满足：
  - docker compose up 已完成
  - remnashop 容器已达到 health=healthy
后续可执行：
  1) 查看状态: cd ${INSTALL_DIR} && docker compose -p ${PROJECT_NAME} ps
  2) 查看日志: cd ${INSTALL_DIR} && docker compose -p ${PROJECT_NAME} logs -f remnashop

MSG
}

uninstall_flow() {
  confirm_uninstall_if_needed
  uninstall_stack
  remove_project_directory

  cat <<MSG

✅ RemnaShop-Pro 卸载完成。
仅处理了项目 '${PROJECT_NAME}' 相关资源。

MSG
}

show_menu() {
  echo
  echo "RemnaShop-Pro 引导脚本"
  echo "1) 安装"
  echo "2) 卸载"
  echo "0) 退出"
  read -r -p "请选择 [0-2]: " choice
  case "${choice}" in
    1) ACTION="install" ;;
    2) ACTION="uninstall" ;;
    0) exit 0 ;;
    *) err "无效选择"; exit 1 ;;
  esac
}

select_action() {
  ACTION="${1:-}"
  FROM_ARG=0

  case "${ACTION}" in
    install|uninstall)
      FROM_ARG=1
      ;;
    "")
      if [ -t 0 ]; then
        show_menu
      else
        ACTION="install"
        log "非交互模式未指定动作，默认执行 install。"
      fi
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      err "未知动作：${ACTION}"
      usage
      exit 1
      ;;
  esac
}

main() {
  need_sudo
  select_action "${1:-}"

  if [ "${ACTION}" = "install" ]; then
    install_flow
  elif [ "${ACTION}" = "uninstall" ]; then
    uninstall_flow
  fi
}

main "$@"
