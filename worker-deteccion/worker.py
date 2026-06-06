"""
Worker Detección — OVDAS Microservicios
Pipeline ADOVE real integrado.

Modos de operación:
  1. API REST (POC):     POST /procesar  →  archivo .mseed → pipeline → DB + callback
  2. RabbitMQ (prod):   consume ovdas.deteccion → pipeline → DB + callback

En producción, cuando se disponga del servidor WWS, reemplazar leer_mseed()
por WavePyWWS.getWavefromWWS() sin cambiar el resto del pipeline.

Endpoints:
  POST /procesar          →  corre pipeline sobre un archivo .mseed local
  GET  /archivos          →  lista archivos .mseed disponibles en DATA_DIR
  GET  /health            →  liveness / readiness check
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pika
import pika.exceptions
import psycopg2
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Fijar CWD para que cesvec encuentre b.txt y pipeline_v5/ ──
os.chdir(Path(__file__).parent)

# b.txt: cesvec.detection() lo lee desde CWD con np.genfromtxt('b.txt')
_b = Path("b.txt")
if not _b.exists():
    _b.symlink_to("filters/b.txt")

import cesvec  # noqa: E402
import pipeline_v5.utils as utils  # noqa: E402
import torch  # noqa: E402

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker-deteccion: %(message)s",
)
log = logging.getLogger("worker.deteccion")

# ── Config (env vars con defaults para desarrollo local) ───────
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://ovdas:ovdas@localhost:5672/")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN", "host=localhost port=5432 dbname=ovdas user=ovdas password=ovdas"
)
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
OUT_DIR  = Path(os.getenv("OUT_DIR",  "/app/out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

Q_IN       = "ovdas.deteccion"
Q_CALLBACK = "ovdas.callbacks"

UMBRAL     = int(os.getenv("UMBRAL",     "504"))
COMPONENTE = os.getenv("COMPONENTE",     "Z")
VOLCAN     = os.getenv("VOLCAN",         "99")
EVENTCLASS = ["VT", "LP", "TR", "OT", "OT", "OT"]

# ── Filtros SOS (cargados una vez al importar) ─────────────────
D  = np.genfromtxt("filters/D.txt")
D2 = np.genfromtxt("filters/D2.txt")
log.info("Filtros D(%d secciones) y D2(%d secciones) cargados.", D.shape[0], D2.shape[0])

# ── Modelos ML (se cargan una vez en startup) ──────────────────
_model        = None
_ood_detector = None


def cargar_modelos() -> None:
    global _model, _ood_detector
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Cargando modelo VGG16 en %s ...", device)
    _model        = utils.load_model("pipeline_v5/rep2_weights.pt", device)
    _ood_detector = utils.load_OOD_detector("pipeline_v5/OOD_detector.pkl")
    log.info("Modelos cargados OK.")


# ══════════════════════════════════════════════════════════════
# LECTURA DE DATOS
# En producción: reemplazar por WavePyWWS.getWavefromWWS()
# ══════════════════════════════════════════════════════════════

def leer_mseed(ruta: str, estacion_id: str = None):
    """
    Lee un archivo MiniSEED y devuelve (xtime, data, stats).
    Si estacion_id se especifica, filtra el trace de esa estación.
    Reemplaza WavePyWWS.getWavefromWWS() para el modo POC/archivo.
    """
    from obspy import read as obspy_read

    st   = obspy_read(str(ruta))
    st_z = st.select(channel=f"*{COMPONENTE}")
    if not st_z:
        log.warning("Componente %s no encontrada, usando stream completo.", COMPONENTE)
        st_z = st

    if estacion_id:
        st_est = st_z.select(station=estacion_id)
        if st_est:
            st_z = st_est
        else:
            log.warning(
                "Estación %s no encontrada en el archivo, usando primer trace disponible.",
                estacion_id,
            )

    st_z.merge(method=1, fill_value=0)
    tr = st_z[0]

    xtime = tr.times("timestamp")
    data  = np.array(tr.data, dtype=float)
    return xtime, data, tr.stats


# ══════════════════════════════════════════════════════════════
# PIPELINE ADOVE REAL
# ══════════════════════════════════════════════════════════════

def _calcular_snr(reduced_data, idx_ini: int, idx_fin: int) -> tuple:
    """
    SNR en dB usando ventana de ruido pre-evento (500 muestras = 5s).
    Retorna (snr_db, noise_rms).
    """
    noise_window = np.array(reduced_data[max(0, idx_ini - 500):idx_ini], dtype=float)
    noise_rms = float(np.sqrt(np.mean(noise_window ** 2))) if len(noise_window) > 0 else 1.0
    if noise_rms <= 0:
        noise_rms = 1e-6
    signal_peak = float(np.max(np.abs(np.array(reduced_data[idx_ini:idx_fin], dtype=float))))
    snr_db = round(20.0 * np.log10(max(signal_peak, 1e-6) / noise_rms), 2)
    return snr_db, round(noise_rms, 6)


def run_adove_pipeline(ruta: str, evento_id: str, estacion_id: str = None) -> dict:
    """
    Ejecuta el pipeline ADOVE completo sobre un archivo MiniSEED:
      gapreduce → filtro D → filtro D2 → detección → clasificación VGG16

    Retorna el dict de resultado (equivalente al JSON de adove_local.py).
    """
    nombre = Path(ruta).name
    log.info("Pipeline iniciado: %s (evento_id=%s)", nombre, evento_id)

    # ── 1. Leer datos ──────────────────────────────────────────
    xtime, data, stats = leer_mseed(ruta, estacion_id=estacion_id)
    estacion  = stats.station
    canal     = stats.channel
    fs        = stats.sampling_rate
    t_inicio  = datetime.fromtimestamp(float(xtime[0])).strftime("%Y-%m-%d %H:%M:%S")
    t_fin     = datetime.fromtimestamp(float(xtime[-1])).strftime("%Y-%m-%d %H:%M:%S")
    station   = estacion + COMPONENTE
    log.info("  %s | %s | fs=%.1f Hz | %s → %s | %d muestras",
             estacion, canal, fs, t_inicio, t_fin, len(data))

    # ── 2. Gap reduce ──────────────────────────────────────────
    reduced_data = cesvec.gapreduce(data)

    # ── 3. Filtro D (para clasificación espectral) ─────────────
    tmp           = np.concatenate((np.flipud(reduced_data[0:500]), reduced_data))
    filtered_data = cesvec.sos_filter(D, tmp)[500:]
    filtered_data = np.asarray(filtered_data)

    # ── 4. Filtro D2 (para detección y clasificación) ──────────
    tmp2           = np.concatenate((np.flipud(reduced_data[0:500]), reduced_data))
    filtered_data2 = cesvec.sos_filter(D2, tmp2)[500:]
    filtered_data2 = np.asarray(filtered_data2)

    # ── 5. Detección ───────────────────────────────────────────
    ev     = cesvec.detection(filtered_data2, reduced_data, UMBRAL)
    nevent = len(ev)
    log.info("  Eventos detectados: %d", nevent)

    resultado = {
        "evento_id":          evento_id,
        "archivo_fuente":     nombre,
        "estacion":           estacion,
        "componente":         COMPONENTE,
        "canal":              canal,
        "volcan":             VOLCAN,
        "periodo_analizado":  {"inicio": t_inicio, "fin": t_fin},
        "umbral":             UMBRAL,
        "fs_hz":              float(fs),
        "n_eventos":          nevent,
        "eventos_detectados": [],
        "procesado_en":       datetime.now().isoformat(),
    }

    if nevent < 1:
        _guardar_json(resultado, estacion, t_inicio)
        return resultado

    # ── 6. Clasificación VGG16 + reglas espectrales ────────────
    log.info("  Clasificando con VGG16 + reglas espectrales...")
    tpclass, tpclass2, prob = cesvec.classification(
        filtered_data, filtered_data2, ev,
        model=_model, ood_detector=_ood_detector,
    )

    # ── 7. Construir eventos ───────────────────────────────────
    eventos = []
    for kevent in range(nevent):
        idx_ini = int(ev[kevent, 0])
        idx_fin = int(ev[kevent, 1])
        inicio2 = float(xtime[ev[kevent, 0]])
        fin2    = float(xtime[ev[kevent, 1]])

        largo        = (idx_fin - idx_ini) / 100.0
        label_event  = EVENTCLASS[tpclass2[kevent] - 1]   # clasificador reglas
        label_vgg    = int(tpclass[kevent])                # clasificador VGG16
        tmp_prob     = prob[kevent]                        # [lp, tr, vt, ot]

        snr_db, ruido_fondo = _calcular_snr(reduced_data, idx_ini, idx_fin)

        seg_fft  = filtered_data[idx_ini:idx_fin]
        frec_max = 0.0
        if len(seg_fft) > 0 and largo > 0:
            y_f    = np.fft.fft(seg_fft)
            n_bins = int(50.0 * largo)
            if n_bins > 0:
                x_f      = np.linspace(0.0, 50.0, n_bins)
                frec_max = float(x_f[np.argmax(np.abs(y_f[0:n_bins]))])

        dt_object = datetime.fromtimestamp(inicio2)
        cod_event = station + dt_object.strftime("%d-%m-%Y_%H:%M:%S.%f")
        cod_event = cod_event[:26] + "0001"

        evento = {
            # Campos espejo de identificacion_senal (legacy)
            "cod_event":          cod_event,
            "cod_event_in":       kevent,
            "volcan":             VOLCAN,
            "est":                estacion,
            "componente":         COMPONENTE,
            "cl_id":              1,
            "algo_detection_id":  1,
            "label_event":        label_event,
            "fecha_pick":         "today",
            "analista":           "detectron",
            "c_label":            1.0,
            "inicio":             inicio2,
            "fin":                fin2,
            "inicio_serial":      719529 + inicio2 / 86400.0,
            "fin_serial":         719529 + fin2    / 86400.0,
            "prob_lp":            float(tmp_prob[0]),
            "prob_tr":            float(tmp_prob[1]),
            "prob_vt":            float(tmp_prob[2]),
            "prob_ot":            float(tmp_prob[3]),
            "campo_1":            UMBRAL,
            "largo":              float(largo),
            "campo_2":            frec_max,
            "label_pipeline_vgg": label_vgg,
            "inicio_iso":         datetime.fromtimestamp(inicio2).isoformat(),
            "fin_iso":            datetime.fromtimestamp(fin2).isoformat(),
            "idx_inicio":         idx_ini,
            "idx_fin":            idx_fin,
            # Campos para deteccion.resultados
            "snr":                snr_db,
            "prom_ruido_fondo":   ruido_fondo,
        }
        eventos.append(evento)
        log.info("    [%02d] %s | %s | %.1fs | fmax=%.1fHz | SNR=%.1fdB",
                 kevent + 1, label_event,
                 dt_object.strftime("%H:%M:%S"),
                 largo, frec_max, snr_db)

    resultado["eventos_detectados"] = eventos
    _guardar_json(resultado, estacion, t_inicio)
    return resultado


def _guardar_json(resultado: dict, estacion: str, t_inicio: str) -> None:
    """Guarda el resultado en out/ como JSON de debug/auditoría."""
    fecha_str  = t_inicio.replace(" ", "_").replace(":", "-")
    ruta_out   = OUT_DIR / f"{estacion}_{fecha_str}.json"
    with open(ruta_out, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2, default=str)
    log.info("  JSON guardado: %s", ruta_out)


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS — deteccion.resultados
# ══════════════════════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def guardar_resultados_deteccion(resultado: dict) -> int:
    """
    Inserta una fila en deteccion.resultados por cada evento detectado.
    Retorna el número de filas insertadas.
    """
    eventos = resultado.get("eventos_detectados", [])
    if not eventos:
        return 0

    sql = """
        INSERT INTO deteccion.resultados
            (evento_id, estacion, componente, snr, label_event,
             prob_vt, prob_lp, prob_tr, prob_ot,
             inicio, fin, largo, prom_ruido_fondo)
        VALUES
            (%(evento_id)s, %(estacion)s, %(componente)s, %(snr)s,
             %(label_event)s, %(prob_vt)s, %(prob_lp)s, %(prob_tr)s,
             %(prob_ot)s, %(inicio)s, %(fin)s, %(largo)s, %(prom_ruido_fondo)s)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ev in eventos:
                cur.execute(sql, {
                    "evento_id":       resultado["evento_id"],
                    "estacion":        ev["est"],
                    "componente":      ev["componente"],
                    "snr":             ev["snr"],
                    "label_event":     ev["label_event"],
                    "prob_vt":         ev["prob_vt"],
                    "prob_lp":         ev["prob_lp"],
                    "prob_tr":         ev["prob_tr"],
                    "prob_ot":         ev["prob_ot"],
                    "inicio":          ev["inicio"],
                    "fin":             ev["fin"],
                    "largo":           ev["largo"],
                    "prom_ruido_fondo": ev["prom_ruido_fondo"],
                })
    log.info("DB: %d evento(s) guardados en deteccion.resultados (evento_id=%s)",
             len(eventos), resultado["evento_id"])
    return len(eventos)


