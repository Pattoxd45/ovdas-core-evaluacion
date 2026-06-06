# Core Orchestrator

Núcleo central del sistema OVDAS. Implementa el patrón **Orchestration-based Saga**:
recibe trazas sísmicas, gestiona la máquina de estados del pipeline, y coordina a los
workers mediante colas RabbitMQ.

---

## Responsabilidades

- Exponer la API REST de ingesta (`POST /ingesta/traza`)
- Crear y persistir eventos en `core.pipeline_eventos`
- Avanzar el estado del pipeline al recibir callbacks de los workers
- Publicar tareas en las colas de cada worker
- Agrupar múltiples estaciones bajo un mismo `evento_id` (multi-estación)
- Servir consultas de estado (`GET /evento/{id}`, `GET /eventos`)

---

## Flujo interno

```mermaid
sequenceDiagram
    participant P as wws-poller
    participant A as Core API (FastAPI)
    participant D as core.db
    participant O as orchestrator
    participant MQ as RabbitMQ

    P->>A: POST /ingesta/traza {grupo_id, n_estaciones}
    A->>D: buscar_evento_por_grupo(grupo_id)
    alt grupo nuevo
        D-->>A: None
        A->>O: iniciar_evento(evento_id, n_estaciones)
        O->>D: crear_evento() + actualizar_estado(DETECTANDO)
        O->>MQ: publish → ovdas.deteccion
    else grupo existente
        D-->>A: evento_id existente
        A->>O: agregar_estacion(evento_id)
        O->>MQ: publish → ovdas.deteccion (segunda estación)
    end
    A-->>P: 202 {evento_id, estado}
```

```mermaid
sequenceDiagram
    participant MQ as RabbitMQ
    participant O as orchestrator (hilo daemon)
    participant D as core.db

    MQ->>O: DETECCION_COMPLETADA {evento_id}
    O->>D: actualizar_estado(PICKING)
    O->>MQ: publish → ovdas.picking

    MQ->>O: PICKING_COMPLETADO {evento_id}
    O->>D: contar_picks(evento_id)
    alt picks < n_esperadas
        O->>O: esperar más estaciones
    else picks >= n_esperadas
        O->>D: actualizar_estado(LOCALIZANDO)
        O->>MQ: publish → ovdas.localizacion
    end

    MQ->>O: LOCALIZACION_COMPLETADA {evento_id}
    O->>D: actualizar_estado(COMPLETADO)
```

---

## Máquina de estados

```mermaid
stateDiagram-v2
    [*] --> INGRESADO : crear_evento()
    INGRESADO --> DETECTANDO : iniciar_evento()
    DETECTANDO --> PICKING : DETECCION_COMPLETADA
    PICKING --> PICKING : picks parciales
    PICKING --> LOCALIZANDO : n_picks == n_estaciones
    LOCALIZANDO --> COMPLETADO : LOCALIZACION_COMPLETADA
    DETECTANDO --> ERROR : ERROR_WORKER
    PICKING --> ERROR : ERROR_WORKER
    LOCALIZANDO --> ERROR : ERROR_WORKER
```

---

## API REST

### `POST /ingesta/traza`

Ingresa una nueva traza sísmica al pipeline.

**Body (JSON):**

| Campo | Tipo | Descripción |
|---|---|---|
| `volcan_id` | string | Código volcán (ej: `"99"`, `"VV"`) |
| `estacion_id` | string | Código estación (ej: `"FU2"`) |
| `componente` | string | Componente sísmica (`"Z"`, `"N"`, `"E"`) |
| `inicio_unix` | float | Timestamp Unix de inicio de la traza |
| `duracion_seg` | float | Duración en segundos |
| `muestra_hz` | int | Frecuencia de muestreo (Hz) |
| `extra.archivo` | string | Nombre del archivo en `/shared/data/` |
| `extra.grupo_id` | string | ID compartido del ciclo (multi-estación) |
| `extra.n_estaciones` | int | Total de estaciones en el ciclo |

**Respuesta:** `202 Accepted`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "estado": "DETECTANDO",
  "mensaje": "Traza aceptada. Pipeline iniciado."
}
```

**Comportamiento multi-estación:**
- Si `grupo_id` no existe → crea evento nuevo
- Si `grupo_id` ya existe → reutiliza `evento_id`, envía tarea de detección adicional

### `GET /evento/{evento_id}`

Retorna el estado actual de un evento.

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "volcan_id": "99",
  "estacion_id": "FU2",
  "estado": "COMPLETADO",
  "traza_metadata": {
    "grupo_id": "abc123",
    "n_estaciones": 2,
    "inicio_unix": 1741305600.0
  },
  "error_msg": null,
  "created_at": "2026-03-07T03:00:00",
  "updated_at": "2026-03-07T03:01:30"
}
```

### `GET /eventos?limite=50`

Lista los últimos N eventos.

### `GET /health`

Liveness check. Verifica conexión a PostgreSQL.

---

## Base de datos

### `core.pipeline_eventos`

| Columna | Tipo | Descripción |
|---|---|---|
| `evento_id` | VARCHAR(25) PK | ID único (`VOLCAN-FECHA-UUID6`) |
| `volcan_id` | VARCHAR(3) | Referencia a `core.volcanes` |
| `estacion_id` | VARCHAR(10) | Estación que originó el evento |
| `estado` | VARCHAR(30) | Estado actual del pipeline |
| `traza_metadata` | JSONB | Metadatos de la traza + grupo_id + n_estaciones |
| `error_msg` | TEXT | Descripción del error (si aplica) |
| `created_at` | TIMESTAMP | Creación del evento |
| `updated_at` | TIMESTAMP | Última actualización (trigger automático) |

### `core.volcanes` / `core.estaciones`

Catálogos SSOT (Single Source of Truth). Solo lectura para los workers.
La validación de estación contra volcán ocurre en `POST /ingesta/traza`.

---

## Archivos

| Archivo | Función |
|---|---|
| `main.py` | FastAPI app, endpoints REST, startup |
| `orchestrator.py` | Máquina de estados, publicación de tareas, consumidor de callbacks |
| `db.py` | Acceso a PostgreSQL (CRUD) |
| `config.py` | Constantes: URLs, nombres de colas, estados, tipos de callback |
| `models.py` | Pydantic models: `TraceInput`, `EventoResponse` |

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `RABBITMQ_URL` | `amqp://ovdas:ovdas@rabbitmq:5672/` | URL de conexión RabbitMQ |
| `POSTGRES_DSN` | `host=postgres ...` | DSN de conexión PostgreSQL |

---

## Colas que gestiona

| Cola | Acción |
|---|---|
| `ovdas.deteccion` | Publica tareas de detección |
| `ovdas.picking` | Publica tareas de picking |
| `ovdas.localizacion` | Publica tareas de localización |
| `ovdas.callbacks` | **Consume** resultados de los workers |

---

## Notas de implementación

- El consumidor de callbacks corre en un **hilo daemon** separado del hilo principal de FastAPI.
- La conexión RabbitMQ usa `heartbeat=60` (el core no hace procesamiento pesado).
- La función `touch_updated_at()` es un trigger PostgreSQL que actualiza `updated_at` automáticamente en cada `UPDATE`.
- El `evento_id` se genera en `models.py` como `VOLCAN-YYYYMMDD-UUID6` basado en `inicio_unix`.
