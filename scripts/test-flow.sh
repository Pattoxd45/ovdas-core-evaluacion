#!/usr/bin/env bash
# ============================================================
# test-flow.sh — Prueba el flujo completo del pipeline OVDAS
# ============================================================
# Uso:
#   bash scripts/test-flow.sh              (contra Docker Compose)
#   bash scripts/test-flow.sh k3s          (contra K3s)
# ============================================================
set -euo pipefail

MODE=${1:-"docker"}

if [[ "$MODE" == "k3s" ]]; then
    NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
    CORE_URL="http://${NODE_IP}:30800"
    GW_URL="http://${NODE_IP}:30808"
else
    CORE_URL="http://localhost:8000"
    GW_URL="http://localhost:8080"
fi

echo "=== OVDAS: Test de flujo completo ==="
echo "Core:    $CORE_URL"
echo "Gateway: $GW_URL"
echo ""

# 1. Health check
echo "--- [1/5] Health check del Core ---"
curl -sf "$CORE_URL/health" | python3 -m json.tool
echo ""

# 2. Ingestar traza (estación VLL/PFT)
echo "--- [2/5] Ingesta de traza sísmica (VLL - PFT) ---"
RESPONSE=$(curl -sf -X POST "$CORE_URL/ingesta/traza" \
    -H "Content-Type: application/json" \
    -d '{
        "volcan_id":    "VLL",
        "estacion_id":  "PFT",
        "componente":   "Z",
        "duracion_seg": 45.0,
        "muestra_hz":   100
    }')
echo "$RESPONSE" | python3 -m json.tool
EVENTO_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['evento_id'])")
echo ""
echo "  evento_id: $EVENTO_ID"
echo ""

# 3. Esperar procesamiento
echo "--- [3/5] Esperando procesamiento del pipeline (5s)... ---"
sleep 5

# 4. Consultar estado via Core
echo "--- [4/5] Estado del evento (Core) ---"
curl -sf "$CORE_URL/evento/$EVENTO_ID" | python3 -m json.tool
echo ""

# 5. Reporte agregado via API Gateway
echo "--- [5/5] Reporte completo (API Gateway) ---"
curl -sf "$GW_URL/reporte/evento/$EVENTO_ID" | python3 -m json.tool
echo ""

# Extra: inyectar múltiples eventos para ver el flujo
echo "--- [Extra] Inyectando 5 eventos adicionales para CHI/CHR ---"
for i in $(seq 1 5); do
    curl -sf -X POST "$CORE_URL/ingesta/traza" \
        -H "Content-Type: application/json" \
        -d "{
            \"volcan_id\":    \"CHI\",
            \"estacion_id\":  \"CHR\",
            \"componente\":   \"Z\",
            \"duracion_seg\": $((20 + i * 5)),
            \"muestra_hz\":   100
        }" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print(f'  Evento {$i}: {r[\"evento_id\"]} → {r[\"estado\"]}')
"
    sleep 0.2
done

echo ""
echo "--- Listado de eventos recientes ---"
sleep 3
curl -sf "$GW_URL/eventos?limite=10" | python3 -m json.tool
echo ""
echo "=== Test completado ==="
