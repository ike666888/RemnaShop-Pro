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
Usage:
  bash bootstrap.sh install
  bash bootstrap.sh uninstall
  bash bootstrap.sh              # interactive menu when TTY is available; otherwise defaults to install

Public one-command install:
  curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bootstrap.sh | bash
Public one-command uninstall:
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
      log "Refreshing apt package index..."
      ${SUDO} apt-get update -y
      ;;
    dnf|yum)
      ;;
    *)
      err "No supported package manager found for automatic dependency installation."
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
      err "Cannot install packages automatically: unsupported package manager."
      return 1
      ;;
  esac
}

ensure_cmd_with_package() {
  local cmd="$1"
  local pkg="$2"

  if require_cmd "${cmd}"; then
    log "Dependency check: ${cmd} is available."
    return 0
  fi

  warn "Dependency missing: ${cmd}. Attempting automatic install (package: ${pkg})."
  install_packages "${pkg}"
  if require_cmd "${cmd}"; then
    log "Dependency installed: ${cmd}."
    return 0
  fi

  err "Failed to install dependency '${cmd}' automatically."
  return 1
}

install_base_dependencies() {
  detect_package_manager
  if [ -z "${PACKAGE_MANAGER}" ]; then
    err "Unsupported system: requires apt, dnf, or yum for automatic dependency installation."
    exit 1
  fi

  log "Checking common base dependencies for bootstrap."
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
    warn "Dependency missing: ca-certificates. Attempting automatic install."
    install_packages ca-certificates
    if ! require_cmd update-ca-certificates; then
      err "Failed to install ca-certificates automatically."
      exit 1
    fi
  else
    log "Dependency check: ca-certificates is available."
  fi
}

install_docker() {
  if require_cmd docker; then
    log "Docker already installed: $(docker --version)"
    return
  fi

  log "Docker not found, installing Docker..."
  curl -fsSL https://get.docker.com | sh
  ${SUDO} systemctl enable docker >/dev/null 2>&1 || true
  ${SUDO} systemctl start docker >/dev/null 2>&1 || true

  if ! require_cmd docker; then
    err "Docker installation failed."
    exit 1
  fi

  log "Docker installed: $(docker --version)"
}

install_docker_compose() {
  if docker compose version >/dev/null 2>&1; then
    log "Docker Compose already available: $(docker compose version | head -n 1)"
    return
  fi

  log "Docker Compose plugin not found, installing..."

  if require_cmd apt-get; then
    ${SUDO} apt-get update -y
    ${SUDO} apt-get install -y docker-compose-plugin
  elif require_cmd dnf; then
    ${SUDO} dnf install -y docker-compose-plugin
  elif require_cmd yum; then
    ${SUDO} yum install -y docker-compose-plugin
  else
    err "Unsupported package manager. Please install Docker Compose plugin manually."
    exit 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    err "Docker Compose installation failed."
    exit 1
  fi

  log "Docker Compose installed: $(docker compose version | head -n 1)"
}

prepare_repo() {
  log "Preparing repository at ${INSTALL_DIR}"

  if [ -d "${INSTALL_DIR}/.git" ]; then
    log "Existing git repository detected, updating..."
    git -C "${INSTALL_DIR}" fetch origin
    git -C "${INSTALL_DIR}" checkout "${BRANCH}"
    git -C "${INSTALL_DIR}" pull --ff-only origin "${BRANCH}"
  else
    ${SUDO} mkdir -p "$(dirname "${INSTALL_DIR}")"
    if [ -d "${INSTALL_DIR}" ]; then
      warn "${INSTALL_DIR} exists but is not a git repository. Removing it to avoid conflicts."
      ${SUDO} rm -rf "${INSTALL_DIR}"
    fi
    git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
  fi
}