# ══════════════════════════════════════════════════════════════
# RABBITMQ — Consumer + Publicar callback
# ══════════════════════════════════════════════════════════════

def publicar_callback(resultado: dict) -> None:
    """
    Publica DETECCION_COMPLETADA al core.
    El core espera: estacion, componente, inicio, snr, label_event.
    """
    evs = resultado.get("eventos_detectados", [])
    rep = evs[0] if evs else {}

    msg = {
        "evento_id": resultado["evento_id"],
        "tipo":      "DETECCION_COMPLETADA",
        "payload": {
            "estacion":    resultado["estacion"],
            "componente":  resultado["componente"],
            "inicio":      rep.get("inicio"),
            "snr":         rep.get("snr"),
            "label_event": rep.get("label_event"),
            "n_eventos":   resultado["n_eventos"],
        },
    }
    try:
        params = pika.URLParameters(RABBITMQ_URL)
        conn = pika.BlockingConnection(params)
        ch   = conn.channel()
        for q in [Q_IN, Q_CALLBACK]:
            ch.queue_declare(queue=q, durable=True)
        ch.basic_publish(
            exchange="",
            routing_key=Q_CALLBACK,
            body=json.dumps(msg, default=str).encode(),
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json"
            ),
        )
        conn.close()
        log.info("Callback DETECCION_COMPLETADA publicado (evento_id=%s)", resultado["evento_id"])
    except Exception as e:
        log.warning("No se pudo publicar callback MQ: %s", e)


