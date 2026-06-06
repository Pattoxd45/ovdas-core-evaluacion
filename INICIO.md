# OVDAS Core — Guía de inicio

Sistema de monitoreo volcánico OVDAS rediseñado con arquitectura de microservicios.

---

## Arquitectura rápida

```
[Cliente / curl]
       │  POST /ingesta/traza
       ▼
  ┌─────────┐   ovdas.deteccion   ┌──────────────────┐
  │  Core   │ ──────────────────► │ worker-deteccion │ (stub ADOVE)
  │  :8000  │ ◄────────────────── │                  │
  └────┬────┘  DETECCION_COMPLETADA└──────────────────┘
       │
       │  ovdas.picking          ┌──────────────────┐
       └───────────────────────► │  worker-picking  │ (stub P-S picker)
         ◄─────────────────────── │                  │
          PICKING_COMPLETADO      └──────────────────┘
       │
  ┌────▼─────┐
  │PostgreSQL│  schemas: core / deteccion / picking
  └──────────┘

  [API Gateway :8080] — agrega datos de los 3 schemas
  [RabbitMQ :15672]   — management UI
```

---

## Opción A — Docker Compose (recomendado para desarrollo)

### Prerrequisitos

```bash
# Fedora/RHEL
sudo dnf install -y docker docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # cerrar sesión y volver a entrar
```

### Levantar todo

```bash
cd ovdas-core
docker compose up --build
```

La primera vez descarga imágenes (~500 MB) y compila las 4 imágenes propias.
Esperar hasta ver:

```
ovdas-core  | INFO  Core Orchestrator listo.
ovdas-core  | INFO  Esperando callbacks en ovdas.callbacks ...
```

### URLs disponibles

| Servicio          | URL                                  | Usuario/Contraseña |
|-------------------|--------------------------------------|--------------------|
| Core API Swagger  | http://localhost:8000/docs           | —                  |
| API Gateway Swagger | http://localhost:8080/docs         | —                  |
| RabbitMQ UI       | http://localhost:15672               | ovdas / ovdas      |
| PostgreSQL        | localhost:5432 · BD: ovdas           | ovdas / ovdas      |

### Detener

```bash
docker compose down          # mantiene volúmenes (datos)
docker compose down -v       # elimina volúmenes (reset total)
```

### Escalar workers

```bash
# 3 workers de detección procesando en paralelo
docker compose up --scale worker-deteccion=3
```

---

## Opción B — Kubernetes con K3s (on-premise Linux)

### 1. Instalar K3s y Docker

```bash
sudo bash scripts/setup-k3s.sh
# Cierra sesión y vuelve a entrar para que apliquen los grupos
```

### 2. Construir e importar imágenes

```bash
bash scripts/build-images.sh
# Hace: docker build + k3s ctr images import para cada servicio
```

### 3. Desplegar en el clúster

```bash
bash scripts/deploy.sh
# Aplica todos los manifiestos en k8s/ en orden
# Al final imprime los NodePorts disponibles
```

### NodePorts (acceso desde la máquina local)

| Servicio        | Puerto   |
|-----------------|----------|
| Core API        | 30800    |
| API Gateway     | 30808    |
| RabbitMQ UI     | 31672    |

Ejemplo con IP del nodo `192.168.1.100`:
- Core: `http://192.168.1.100:30800/docs`
- RabbitMQ: `http://192.168.1.100:31672` (ovdas/ovdas)

### Verificar pods

```bash
kubectl get pods -n ovdas
kubectl get hpa -n ovdas
```

---

## Probar el flujo completo

El script `scripts/test-flow.sh` envía eventos de prueba y consulta resultados.

```bash
# Contra Docker Compose (localhost)
bash scripts/test-flow.sh

# Contra K3s (reemplaza IP por la del nodo)
bash scripts/test-flow.sh k3s
```

### Prueba manual paso a paso

#### 1. Ingresar una traza sísmica

```bash
curl -s -X POST http://localhost:8000/ingesta/traza \
  -H "Content-Type: application/json" \
  -d '{
    "volcan_id":   "VLL",
    "estacion_id": "PFT",
    "componente":  "Z",
    "inicio_unix": 1704067200.0,
    "duracion_seg": 120.0,
    "muestra_hz":  100
  }' | python3 -m json.tool
```

Respuesta esperada (HTTP 202):

```json
{
  "evento_id": "VLL-PFT-20240101T000000-abc12345",
  "estado":    "DETECTANDO",
  "mensaje":   "Traza aceptada. Pipeline iniciado."
}
```

#### 2. Consultar el estado del evento

```bash
# Reemplazar <evento_id> con el valor recibido
curl -s http://localhost:8000/evento/<evento_id> | python3 -m json.tool
```

