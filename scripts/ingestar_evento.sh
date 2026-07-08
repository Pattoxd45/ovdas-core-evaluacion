#!/bin/bash
# Uso: ./ingestar_evento.sh <archivo.mseed> <estacion_id> <volcan_id> [componente]
# Ejemplo: ./ingestar_evento.sh prueba.mseed CHS 99 E

set -e

ARCHIVO=$1
ESTACION=$2
VOLCAN=$3
COMPONENTE=${4:-Z}

if [ -z "$ARCHIVO" ] || [ -z "$ESTACION" ] || [ -z "$VOLCAN" ]; then
  echo "Uso: $0 <archivo.mseed> <estacion_id> <volcan_id> [componente]"
  exit 1
fi

echo "→ Copiando $ARCHIVO al volumen compartido..."
docker cp "$ARCHIVO" ovdas-worker-deteccion:/shared/data/"$(basename "$ARCHIVO")"

echo "→ Leyendo metadatos con ObsPy..."
METADATA=$(docker exec ovdas-worker-deteccion python3 -c "
from obspy import read
st = read('/shared/data/$(basename "$ARCHIVO")')
tr = st.select(channel='*$COMPONENTE')[0] if st.select(channel='*$COMPONENTE') else st[0]
print(tr.stats.starttime.timestamp)
print(tr.stats.npts / tr.stats.sampling_rate)
")

INICIO_UNIX=$(echo "$METADATA" | sed -n '1p')
DURACION=$(echo "$METADATA" | sed -n '2p')

echo "  inicio_unix: $INICIO_UNIX"
echo "  duracion_seg: $DURACION"

echo "→ Disparando pipeline..."
RESPUESTA=$(curl -s -X POST http://localhost:8000/ingesta/traza \
  -H "Content-Type: application/json" \
  -d "{
    \"volcan_id\": \"$VOLCAN\",
    \"estacion_id\": \"$ESTACION\",
    \"componente\": \"$COMPONENTE\",
    \"inicio_unix\": $INICIO_UNIX,
    \"duracion_seg\": $DURACION,
    \"muestra_hz\": 100,
    \"extra\": { \"archivo\": \"$(basename "$ARCHIVO")\" }
  }")

echo "$RESPUESTA" | python3 -m json.tool

EVENTO_ID=$(echo "$RESPUESTA" | python3 -c "import sys, json; print(json.load(sys.stdin)['evento_id'])")
echo ""
echo "✅ Evento creado: $EVENTO_ID"
echo "   Consultar estado: curl -s http://localhost:8000/evento/$EVENTO_ID | python3 -m json.tool"
