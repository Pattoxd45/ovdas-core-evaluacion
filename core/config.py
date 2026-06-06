import os

# ── Conexiones ────────────────────────────────────────────────
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://ovdas:ovdas@rabbitmq:5672/")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "host=postgres port=5432 dbname=ovdas user=ovdas password=ovdas"
)

# ── Colas RabbitMQ ────────────────────────────────────────────
Q_DETECCION    = "ovdas.deteccion"    # → worker-deteccion consume
Q_PICKING      = "ovdas.picking"      # → worker-picking consume
Q_LOCALIZACION = "ovdas.localizacion" # → worker-localizacion consume
Q_CALLBACKS    = "ovdas.callbacks"    # → core consume (resultados de workers)

# ── Estados del pipeline ──────────────────────────────────────
class Estado:
    INGRESADO   = "INGRESADO"
    DETECTANDO  = "DETECTANDO"
    PICKING     = "PICKING"
    LOCALIZANDO = "LOCALIZANDO"   # futuro: worker-localizacion
    CLASIFICANDO= "CLASIFICANDO"  # futuro: worker-clasificacion
    COMPLETADO  = "COMPLETADO"
    ERROR       = "ERROR"

# ── Tipos de mensaje callback ─────────────────────────────────
class TipoCallback:
    DETECCION_COMPLETADA   = "DETECCION_COMPLETADA"
    PICKING_COMPLETADO     = "PICKING_COMPLETADO"
    LOCALIZACION_COMPLETADA = "LOCALIZACION_COMPLETADA"
    ERROR_WORKER           = "ERROR_WORKER"

# ── Configuración general ─────────────────────────────────────
MAX_RETRY_MQ = 10       # reintentos conexión RabbitMQ al arrancar
RETRY_DELAY  = 3        # segundos entre reintentos
