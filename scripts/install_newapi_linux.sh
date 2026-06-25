#!/usr/bin/env bash
set -euo pipefail

VERSION="${NEWAPI_VERSION:-v1.0.0-rc.14}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POOL_DIR="${ROOT_DIR}/pool-gateway"
TARGET="${NEWAPI_TARGET:-${POOL_DIR}/new-api}"

case "$(uname -m)" in
  x86_64|amd64)
    ASSET="new-api-${VERSION}"
    CHECKSUM_ASSET="checksums-linux.txt"
    ;;
  aarch64|arm64)
    ASSET="new-api-arm64-${VERSION}"
    CHECKSUM_ASSET="checksums-linux.txt"
    ;;
  *)
    echo "Unsupported Linux architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

BASE_URL="https://github.com/QuantumNous/new-api/releases/download/${VERSION}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${POOL_DIR}"

echo "Downloading ${ASSET}..."
curl -fL --retry 3 --connect-timeout 20 "${BASE_URL}/${ASSET}" -o "${TMP_DIR}/${ASSET}"

echo "Downloading checksums..."
curl -fL --retry 3 --connect-timeout 20 "${BASE_URL}/${CHECKSUM_ASSET}" -o "${TMP_DIR}/${CHECKSUM_ASSET}"

if command -v sha256sum >/dev/null 2>&1; then
  (
    cd "${TMP_DIR}"
    grep " ${ASSET}$" "${CHECKSUM_ASSET}" | sha256sum -c -
  )
else
  echo "sha256sum not found; skipping checksum verification" >&2
fi

install -m 0755 "${TMP_DIR}/${ASSET}" "${TARGET}"
echo "Installed New API ${VERSION} to ${TARGET}"
