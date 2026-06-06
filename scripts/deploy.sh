#!/usr/bin/env bash
# ============================================================
# deploy.sh — Despliega OVDAS en K3s
# ============================================================
# Uso: bash scripts/deploy.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(dirname "$SCRIPT_DIR")/k8s"

echo "=== OVDAS: Desplegando en Kubernetes (K3s) ==="

# Aplicar manifiestos en orden
for manifest in "$K8S_DIR"/*.yaml; do
    echo ">>> Aplicando: $(basename "$manifest")"
    kubectl apply -f "$manifest"
done

echo ""
echo ">>> Esperando que la infraestructura esté lista..."
kubectl -n ovdas wait --for=condition=ready pod \
    -l app=postgres --timeout=120s || true
kubectl -n ovdas wait --for=condition=ready pod \
    -l app=rabbitmq --timeout=120s || true

echo ""
echo "=== Estado del despliegue ==="
kubectl -n ovdas get pods -o wide
echo ""
kubectl -n ovdas get services

echo ""
echo "=== Puertos expuestos ==="
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "  Core API:        http://${NODE_IP}:30800/docs"
echo "  API Gateway:     http://${NODE_IP}:30808/docs"
echo "  RabbitMQ UI:     http://${NODE_IP}:31672  (user: ovdas / ovdas)"
echo ""
echo "Para ver logs: kubectl -n ovdas logs -f deploy/core"
echo "Para escalar:  kubectl -n ovdas scale deploy/worker-deteccion --replicas=3"