prepare_env() {
  cd "${INSTALL_DIR}"

  if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    log "Created .env from .env.example"
    warn "Please edit ${INSTALL_DIR}/.env and set at least ADMIN_ID and BOT_TOKEN"
  elif [ -f .env ]; then
    log ".env already exists, keeping current values"
  else
    err "Missing .env.example, cannot prepare environment file."
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

input_fd() {
  if [ -t 0 ]; then
    echo "/dev/stdin"
  elif { exec 3</dev/tty; } 2>/dev/null; then
    echo "/dev/fd/3"
  else
    echo ""
  fi
}

prompt_required_env() {
  local fd
  fd="$(input_fd)"
  if [ -z "${fd}" ]; then
    err "Interactive input is required to set ADMIN_ID and BOT_TOKEN, but no TTY is available."
    err "Run on a terminal or preconfigure ${INSTALL_DIR}/.env before install."
    exit 1
  fi

  local admin_current bot_current admin_new bot_new keep
  admin_current="$(get_env_value "ADMIN_ID")"
  bot_current="$(get_env_value "BOT_TOKEN")"

  echo
  log "Configure required environment values."

  if [ -n "${admin_current}" ]; then
    read -r -p "Existing ADMIN_ID='${admin_current}'. Keep it? [Y/n]: " keep <"${fd}"
    if [[ ! "${keep:-Y}" =~ ^[Yy]$ ]]; then
      while true; do
        read -r -p "Enter ADMIN_ID: " admin_new <"${fd}"
        if [ -n "${admin_new}" ]; then
          upsert_env_value "ADMIN_ID" "${admin_new}"
          break
        fi
        warn "ADMIN_ID cannot be empty."
      done
    fi
  else
    while true; do
      read -r -p "Enter ADMIN_ID: " admin_new <"${fd}"
      if [ -n "${admin_new}" ]; then
        upsert_env_value "ADMIN_ID" "${admin_new}"
        break
      fi
      warn "ADMIN_ID cannot be empty."
    done
  fi

  if [ -n "${bot_current}" ]; then
    read -r -p "Existing BOT_TOKEN='${bot_current}'. Keep it? [Y/n]: " keep <"${fd}"
    if [[ ! "${keep:-Y}" =~ ^[Yy]$ ]]; then
      while true; do
        read -r -p "Enter BOT_TOKEN: " bot_new <"${fd}"
        if [ -n "${bot_new}" ]; then
          upsert_env_value "BOT_TOKEN" "${bot_new}"
          break
        fi
        warn "BOT_TOKEN cannot be empty."
      done
    fi
  else
    while true; do
      read -r -p "Enter BOT_TOKEN: " bot_new <"${fd}"
      if [ -n "${bot_new}" ]; then
        upsert_env_value "BOT_TOKEN" "${bot_new}"
        break
      fi
      warn "BOT_TOKEN cannot be empty."
    done
  fi

  if [ -z "$(get_env_value "ADMIN_ID")" ] || [ -z "$(get_env_value "BOT_TOKEN")" ]; then
    err "ADMIN_ID and BOT_TOKEN must both be set in ${INSTALL_DIR}/.env."
    [ "${fd}" = "/dev/fd/3" ] && exec 3<&-
    exit 1
  fi

  [ "${fd}" = "/dev/fd/3" ] && exec 3<&-
}

start_stack() {
  cd "${INSTALL_DIR}"
  log "Starting Docker Compose stack..."
  docker compose -p "${PROJECT_NAME}" up -d --build
}

print_failure_hint() {
  cat <<MSG

❌ Install failed: remnashop container did not become healthy.
Troubleshooting:
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
      err "remnashop container not found."
      print_failure_hint
      return 1
    fi

    state="$(docker inspect --format='{{.State.Status}}' "${container_id}" 2>/dev/null || echo "unknown")"
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${container_id}" 2>/dev/null || echo "unknown")"
    restart_count="$(docker inspect --format='{{.RestartCount}}' "${container_id}" 2>/dev/null || echo "0")"

    if [ "${state}" = "running" ] && [ "${health}" = "healthy" ]; then
      log "remnashop container is healthy."
      return 0
    fi

    if [ "${state}" = "exited" ] || [ "${state}" = "dead" ] || [ "${health}" = "unhealthy" ] || [ "${state}" = "restarting" ] || [ "${restart_count}" -gt 3 ]; then
      err "Container state indicates failure (state=${state}, health=${health}, restarts=${restart_count})."
      print_failure_hint
      return 1
    fi

    echo "[wait] remnashop state=${state}, health=${health}, restarts=${restart_count} (${elapsed}s/${timeout}s)"
    sleep "${interval}"
    elapsed=$((elapsed + interval))
  done

  err "Timed out after ${timeout}s waiting for healthy remnashop container."
  print_failure_hint
  return 1
}

verify_stack() {
  cd "${INSTALL_DIR}"

  log "Verification: Docker"
  docker --version

  log "Verification: Docker Compose"
  docker compose version

  log "Verification: environment file"
  if [ -f .env ]; then
    echo "[ok] ${INSTALL_DIR}/.env exists"
  else
    err ".env file does not exist"
    exit 1
  fi

  log "Verification: stack status"
  docker compose -p "${PROJECT_NAME}" ps

  log "Verification: service health"
  container_id="$(docker compose -p "${PROJECT_NAME}" ps -q remnashop || true)"
  if [ -n "${container_id}" ]; then
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${container_id}")"
    echo "[ok] remnashop container health: ${health}"
  else
    err "remnashop container not found"
    exit 1
  fi
}

uninstall_stack() {
  if ! require_cmd docker; then
    warn "Docker not found, skipping container/image/volume cleanup."
    return
  fi

  if [ -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    log "Stopping and removing RemnaShop-Pro compose stack..."
    docker compose -f "${INSTALL_DIR}/docker-compose.yml" -p "${PROJECT_NAME}" down -v --rmi local --remove-orphans || true
  else
    warn "Compose file not found at ${INSTALL_DIR}/docker-compose.yml, trying label-based cleanup only."
  fi

  local ids
  ids="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${ids}" ]; then
    log "Removing leftover containers labeled for project ${PROJECT_NAME}"
    docker rm -f ${ids} || true
  fi

  local volume_ids
  volume_ids="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${volume_ids}" ]; then
    log "Removing leftover volumes labeled for project ${PROJECT_NAME}"
    docker volume rm ${volume_ids} || true
  fi

  local image_ids
  image_ids="$(docker image ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}" || true)"
  if [ -n "${image_ids}" ]; then
    log "Removing leftover images labeled for project ${PROJECT_NAME}"
    docker image rm ${image_ids} || true
  fi
}