El estado avanza: `DETECTANDO → PICKING → COMPLETADO`
(el worker-deteccion stub tarda ~15-20 s, worker-picking ~5-10 s).

#### 3. Ver reporte consolidado en API Gateway

```bash
curl -s http://localhost:8080/reporte/evento/<evento_id> | python3 -m json.tool
```

#### 4. Listar últimos eventos

```bash
curl -s http://localhost:8080/eventos | python3 -m json.tool
```

#### 5. Verificar estado del sistema

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8080/health
```

---

## Ver logs en tiempo real

```bash
# Todos los servicios
docker compose logs -f

# Solo un servicio
docker compose logs -f worker-deteccion

# En K3s
kubectl logs -n ovdas -l app=worker-deteccion -f
```

---

## Ver colas en RabbitMQ

Abrir http://localhost:15672 → Queues:
- `ovdas.deteccion` — tareas pendientes de detección
- `ovdas.picking`   — tareas pendientes de picking
- `ovdas.callbacks` — resultados devueltos por workers

---

## Consultar PostgreSQL directamente

```bash
# Desde Docker
docker exec -it ovdas-postgres psql -U ovdas -d ovdas

# Dentro de psql:
SELECT evento_id, estado, created_at FROM core.pipeline_eventos ORDER BY created_at DESC LIMIT 10;
SELECT * FROM deteccion.resultados ORDER BY created_at DESC LIMIT 5;
SELECT * FROM picking.resultados ORDER BY created_at DESC LIMIT 5;
```

---

## Estaciones disponibles para pruebas

Las siguientes combinaciones volcán/estación están precargadas en la BD:

| volcan_id | estacion_id | Nombre               |
|-----------|-------------|----------------------|
| VLL       | PFT         | Volcán Villarrica – PFT |
| VLL       | PVT         | Volcán Villarrica – PVT |
| LLA       | CHR         | Volcán Llaima – CHR  |
| CHI       | LLM         | Volcán Chillán – LLM |

Cualquier otra combinación devuelve HTTP 422 (estación no registrada).

---

## Estructura del proyecto

```
ovdas-core/
├── db/
│   └── init.sql              ← esquema PostgreSQL (schemas: core, deteccion, picking)
├── core/
│   ├── main.py               ← FastAPI (API de ingesta)
│   ├── orchestrator.py       ← máquina de estados Saga + RabbitMQ
│   ├── db.py                 ← helpers psycopg2
│   ├── models.py             ← modelos Pydantic
│   ├── config.py             ← constantes y enumeraciones
│   ├── requirements.txt
│   └── Dockerfile
├── worker-deteccion/
│   ├── worker.py             ← stub ADOVE (reemplazar con adove-local)
│   ├── requirements.txt
│   └── Dockerfile
├── worker-picking/
│   ├── worker.py             ← stub P-S picker
│   ├── requirements.txt
│   └── Dockerfile
├── api-gateway/
│   ├── main.py               ← agregación API Composition
│   ├── requirements.txt
│   └── Dockerfile
├── k8s/                      ← manifiestos Kubernetes (K3s)
│   ├── 00-namespace.yaml
│   ├── 01-configmap-secrets.yaml
│   ├── 02-postgres.yaml
│   ├── 03-rabbitmq.yaml
│   ├── 04-redis.yaml
│   ├── 05-core.yaml
│   ├── 06-worker-deteccion.yaml
│   ├── 07-worker-picking.yaml
│   ├── 08-api-gateway.yaml
│   └── 09-hpa.yaml           ← autoescalado 1-5 pods por CPU
├── scripts/
│   ├── setup-k3s.sh          ← instala Docker + K3s
│   ├── build-images.sh       ← docker build + import a K3s
│   ├── deploy.sh             ← kubectl apply en orden
│   └── test-flow.sh          ← prueba de flujo completa
└── docker-compose.yml
```

---

## Próximos pasos: integrar ADOVE real

El `worker-deteccion/worker.py` actual es un stub (simulación).
Para integrarlo con el código real de `adove-local/`:

1. Copiar `adove_local.py`, `cesvec.py`, `CLASIFICADOR_vgg16.py` y `pipeline_v5/`
   dentro de `worker-deteccion/`.
2. En `worker.py`, reemplazar el bloque `--- PROCESAMIENTO STUB ---` con:

```python
# Obtener la traza desde almacenamiento (S3, NFS, etc.)
# ruta_mseed = descargar_traza(tarea["evento_id"])
resultado_adove = adove_local.procesar_mseed(ruta_mseed)
```

3. Mapear los campos del JSON de `adove_local` a los campos de `deteccion.resultados`.
4. Actualizar `worker-deteccion/requirements.txt` con las dependencias de adove-local.
