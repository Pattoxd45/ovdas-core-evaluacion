# Worker Detección (ADOVE)

Primer worker del pipeline sísmico. Implementa el pipeline **ADOVE** completo:
detección de eventos sísmicos y clasificación de tipo (`VT`, `LP`, `TR`, `OT`)
usando el modelo **VGG16** entrenado sobre espectrogramas volcánicos.

Portado desde el sistema legacy `adove_local.py` + `pipeline_v5/`.

---

## Entradas

| Fuente | Descripción |
|---|---|
| **RabbitMQ** `ovdas.deteccion` | Tarea con `evento_id` y nombre de archivo |
| **Volumen** `/shared/data/` | Archivo MiniSEED descargado por `wws-poller` |

### Mensaje de entrada (JSON)

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "volcan_id": "99",
  "estacion_id": "FU2",
  "componente": "Z",
  "inicio_unix": 1741305600.0,
  "duracion_seg": 3600.0,
  "muestra_hz": 100,
  "archivo": "FU2_Z_20260307_030000.mseed"
}
```

---

## Pipeline de procesamiento

```mermaid
flowchart TD
    IN[Mensaje RabbitMQ] --> READ[Leer archivo .mseed\nObsPy]
    READ --> GAP[gapreduce()\ncesvec]
    GAP --> FD[Filtro D\nsos_filter SOS]
    FD --> FD2[Filtro D2\nsos_filter SOS]
    FD2 --> DET[detection()\ncesvec UMBRAL=504]
    DET --> N{N eventos\ndetectados}
    N -->|"N = 0"| JSON0[Guardar JSON\n0 eventos]
    N -->|"N > 0"| CLASS[classification()\nVGG16 + reglas espectrales]
    CLASS --> CALC[Calcular por evento:\nSNR, frec_dom, inicio, fin, largo]
    CALC --> DB[INSERT deteccion.resultados\nN filas]
    DB --> JSON[Guardar JSON auditoría\n/app/out/]
    JSON --> CB[Publicar DETECCION_COMPLETADA\novdas.callbacks]
    JSON0 --> CB
    CB --> ACK[basic_ack]
```

---

## Algoritmos

### Filtros SOS

Dos filtros de segundo orden (`D.txt`, `D2.txt`) precargados al inicio:
- **Filtro D**: preparado para clasificación espectral
- **Filtro D2**: preparado para detección y clasificación combinada

Ambos se aplican con padding invertido de 500 muestras para evitar artefactos de borde:
```
[flip(data[:500]) | data] → sos_filter → [500:]
```

### Detección (`cesvec.detection`)

Algoritmo STA/LTA sobre la señal filtrada D2. Umbral fijo: **504**.
Retorna índices de inicio/fin de cada evento detectado.

### Clasificación (`cesvec.classification`)

Doble clasificador en paralelo:
1. **VGG16** — red neuronal convolucional entrenada sobre espectrogramas
2. **Reglas espectrales** — clasificador basado en frecuencias (portado del legacy)

El tipo final (`label_event`) proviene del clasificador de reglas.

### SNR

```
SNR (dB) = 20 × log10(peak_signal / rms_noise)
noise_window = 500 muestras pre-evento (5s a 100Hz)
```

### Frecuencia dominante

FFT sobre el segmento filtrado del evento. Retorna la frecuencia con
máxima potencia espectral dentro de la banda de análisis.

---

## Salidas

### 1. Base de datos — `deteccion.resultados`

Una fila por cada evento detectado dentro del archivo:

| Columna | Descripción |
|---|---|
| `evento_id` | ID del pipeline |
| `estacion` | Código de estación |
| `componente` | Componente (`Z`) |
| `snr` | SNR en dB |
| `label_event` | Tipo: `VT`, `LP`, `TR`, `OT` |
| `prob_vt/lp/tr/ot` | Probabilidades del clasificador VGG16 |
| `inicio` / `fin` | Timestamps Unix del evento |
| `largo` | Duración en segundos |
| `prom_ruido_fondo` | RMS del ruido de fondo |

### 2. JSON de auditoría — `/app/out/{estacion}_{fecha}.json`

Resultado completo incluyendo todos los eventos detectados con sus
probabilidades, clasificaciones y metadatos. Útil para debug y auditoría.

### 3. Callback RabbitMQ — `ovdas.callbacks`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "tipo": "DETECCION_COMPLETADA",
  "payload": {
    "estacion": "FU2",
    "componente": "Z",
    "inicio": 1741305700.5,
    "snr": 12.4,
    "label_event": "VT",
    "n_eventos": 3
  }
}
```

