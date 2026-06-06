"""
Worker Picking P-S — OVDAS Microservicios
Pipeline real (Castilla Picker) integrado.

Fuente de datos: archivo MiniSEED en volumen compartido /shared/data
  (mismo volumen que wws-poller deposita y worker-deteccion lee).

Flujo:
  1. Recibe tarea de ovdas.picking (evento_id, estacion_id, componente, inicio_unix)
  2. Busca el archivo .mseed en wws.descargas por evento_id
  3. Lee el archivo del volumen compartido, extrae ventana de 60s desde inicio_unix
  4. Aplica picker de ondas P y S (portado desde Castilla_Picker.py)
  5. Guarda resultado en picking.resultados (PostgreSQL)
  6. Guarda JSON de auditoría en /app/out/
  7. Publica PICKING_COMPLETADO en ovdas.callbacks
"""

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pika
import pika.exceptions
import psycopg2
from scipy import signal
from scipy.ndimage import gaussian_filter1d
from numpy.linalg import norm

# ── Config ────────────────────────────────────────────────────
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://ovdas:ovdas@rabbitmq:5672/")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "host=postgres port=5432 dbname=ovdas user=ovdas password=ovdas"
)
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
OUT_DIR  = Path(os.getenv("OUT_DIR",  "/app/out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

Q_IN        = "ovdas.picking"
Q_CALLBACK  = "ovdas.callbacks"
RETRY_DELAY = 5
FS_TARGET   = 100   # Hz — los algoritmos asumen 100 Hz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker-picking: %(message)s"
)
log = logging.getLogger("worker.picking")


# ══════════════════════════════════════════════════════════════
# ALGORITMOS DE PICKING
# Portados fielmente desde Castilla_Picker.py / newPrecotev2.py
# ══════════════════════════════════════════════════════════════

def _butter_bandpass(lowcut, highcut, fs, order=10):
    nyq = 0.5 * fs
    b, a = signal.butter(order, [lowcut / nyq, highcut / nyq], btype='band')
    return b, a


def _butter_bandpass_lfilter(data, lowcut, highcut, fs, order=6):
    b, a = _butter_bandpass(lowcut, highcut, fs, order=order)
    return signal.lfilter(b, a, data)


def _frec_fun(yin):
    """Frecuencia dominante via FFT (potencia espectral máxima)."""
    Fs = FS_TARGET
    L  = len(yin)
    if L % 2 > 0:
        L = L - 1
        y = yin[1:L + 1]
    else:
        y = yin
    Y  = pow(abs(np.fft.fft(y)), 2)
    P1 = Y[1:int(L / 2 + 1)]
    f  = np.arange(int(L / 2) + 1) * Fs / L
    return float(f[P1.argmax()])


def _wave_p(y):
    """
    Detección onda P: ratio amplitud máxima ventana larga / ventana corta.
    Parámetros fijados para 100 Hz: ws=400 (4s), wl=100 (1s).
    """
    ws, wl  = 400, 100
    lim_inf = 0.05
    n       = len(y)
    RV      = np.zeros(n)
    LV      = np.zeros(n)
    stmltm  = np.zeros(n)

    for i in range(n - ws):
        RV[i]      = np.max(np.abs(y[i:i + ws]))
        LV[i + wl] = np.max(np.abs(y[i:i + wl]))
        lta        = norm(y[i:i + ws], 1)
        sta        = norm(y[i + ws - wl:i + ws], 1)
        if LV[i] < lim_inf:
            LV[i] = lim_inf
        stmltm[i] = RV[i] / LV[i]

    stmltm = max(LV) / max(RV) * stmltm
    return int(np.argmax(stmltm) + ws - wl)


def _wave_s(y):
    """
    Detección onda S: punto de inflexión máximo en energía espectral acumulada.
    Usa espectrograma con nperseg=300, noverlap=299 (100 Hz).
    """
    f, t, Sxx = signal.spectrogram(y, FS_TARGET, nperseg=300, noverlap=299)
    S   = pow(abs(Sxx), 2)
    Sn  = (S - S.min()) / (S.max() - S.min() + 1e-6)
    u   = Sn.mean()
    B   = Sn >= u
    Su  = Sn * B

    Ev  = Su.sum(axis=0)
    Evn = (Ev - Ev.min()) / (Ev.max() - Ev.min() + 1e-6)
    v   = B.sum(axis=0)
    vn  = (v - v.min()) / (v.max() - v.min() + 1e-6)

    SE   = vn * Evn
    p    = np.cumsum(SE) / SE.sum()
    p_f  = gaussian_filter1d(p, 25)
    p_dd = np.gradient(np.gradient(p_f))

    ts = np.round(t[np.argmax(p_dd)] * FS_TARGET)
    return int(ts)


def _truncar_ts(ts_float: float) -> float:
    """
    Trunca un timestamp unix a ~10ms de precisión.
    Reproducción exacta del truco del legado: str(ts)[:13] → float.
    Ej: 1700000000.123456 → '1700000000.12' → 1700000000.12
    """
    return float(str(ts_float)[:13])


def _picker(traza) -> dict:
    """
    Aplica el picker de ondas P y S a una traza ObsPy.
    Retorna dict con: onda_P, onda_S, snr, freq, amp.
    """
    yraw   = np.array(traza.data, dtype=float)
    times  = traza.times('timestamp')
    datenum = [t for t in times if t != "masked"]

    vacio = {"onda_P": 0.0, "onda_S": 0.0, "snr": 0.0, "freq": 0.0, "amp": 0.0}

    if len(yraw) == 0 or len(datenum) == 0:
        return vacio

    # Bandpass 0.9–12 Hz con padding para evitar artefactos de borde
    pad = min(500, len(yraw))
    tmp = np.concatenate((np.flipud(yraw[:pad]), yraw))
    yf  = _butter_bandpass_lfilter(tmp, 0.9, 12, FS_TARGET)
    yf  = yf[pad:]

    amp_max = float(np.max(np.abs(yf)))
    if amp_max == 0:
        return {**vacio, "freq": _frec_fun(yf) if len(yf) > 1 else 0.0}

    y_l = yf / amp_max
    y   = y_l[:3000] if len(y_l) > 3000 else y_l

    # Traza demasiado corta para los algoritmos (< 7s a 100 Hz)
    if len(y) < 700:
        return {
            "onda_P": 0.0,
            "onda_S": 0.0,
            "snr":    0.0,
            "freq":   _frec_fun(yf),
            "amp":    amp_max,
        }

    # Índices de arribo P y S
    iP = _wave_p(y)
    if iP > len(y) // 2:
        iP = len(y) // 2
    iP = max(iP - 50, 0)

    iS2 = _wave_s(y[iP:])
    iS  = iS2 + iP
    if iS <= iP:                  # fallback: recalcular desde iP
        iS = _wave_s(y[iP:]) + iP

    # Timestamps de P y S
    tP = _truncar_ts(datenum[iP]) if 0 < iP < len(datenum) else 0.0
    tS = _truncar_ts(datenum[iS]) if 0 < iS < len(datenum) else 0.0

    # SNR en dB (señal post-P vs ruido pre-P)
    snr = 0.0
    try:
        a   = np.mean(pow(y[int(iP + 50):-100], 2))
        b   = np.mean(pow(y[101:max(int(iP - 100), 102)], 2))
        snr = 10 * math.log10(abs(a - b) / max(b, 1e-9))
    except (ValueError, ZeroDivisionError):
        pass

    return {
        "onda_P": tP,
        "onda_S": tS,
        "snr":    round(snr, 4),
        "freq":   round(_frec_fun(yf), 4),
        "amp":    round(amp_max, 8),
    }


# ══════════════════════════════════════════════════════════════
# LECTURA DESDE VOLUMEN COMPARTIDO
# ══════════════════════════════════════════════════════════════

def _leer_traza(ruta: str, inicio_unix: float, componente: str = "Z"):
    """
    Lee un archivo MiniSEED del volumen compartido y extrae una ventana
    de 60 segundos desde inicio_unix para la componente indicada.
    Remuestrea a FS_TARGET Hz si es necesario.
    """
    from obspy import read as obspy_read, UTCDateTime

    st = obspy_read(str(ruta))
    st_c = st.select(channel=f"*{componente}")
    if not st_c:
        log.warning("Componente %s no encontrada en %s, usando primer trace.", componente, ruta)
        st_c = st

    st_c.merge(method=1, fill_value=0)

    for tr in st_c:
        if abs(tr.stats.sampling_rate - FS_TARGET) > 1:
            log.info("Remuestreando %.1f Hz → %d Hz", tr.stats.sampling_rate, FS_TARGET)
            tr.resample(FS_TARGET)

    t1   = UTCDateTime(inicio_unix)
    t2   = t1 + 60
    st_c = st_c.slice(t1, t2)

    if len(st_c) == 0:
        raise ValueError(
            f"Sin datos en la ventana {t1} – {t2} "
            f"(componente={componente}, archivo={ruta})"
        )
    return st_c[0]


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS
# ══════════════════════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def _obtener_archivo(conn, evento_id: str) -> str | None:
    """Consulta wws.descargas para obtener el nombre del archivo del evento."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archivo FROM wws.descargas WHERE evento_id = %s LIMIT 1",
            (evento_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def guardar_resultado(conn, resultado: dict) -> None:
    sql = """
        INSERT INTO picking.resultados
            (evento_id, estacion, componente, t_p, t_s, snr, amplitud, polar, freq_dom)
        VALUES
            (%(evento_id)s, %(estacion)s, %(componente)s,
             %(t_p)s, %(t_s)s, %(snr)s, %(amplitud)s, %(polar)s, %(freq_dom)s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, {
            "evento_id":  resultado["evento_id"],
            "estacion":   resultado["estacion"],
            "componente": resultado["componente"],
            "t_p":        resultado["t_p"],
            "t_s":        resultado["t_s"],
            "snr":        resultado["snr"],
            "amplitud":   resultado["amplitud"],
            "polar":      resultado["polar"],
            "freq_dom":   resultado["freq_dom"],
        })
    conn.commit()
    log.info("DB: resultado guardado en picking.resultados (evento_id=%s)",
             resultado["evento_id"])


def _guardar_json(resultado: dict) -> None:
    """Guarda el resultado completo en out/ como JSON de auditoría."""
    ruta_out = OUT_DIR / f"picking_{resultado['evento_id']}.json"
    with open(ruta_out, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2, default=str)
    log.info("JSON guardado: %s", ruta_out)


# ══════════════════════════════════════════════════════════════
# PROCESAMIENTO PRINCIPAL
# ══════════════════════════════════════════════════════════════

def procesar_picking(tarea: dict, conn) -> dict:
    """
    Ejecuta el pipeline de picking real sobre la traza del evento:
      1. Busca el archivo .mseed en wws.descargas
      2. Lee 60s desde inicio_unix del volumen compartido
      3. Aplica filtro bandpass (0.9–12 Hz, 100 Hz)
      4. Detecta arribo P con waveP() (STA/LTA de amplitud)
      5. Detecta arribo S con waveS() (inflexión en espectrograma)
      6. Calcula SNR, frecuencia dominante, amplitud
    """
    evento_id   = tarea["evento_id"]
    estacion    = tarea.get("estacion_id", "UNK")
    componente  = tarea.get("componente", "Z")
    inicio_unix = tarea.get("inicio_unix")
    label_event = tarea.get("label_event", "OT")

    if not inicio_unix:
        raise ValueError(f"inicio_unix no presente en tarea para evento_id={evento_id}")

    inicio_unix = float(inicio_unix)

    # 1. Obtener nombre del archivo desde wws.descargas
    archivo = _obtener_archivo(conn, evento_id)
    if not archivo:
        raise FileNotFoundError(
            f"No se encontró registro en wws.descargas para evento_id={evento_id}"
        )

    ruta = DATA_DIR / archivo if not Path(archivo).is_absolute() else Path(archivo)
    if not ruta.exists():
        raise FileNotFoundError(f"Archivo no encontrado en volumen compartido: {ruta}")

    log.info("Picking: evento_id=%s archivo=%s t_inicio=%.2f", evento_id, archivo, inicio_unix)

    # 2. Leer ventana de 60s del volumen compartido
    traza = _leer_traza(str(ruta), inicio_unix, componente)

    # 3. Aplicar picker real
    datos = _picker(traza)

    resultado = {
        "evento_id":    evento_id,
        "estacion":     estacion,
        "componente":   componente,
        "t_p":          datos["onda_P"],
        "t_s":          datos["onda_S"],
        "snr":          datos["snr"],
        "amplitud":     datos["amp"],
        "polar":        None,   # una sola componente → sin polaridad
        "freq_dom":     datos["freq"],
        "label_event":  label_event,
        "archivo":      archivo,
        "procesado_en": datetime.now().isoformat(),
    }

    # Log legible de resultados
    if datos["onda_P"] and datos["onda_S"]:
        t_p_iso  = datetime.fromtimestamp(datos["onda_P"]).isoformat()
        t_s_iso  = datetime.fromtimestamp(datos["onda_S"]).isoformat()
        delta_ts = round(datos["onda_S"] - datos["onda_P"], 3)
        log.info(
            "Picking OK → t_p=%s  t_s=%s  Δts=%.3fs  snr=%.2fdB  freq=%.2fHz  amp=%.6f",
            t_p_iso, t_s_iso, delta_ts, datos["snr"], datos["freq"], datos["amp"]
        )
    else:
        log.warning(
            "Picking sin arrivals (traza corta o sin señal): snr=%.2f freq=%.2f",
            datos["snr"], datos["freq"]
        )

    return resultado


# ══════════════════════════════════════════════════════════════
# RABBITMQ
# ══════════════════════════════════════════════════════════════

def publicar_callback(channel, resultado: dict) -> None:
    msg = {
        "evento_id": resultado["evento_id"],
        "tipo":      "PICKING_COMPLETADO",
        "payload": {
            "estacion":   resultado["estacion"],
            "componente": resultado["componente"],
            "t_p":        resultado["t_p"],
            "t_s":        resultado["t_s"],
            "snr":        resultado["snr"],
            "freq_dom":   resultado["freq_dom"],
            "amplitud":   resultado["amplitud"],
        },
    }
    channel.basic_publish(
        exchange="",
        routing_key=Q_CALLBACK,
        body=json.dumps(msg, default=str).encode(),
        properties=pika.BasicProperties(
            delivery_mode=2,
            content_type="application/json"
        )
    )


def _make_on_message(db_conn):
    """
    Factoria del handler: inyecta la conexión DB persistente.
    La conexión se comparte en todos los mensajes del mismo ciclo de vida,
    evitando abrir/cerrar una conexión por cada evento.
    """
    def on_message(ch, method, properties, body):
        evento_id = "unknown"
        try:
            tarea     = json.loads(body.decode())
            evento_id = tarea.get("evento_id", "unknown")
            log.info("Tarea recibida: evento_id=%s", evento_id)

            # JSON primero: auditoría garantizada aunque falle DB o MQ
            resultado = procesar_picking(tarea, db_conn)
            _guardar_json(resultado)
            guardar_resultado(db_conn, resultado)
            publicar_callback(ch, resultado)

            ch.basic_ack(delivery_tag=method.delivery_tag)
            log.info("Tarea completada: evento_id=%s", evento_id)

        except Exception as e:
            log.exception("Error procesando tarea evento_id=%s: %s", evento_id, e)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            try:
                err_msg = {
                    "evento_id": evento_id,
                    "tipo":      "ERROR_WORKER",
                    "payload":   {"error": str(e), "worker": "picking"},
                }
                ch.basic_publish(
                    exchange="",
                    routing_key=Q_CALLBACK,
                    body=json.dumps(err_msg).encode(),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
            except Exception:
                pass

    return on_message


def run():
    while True:
        db_conn = None
        try:
            # Heartbeat=0: deshabilita el mecanismo de heartbeat de pika.
            # Necesario porque _picker() (FFT + espectrograma) bloquea el hilo
            # IO durante varios segundos por evento. Con 600+ eventos en cola
            # el tiempo total supera cualquier timeout razonable de heartbeat,
            # haciendo que RabbitMQ cierre la conexión a mitad del procesamiento.
            params           = pika.URLParameters(RABBITMQ_URL)
            params.heartbeat = 0
            mq_conn = pika.BlockingConnection(params)
            ch      = mq_conn.channel()

            for q in [Q_IN, Q_CALLBACK]:
                ch.queue_declare(queue=q, durable=True)

            ch.basic_qos(prefetch_count=1)

            # Conexión DB persistente: reutilizada en todos los mensajes
            db_conn = get_conn()
            ch.basic_consume(
                queue=Q_IN,
                on_message_callback=_make_on_message(db_conn),
                auto_ack=False
            )

            log.info("Worker-Picking listo. Esperando tareas en '%s'...", Q_IN)
            ch.start_consuming()

        except pika.exceptions.AMQPConnectionError as e:
            log.warning("RabbitMQ no disponible: %s. Reintentando en %ds...", e, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
        except Exception as e:
            log.exception("Error inesperado: %s. Reintentando en %ds...", e, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
        finally:
            if db_conn and not db_conn.closed:
                db_conn.close()


if __name__ == "__main__":
    run()
