#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="${PACKETSAFARI_ONPREM_RAW_BASE:-https://raw.githubusercontent.com/oripka/packetsafari-onprem/main}"
ARCHIVE_URL="${PACKETSAFARI_ONPREM_ARCHIVE_URL:-https://github.com/oripka/packetsafari-onprem/archive/refs/heads/main.tar.gz}"
BOOTSTRAP_MANIFEST_URL="${PACKETSAFARI_ONPREM_BOOTSTRAP_MANIFEST_URL:-}"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
if [ "${SCRIPT_SOURCE}" = "bash" ] || [ "${SCRIPT_SOURCE}" = "-" ]; then
  SCRIPT_DIR="$(pwd)"
else
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CURL_ARGS=(-fsSL)
if [ -n "${PACKETSAFARI_ONPREM_BEARER_TOKEN:-}" ]; then
  CURL_ARGS+=(-H "Authorization: Bearer ${PACKETSAFARI_ONPREM_BEARER_TOKEN}")
fi
if [ -n "${PACKETSAFARI_ONPREM_BASIC_AUTH:-}" ]; then
  CURL_ARGS+=(-u "${PACKETSAFARI_ONPREM_BASIC_AUTH}")
fi
if [ -n "${PACKETSAFARI_ONPREM_DOWNLOAD_HEADER:-}" ]; then
  while IFS= read -r header; do
    if [ -n "${header}" ]; then
      CURL_ARGS+=(-H "${header}")
    fi
  done <<< "${PACKETSAFARI_ONPREM_DOWNLOAD_HEADER//;;/$'\n'}"
fi
if [ "${PACKETSAFARI_ONPREM_INSECURE_TLS:-}" = "true" ]; then
  CURL_ARGS+=(-k)
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

fetch() {
  local rel="$1"
  local out="$2"
  curl "${CURL_ARGS[@]}" "${REPO_RAW_BASE}/${rel}" -o "$out"
}

fetch_url() {
  local url="$1"
  local out="$2"
  curl "${CURL_ARGS[@]}" "$url" -o "$out"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

install_ubuntu_host_deps() {
  if [ "${PACKETSAFARI_ONPREM_INSTALL_HOST_DEPS:-auto}" = "false" ]; then
    return
  fi
  if [ "$(id -u)" -ne 0 ]; then
    return
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    return
  fi

  local missing=()
  for cmd in curl python3 tar openssl zstd docker; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if ! docker compose version >/dev/null 2>&1; then
    missing+=("docker-compose-v2")
  fi
  if [ "${#missing[@]}" -eq 0 ]; then
    return
  fi

  echo "Installing PacketSafari host prerequisites with apt-get: ${missing[*]}" >&2
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl python3 tar openssl zstd docker.io docker-compose-v2
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
}

install_ubuntu_host_deps

require_cmd curl
require_cmd python3
require_cmd tar
require_cmd docker
if ! docker compose version >/dev/null 2>&1; then
  echo "Required command not found: docker compose" >&2
  exit 1
fi

if [ -f "${SCRIPT_DIR}/packetsafari_onprem/cli.py" ] && [ -f "${SCRIPT_DIR}/pyproject.toml" ]; then
  BUNDLE_DIR="${SCRIPT_DIR}"
  cp "${SCRIPT_DIR}/bootstrap-manifest.json" "${TMP_DIR}/bootstrap-manifest.json"
else
  if [ -n "${BOOTSTRAP_MANIFEST_URL}" ]; then
    fetch_url "${BOOTSTRAP_MANIFEST_URL}" "${TMP_DIR}/bootstrap-manifest.json"
  else
    fetch "bootstrap-manifest.json" "${TMP_DIR}/bootstrap-manifest.json"
  fi

  ARCHIVE_PATH="${TMP_DIR}/packetsafari-onprem.tar.gz"
  fetch_url "${ARCHIVE_URL}" "${ARCHIVE_PATH}"
  tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"

  BUNDLE_DIR="$(find "${TMP_DIR}" -maxdepth 1 -type d -name 'packetsafari-onprem-*' | head -n1)"
  if [ -z "${BUNDLE_DIR}" ] || [ ! -d "${BUNDLE_DIR}" ]; then
    echo "Unable to unpack PacketSafari on-prem bundle." >&2
    exit 1
  fi
fi

ENTRYPOINT="${BUNDLE_DIR}/packetsafari_onprem/cli.py"
if [ ! -f "${ENTRYPOINT}" ]; then
  echo "Python CLI entrypoint not found inside the downloaded bundle." >&2
  exit 1
fi

EXPECTED_SHA="$(python3 - <<'PY' "${TMP_DIR}/bootstrap-manifest.json"
import json, sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(((data.get("files") or {}).get("packetsafari_onprem/cli.py") or {}).get("sha256", ""))
PY
)"
ACTUAL_SHA="$(sha256_file "$ENTRYPOINT")"

if [ -n "$EXPECTED_SHA" ] && [ "$EXPECTED_SHA" != "REPLACE_INSTALL_SHA256" ] && [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
  echo "Python CLI checksum verification failed." >&2
  exit 1
fi

ACTION="${1:-install}"
export PYTHONPATH="${BUNDLE_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

case "${ACTION}" in
  install|status|upgrade|rollback|tui|tune|onboard|config|iam|diagnostics)
    exec python3 "${ENTRYPOINT}" "$@"
    ;;
  *)
    echo "Unknown action: ${ACTION}" >&2
    echo "Supported actions: install, status, upgrade, rollback, tui, tune, onboard, config, iam, diagnostics" >&2
    exit 1
    ;;
esac