def on_message(ch, method, properties, body):
    """Handler de mensajes RabbitMQ."""
    try:
        tarea      = json.loads(body.decode())
        evento_id  = tarea["evento_id"]
        log.info("Tarea recibida vía MQ: evento_id=%s", evento_id)

        # Modo POC: el mensaje lleva 'archivo'
        # Modo producción (futuro): llamar a WavePyWWS con los parámetros de tiempo
        archivo = tarea.get("archivo")
        if not archivo:
            raise ValueError(
                "El mensaje no contiene 'archivo'. "
                "En producción se llamaría al servidor WWS con inicio_unix/duracion_seg."
            )

        ruta = DATA_DIR / archivo if not Path(archivo).is_absolute() else Path(archivo)
        if not ruta.exists():
            raise FileNotFoundError(f"Archivo no encontrado: {ruta}")

        resultado = run_adove_pipeline(str(ruta), evento_id, estacion_id=tarea.get("estacion_id"))
        guardar_resultados_deteccion(resultado)
        publicar_callback(resultado)

        ch.basic_ack(delivery_tag=method.delivery_tag)
        log.info("Tarea MQ completada: evento_id=%s n_eventos=%d",
                 evento_id, resultado["n_eventos"])

    except Exception as e:
        log.exception("Error procesando tarea MQ: %s", e)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        try:
            err_msg = {
                "evento_id": json.loads(body.decode()).get("evento_id", "unknown"),
                "tipo":      "ERROR_WORKER",
                "payload":   {"error": str(e), "worker": "deteccion"},
            }
            ch.basic_publish(
                exchange="",
                routing_key=Q_CALLBACK,
                body=json.dumps(err_msg).encode(),
                properties=pika.BasicProperties(delivery_mode=2),
            )
        except Exception:
            pass


