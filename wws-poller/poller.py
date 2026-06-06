"""
WWSPoller — Servicio cron de descarga desde servidor WWS.

Ciclo de trabajo (cada POLL_INTERVAL_SEC segundos):
  Para cada estación configurada:
    1. Consulta el servidor WWS (modo dummy: copia archivo template)
    2. Guarda el archivo .mseed en /shared/data/ (volumen compartido)
    3. Registra la descarga en wws.descargas (PostgreSQL)
    4. Entrega al core via POST /ingesta/traza (el core gestiona el pipeline completo)
    5. Actualiza el estado del registro (INGRESADO o ERROR)

Variables de entorno:
  WWS_HOST          host del servidor WWS         (default: wws.sernageomin.cl)
  WWS_PORT          puerto del servidor WWS        (default: 29384)
  POLL_INTERVAL_SEC segundos entre ciclos          (default: 300)
  ESTACIONES        lista "estacion:volcan" separada por comas (default: FU2:VLL)
  COMPONENTE        componente sísmica             (default: Z)
  CANAL             canal completo                 (default: HHZ)
  DURACION_SEG      duración de la ventana temporal (default: 3600)
  MUESTRA_HZ        frecuencia de muestreo         (default: 100)
  SHARED_DATA_DIR   volumen compartido con workers  (default: /shared/data)
  TEMPLATES_DIR     templates .mseed para dummy    (default: /templates)
  POSTGRES_DSN      cadena de conexión PostgreSQL
  CORE_API_URL      URL base del core orchestrator (default: http://core:8000)
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import requests

from wws_client import WWSClient

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] wws-poller: %(message)s",
)
log = logging.getLogger("wws.poller")

# ── Config ─────────────────────────────────────────────────────
WWS_HOST = os.getenv("WWS_HOST", "wws.sernageomin.cl")
WWS_PORT = int(os.getenv("WWS_PORT", "29384"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "300"))
COMPONENTE = os.getenv("COMPONENTE", "Z")
CANAL = os.getenv("CANAL", "HHZ")
DURACION_SEG = int(os.getenv("DURACION_SEG", "3600"))
MUESTRA_HZ = int(os.getenv("MUESTRA_HZ", "100"))

SHARED_DATA_DIR = Path(os.getenv("SHARED_DATA_DIR", "/shared/data"))
TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", "/templates"))
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN", "host=localhost port=5432 dbname=ovdas user=ovdas password=ovdas"
)
CORE_API_URL = os.getenv("CORE_API_URL", "http://core:8000")

# Parsear ESTACIONES: "FU2:VLL,CHS:CHI"  →  {"FU2": "VLL", "CHS": "CHI"}
_raw_estaciones = os.getenv("ESTACIONES", "CHS:99")
ESTACIONES: dict[str, str] = {}
for par in _raw_estaciones.split(","):
    par = par.strip()
    if ":" in par:
        est, vol = par.split(":", 1)
        ESTACIONES[est.strip()] = vol.strip()
    else:
        # Sin volcán definido → advertir; se usará "???" para que el core rechace
        log.warning(
            "Estación '%s' sin volcán asignado. Usa formato 'ESTACION:VOLCAN'.", par
        )
        ESTACIONES[par] = "???"

SHARED_DATA_DIR.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS — wws.descargas
# ══════════════════════════════════════════════════════════════


def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def _esperar_db(reintentos: int = 12, delay: int = 5) -> None:
    """Espera a que PostgreSQL esté disponible antes de arrancar."""
    for intento in range(1, reintentos + 1):
        try:
            get_conn().close()
            log.info("PostgreSQL disponible (intento %d).", intento)
            return
        except Exception as e:
            log.warning("DB no disponible (intento %d/%d): %s", intento, reintentos, e)
            if intento == reintentos:
                raise
            time.sleep(delay)


def registrar_descarga(
    estacion: str,
    componente: str,
    inicio_unix: float,
    fin_unix: float,
    archivo: str,
) -> int:
    """Inserta un registro PENDIENTE en wws.descargas. Retorna el id."""
    sql = """
        INSERT INTO wws.descargas
            (estacion, componente, inicio_unix, fin_unix, archivo, estado)
        VALUES (%s, %s, %s, %s, %s, 'PENDIENTE')
        RETURNING id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (estacion, componente, inicio_unix, fin_unix, archivo))
            return cur.fetchone()[0]