El payload incluye datos del **primer evento** detectado (usado por el core
para construir la tarea de picking).

---

## API REST (modo POC)

El worker también expone una API HTTP para pruebas directas.

### `POST /procesar`

Procesa un archivo .mseed directamente sin pasar por RabbitMQ.

**Body:**
```json
{
  "archivo": "FU2_Z_20260307_030000.mseed",
  "evento_id": "test-001"
}
```

**Respuesta:**
```json
{
  "evento_id": "test-001",
  "n_eventos": 3,
  "estacion": "FU2",
  "db_guardado": true,
  "mq_publicado": true,
  "resultado": { ... }
}
```

### `GET /archivos`

Lista archivos .mseed disponibles en `DATA_DIR`.

### `GET /health`

```json
{
  "modelo_vgg16_cargado": true,
  "ood_detector_cargado": true,
  "data_dir": "/shared/data",
  "data_dir_existe": true,
  "db": "up"
}
```

---

## Startup

Al iniciar el contenedor se ejecutan en orden:

1. Carga de filtros SOS (`D.txt`, `D2.txt`) desde `filters/`
2. Carga del modelo VGG16 (`pipeline_v5/rep2_weights.pt`)
3. Carga del detector OOD (`pipeline_v5/OOD_detector.pkl`)
4. Inicio del consumer RabbitMQ en hilo daemon

> El paso 2 puede tardar 30–90 segundos. El healthcheck tiene `start_period: 90s`.

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `RABBITMQ_URL` | `amqp://ovdas:ovdas@rabbitmq:5672/` | — |
| `POSTGRES_DSN` | `host=postgres ...` | — |
| `DATA_DIR` | `/shared/data` | Volumen compartido con wws-poller |
| `OUT_DIR` | `/app/out` | Directorio para JSON de auditoría |
| `UMBRAL` | `504` | Umbral de detección STA/LTA |
| `COMPONENTE` | `Z` | Componente sísmica a analizar |
| `VOLCAN` | `99` | Código de volcán (para metadata) |

---

## Dependencias internas

| Módulo | Origen | Función |
|---|---|---|
| `cesvec` | Legacy compilado | `gapreduce`, `sos_filter`, `detection`, `classification` |
| `pipeline_v5/` | Legacy | Carga de modelos VGG16, utilidades PyTorch |
| `filters/D.txt`, `D2.txt` | Legacy | Coeficientes de filtros SOS |
| `b.txt` | Legacy | Datos auxiliares de `cesvec.detection` (symlink) |

---

## Notas de implementación

- `heartbeat=0` en pika: con 600+ eventos por archivo, el procesamiento
  (VGG16 + FFT) puede tomar >60s, bloqueando el hilo IO de pika.
  Deshabilitar heartbeat evita que RabbitMQ cierre la conexión.
- El consumer corre en un **hilo daemon** del mismo proceso FastAPI.
- El CWD se fija a `Path(__file__).parent` al inicio para que `cesvec`
  encuentre `b.txt` mediante ruta relativa.
- Archivos MiniSEED en formato SDS (sin extensión `.mseed`) son leídos
  correctamente por ObsPy via detección automática por magic bytes.