def run_consumer() -> None:
    """Loop del consumer RabbitMQ (corre en hilo daemon)."""
    while True:
        try:
            params           = pika.URLParameters(RABBITMQ_URL)
            params.heartbeat = 0   # deshabilitado: VGG16 + 680 eventos bloquea el hilo IO
            conn = pika.BlockingConnection(params)
            ch   = conn.channel()
            for q in [Q_IN, Q_CALLBACK]:
                ch.queue_declare(queue=q, durable=True)
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(queue=Q_IN, on_message_callback=on_message, auto_ack=False)
            log.info("Consumer MQ listo. Esperando en '%s'...", Q_IN)
            ch.start_consuming()
        except pika.exceptions.AMQPConnectionError as e:
            log.warning("RabbitMQ no disponible: %s. Reintentando en 5s...", e)
            time.sleep(5)
        except Exception as e:
            log.exception("Error en consumer MQ: %s. Reintentando en 5s...", e)
            time.sleep(5)


# ══════════════════════════════════════════════════════════════
# FASTAPI — endpoints HTTP
# ══════════════════════════════════════════════════════════════

app = FastAPI(
    title="OVDAS Worker Detección",
    description=(
        "Pipeline ADOVE real (VGG16 + reglas espectrales).\n\n"
        "**Modo POC:** `POST /procesar` con un archivo `.mseed` local.\n"
        "**Modo producción:** consume la cola RabbitMQ `ovdas.deteccion` en background."
    ),
    version="1.0.0",
)