def actualizar_descarga(
    descarga_id: int,
    estado: str,
    evento_id: str = None,
    worker_resp: dict = None,
) -> None:
    """Actualiza estado, evento_id y respuesta del core en wws.descargas."""
    sql = """
        UPDATE wws.descargas
        SET estado = %s,
            evento_id = COALESCE(%s, evento_id),
            worker_resp = %s,
            updated_at = NOW()
        WHERE id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    estado,
                    evento_id,
                    json.dumps(worker_resp, default=str) if worker_resp else None,
                    descarga_id,
                ),
            )


# ══════════════════════════════════════════════════════════════
# CICLO DE POLLING
# ══════════════════════════════════════════════════════════════


def poll_estacion(
    wws: WWSClient,
    estacion: str,
    volcan_id: str,
    grupo_id: str = None,
    n_estaciones: int = 1,
) -> None:
    """
    Ejecuta un ciclo completo de poll para una estación:
      descarga → DB → entrega al core → actualiza DB

    grupo_id: ID compartido por todas las estaciones del mismo ciclo.
              Permite al core agrupar picks bajo un mismo evento_id.
    n_estaciones: total de estaciones en el ciclo (para que el core
                  sepa cuántos picks esperar antes de lanzar localización).
    """
    ahora = datetime.now(tz=timezone.utc)
    fin_dt = ahora
    inicio_dt = ahora - timedelta(seconds=DURACION_SEG)
    inicio_unix = inicio_dt.timestamp()
    fin_unix = fin_dt.timestamp()

    log.info(
        "Polling estacion=%s volcan=%s  ventana: %s → %s",
        estacion,
        volcan_id,
        inicio_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        fin_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    descarga_id = None
    try:
        # ── 1. Descargar del servidor WWS ──────────────────────
        archivo = wws.get_waveform(
            estacion=estacion,
            componente=COMPONENTE,
            canal=CANAL,
            inicio=inicio_dt,
            fin=fin_dt,
            output_dir=SHARED_DATA_DIR,
        )
        log.info("Archivo disponible: %s/%s", SHARED_DATA_DIR, archivo)

        # ── 2. Registrar descarga en DB ────────────────────────
        descarga_id = registrar_descarga(
            estacion=estacion,
            componente=COMPONENTE,
            inicio_unix=inicio_unix,
            fin_unix=fin_unix,
            archivo=archivo,
        )
        log.info("DB registro creado: id=%d estado=PENDIENTE", descarga_id)

        # ── 3. Entregar al core orchestrator ───────────────────
        payload = {
            "volcan_id": volcan_id,
            "estacion_id": estacion,
            "componente": COMPONENTE,
            "inicio_unix": inicio_unix,
            "duracion_seg": float(DURACION_SEG),
            "muestra_hz": MUESTRA_HZ,
            "extra": {
                "archivo": archivo,       # nombre de archivo en /shared/data/
                "grupo_id": grupo_id,     # ID de ciclo compartido (multi-estación)
                "n_estaciones": n_estaciones,
            },
        }
        log.info(
            "POST %s/ingesta/traza  estacion=%s archivo=%s",
            CORE_API_URL,
            estacion,
            archivo,
        )

        resp = requests.post(
            f"{CORE_API_URL}/ingesta/traza",
            json=payload,
            timeout=30,  # core responde 202 de inmediato
        )
        resp.raise_for_status()
        core_resp = resp.json()

        evento_id = core_resp.get("evento_id")
        log.info(
            "Core aceptó → evento_id=%s estado=%s",
            evento_id,
            core_resp.get("estado"),
        )

        # ── 4. Actualizar estado → INGRESADO ───────────────────
        actualizar_descarga(
            descarga_id, "INGRESADO", evento_id=evento_id, worker_resp=core_resp
        )

    except requests.exceptions.ConnectionError as e:
        log.error("Core no disponible en %s: %s", CORE_API_URL, e)
        if descarga_id:
            actualizar_descarga(
                descarga_id, "ERROR", worker_resp={"error": str(e), "fase": "core_post"}
            )

    except requests.exceptions.HTTPError as e:
        body_txt = e.response.text if e.response else ""
        log.error(
            "Core retornó error HTTP %s: %s  body=%s",
            e.response.status_code,
            e,
            body_txt,
        )
        if descarga_id:
            actualizar_descarga(
                descarga_id,
                "ERROR",
                worker_resp={
                    "error": str(e),
                    "fase": "core_http",
                    "status_code": e.response.status_code,
                    "body": body_txt,
                },
            )

    except FileNotFoundError as e:
        log.error("Template no encontrado para estacion=%s: %s", estacion, e)

    except Exception as e:
        log.exception("Error inesperado en poll estacion=%s: %s", estacion, e)
        if descarga_id:
            actualizar_descarga(
                descarga_id,
                "ERROR",
                worker_resp={"error": str(e), "fase": "desconocida"},
            )


# ══════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════════════


def run() -> None:
    log.info("═" * 60)
    log.info("WWSPoller iniciando...")
    log.info("  WWS host   : %s:%d", WWS_HOST, WWS_PORT)
    log.info("  Estaciones : %s", ESTACIONES)
    log.info("  Intervalo  : %ds", POLL_INTERVAL)
    log.info("  Ventana    : %ds (%.1fh)", DURACION_SEG, DURACION_SEG / 3600)
    log.info("  Datos →    : %s", SHARED_DATA_DIR)
    log.info("  Core API   : %s", CORE_API_URL)
    log.info("═" * 60)

    _esperar_db()

    wws = WWSClient(host=WWS_HOST, port=WWS_PORT, templates_dir=TEMPLATES_DIR)

    if wws.ping():
        log.info("WWS ping OK.")
    else:
        log.warning("WWS ping fallido — continuando de todos modos.")

    ciclo = 0
    while True:
        ciclo += 1
        log.info("── Ciclo #%d ──────────────────────────────────────────", ciclo)

        # Grupo compartido por todas las estaciones de este ciclo
        grupo_id = uuid.uuid4().hex[:12]
        n_estaciones = len(ESTACIONES)
        for estacion, volcan_id in ESTACIONES.items():
            poll_estacion(wws, estacion, volcan_id,
                          grupo_id=grupo_id, n_estaciones=n_estaciones)

        log.info("Ciclo #%d completado. Próxima consulta en %ds.", ciclo, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