remove_project_directory() {
  if [ -d "${INSTALL_DIR}" ]; then
    log "Removing project directory: ${INSTALL_DIR}"
    ${SUDO} rm -rf "${INSTALL_DIR}"
  else
    warn "Project directory not found: ${INSTALL_DIR} (already removed)"
  fi
}

confirm_uninstall_if_needed() {
  local fd
  fd="$(input_fd)"
  if [ -z "${fd}" ]; then
    warn "No interactive TTY detected; proceeding without confirmation prompt."
    return
  fi

  echo
  warn "This will remove ONLY RemnaShop-Pro resources:"
  warn "- compose project: ${PROJECT_NAME}"
  warn "- containers/images/volumes created by this project"
  warn "- directory: ${INSTALL_DIR}"
  read -r -p "Type 'YES' to confirm uninstall: " confirm <"${fd}"
  if [ "${confirm}" != "YES" ]; then
    log "Uninstall canceled."
    [ "${fd}" = "/dev/fd/3" ] && exec 3<&-
    exit 0
  fi
  [ "${fd}" = "/dev/fd/3" ] && exec 3<&-
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

✅ RemnaShop-Pro install completed.

Install directory: ${INSTALL_DIR}
Success criteria met:
  - docker compose up completed
  - remnashop container reached health=healthy
Next steps:
  1) Check status: cd ${INSTALL_DIR} && docker compose -p ${PROJECT_NAME} ps
  2) Check logs:   cd ${INSTALL_DIR} && docker compose -p ${PROJECT_NAME} logs -f remnashop

MSG
}

uninstall_flow() {
  confirm_uninstall_if_needed
  uninstall_stack
  remove_project_directory

  cat <<MSG

✅ RemnaShop-Pro uninstall completed.
Only project '${PROJECT_NAME}' resources were targeted.

MSG
}

show_menu() {
  echo
  echo "RemnaShop-Pro Bootstrap"
  echo "1) Install"
  echo "2) Uninstall"
  echo "0) Exit"
  read -r -p "Choose [0-2]: " choice
  case "${choice}" in
    1) ACTION="install" ;;
    2) ACTION="uninstall" ;;
    0) exit 0 ;;
    *) err "Invalid selection"; exit 1 ;;
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
        log "No action provided in non-interactive mode; defaulting to install."
      fi
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      err "Unknown action: ${ACTION}"
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