class ProcesarRequest(BaseModel):
    archivo:   str            # nombre del archivo .mseed dentro de DATA_DIR
    evento_id: Optional[str] = None   # auto-generado si no se provee


@app.on_event("startup")
def startup():
    cargar_modelos()
    t = threading.Thread(target=run_consumer, daemon=True, name="mq-consumer")
    t.start()
    log.info("Worker Detección listo.")


@app.post("/procesar")
def procesar(req: ProcesarRequest):
    """
    **POC — Procesa un archivo .mseed con el pipeline ADOVE real.**

    - Ejecuta: gapreduce → filtro D/D2 → detección → clasificación VGG16
    - Guarda cada evento en `deteccion.resultados` (PostgreSQL)
    - Publica callback `DETECCION_COMPLETADA` a RabbitMQ (si disponible)
    - Retorna el resultado completo como JSON

    En producción, los datos vendrán del servidor WWS, no de un archivo.
    """
    ruta = DATA_DIR / req.archivo if not Path(req.archivo).is_absolute() else Path(req.archivo)
    if not ruta.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Archivo '{req.archivo}' no encontrado en DATA_DIR={DATA_DIR}"
        )

    evento_id = req.evento_id or f"POC{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[:20]}"

    resultado = run_adove_pipeline(str(ruta), evento_id)

    db_guardado = False
    db_error    = None
    try:
        guardar_resultados_deteccion(resultado)
        db_guardado = True
    except Exception as e:
        db_error = str(e)
        log.warning("No se pudo guardar en DB: %s", e)

    mq_publicado = False
    try:
        if resultado["n_eventos"] > 0:
            publicar_callback(resultado)
            mq_publicado = True
    except Exception as e:
        log.warning("No se pudo publicar callback MQ: %s", e)

    return JSONResponse(content={
        "evento_id":    evento_id,
        "n_eventos":    resultado["n_eventos"],
        "estacion":     resultado["estacion"],
        "db_guardado":  db_guardado,
        "db_error":     db_error,
        "mq_publicado": mq_publicado,
        "resultado":    resultado,
    })


@app.get("/archivos")
def listar_archivos():
    """Lista los archivos `.mseed` disponibles para procesar."""
    archivos = sorted(f.name for f in DATA_DIR.glob("*.mseed"))
    return {"data_dir": str(DATA_DIR), "archivos": archivos}


@app.get("/health")
def health():
    """Liveness + readiness check."""
    status: dict = {
        "modelo_vgg16_cargado": _model is not None,
        "ood_detector_cargado": _ood_detector is not None,
        "data_dir":             str(DATA_DIR),
        "data_dir_existe":      DATA_DIR.exists(),
    }
    try:
        conn = get_conn()
        conn.close()
        status["db"] = "up"
    except Exception as e:
        status["db"] = f"error: {e}"
    return status


if __name__ == "__main__":
    uvicorn.run("worker:app", host="0.0.0.0", port=8001, reload=False)
