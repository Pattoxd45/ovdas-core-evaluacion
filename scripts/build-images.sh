#!/usr/bin/env bash
# ============================================================
# build-images.sh — Construye imágenes Docker e importa en K3s
# ============================================================
# Uso: bash scripts/build-images.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== OVDAS: Construyendo imágenes Docker ==="
cd "$ROOT_DIR"

build_and_import() {
    local name=$1
    local context=$2
    local tag="ovdas/${name}:latest"

    echo ""
    echo ">>> Construyendo: $tag"
    docker build -t "$tag" "$context"

    echo ">>> Importando en K3s: $tag"
    docker save "$tag" | sudo k3s ctr images import -
    echo "    OK: $tag"
}

build_and_import "core"             "./core"
build_and_import "worker-deteccion" "./worker-deteccion"
build_and_import "worker-picking"   "./worker-picking"
build_and_import "api-gateway"      "./api-gateway"

echo ""
echo "=== Imágenes disponibles en K3s ==="
sudo k3s ctr images list | grep ovdas
echo ""
echo "Siguiente paso: bash scripts/deploy.sh"
