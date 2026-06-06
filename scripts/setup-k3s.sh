#!/usr/bin/env bash
# ============================================================
# setup-k3s.sh — Instala K3s y dependencias en Linux (Fedora/RHEL)
# ============================================================
# Uso: sudo bash scripts/setup-k3s.sh
# ============================================================
set -euo pipefail

echo "=== OVDAS: Instalando K3s (Kubernetes ligero) ==="

# 1. Instalar Docker si no está presente
if ! command -v docker &>/dev/null; then
    echo ">>> Instalando Docker..."
    sudo dnf install -y docker
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "NOTA: Cierra sesión y vuelve a entrar para que el grupo docker sea efectivo."
fi

# 2. Instalar K3s (sin Traefik por ahora, usaremos NodePort)
if ! command -v k3s &>/dev/null; then
    echo ">>> Instalando K3s..."
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -
    echo ">>> K3s instalado."
fi

# 3. Configurar kubectl para el usuario actual
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$USER:$USER" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"

# 4. Esperar a que K3s esté listo
echo ">>> Esperando que K3s esté listo..."
until kubectl get nodes &>/dev/null; do sleep 2; done
kubectl wait --for=condition=ready node --all --timeout=120s

echo ""
echo "=== K3s listo ==="
kubectl get nodes
echo ""
echo "Siguiente paso:"
echo "  bash scripts/build-images.sh"
echo "  bash scripts/deploy.sh"
