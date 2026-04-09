#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="${PACKETSAFARI_ONPREM_RAW_BASE:-https://raw.githubusercontent.com/packetsafari/packetsafari-onprem/main}"
ARCHIVE_URL="${PACKETSAFARI_ONPREM_ARCHIVE_URL:-https://github.com/packetsafari/packetsafari-onprem/archive/refs/heads/main.tar.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

fetch() {
  local rel="$1"
  local out="$2"
  curl -fsSL "${REPO_RAW_BASE}/${rel}" -o "$out"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

require_cmd python3
require_cmd tar

if [ -f "${SCRIPT_DIR}/packetsafari_onprem/cli.py" ] && [ -f "${SCRIPT_DIR}/pyproject.toml" ]; then
  BUNDLE_DIR="${SCRIPT_DIR}"
  cp "${SCRIPT_DIR}/bootstrap-manifest.json" "${TMP_DIR}/bootstrap-manifest.json"
else
  require_cmd curl
  fetch "bootstrap-manifest.json" "${TMP_DIR}/bootstrap-manifest.json"
  fetch "VERSION" "${TMP_DIR}/VERSION"

  ARCHIVE_PATH="${TMP_DIR}/packetsafari-onprem.tar.gz"
  curl -fsSL "${ARCHIVE_URL}" -o "${ARCHIVE_PATH}"
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
  install|status|upgrade|rollback|tui|onboard|config|iam|diagnostics)
    exec python3 "${ENTRYPOINT}" "$@"
    ;;
  *)
    echo "Unknown action: ${ACTION}" >&2
    echo "Supported actions: install, status, upgrade, rollback, tui, onboard, config, iam, diagnostics" >&2
    exit 1
    ;;
esac
