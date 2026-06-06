"""
Worker Localización — OVDAS Microservicios
Pipeline real HYPOSAT integrado.

Es el último módulo del pipeline. Arranca después de picking.

Fuente de datos: resultados de picking en PostgreSQL (picking.resultados).
  No necesita descargar datos sísmicos del volumen compartido;
  trabaja directamente con los tiempos t_p / t_s ya calculados.

Flujo:
  1. Recibe tarea de ovdas.localizacion (evento_id)
  2. Consulta picking.resultados WHERE evento_id = ?
  3. Genera archivo hyposat-in con fases P y S por estación
  4. Ejecuta binario HYPOSAT en directorio de trabajo temporal
  5. Parsea hyposat-out: LAT, LON, Z, RMSE, gap, ml, ejes de error
  6. Guarda JSON de auditoría en /app/out/
  7. Guarda resultado en localizacion.resultados (PostgreSQL)
  8. Publica LOCALIZACION_COMPLETADA → core marca pipeline COMPLETADO
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pika
import pika.exceptions
import psycopg2

# ── Config ────────────────────────────────────────────────────
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://ovdas:ovdas@rabbitmq:5672/")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "host=postgres port=5432 dbname=ovdas user=ovdas password=ovdas"
)
OUT_DIR      = Path(os.getenv("OUT_DIR", "/app/out"))
HYPO_DIR     = Path(os.getenv("HYPO_DIR", "/app/hypo"))   # binario + archivos estáticos
OUT_DIR.mkdir(parents=True, exist_ok=True)

Q_IN        = "ovdas.localizacion"
Q_CALLBACK  = "ovdas.callbacks"
RETRY_DELAY = 5

# Parámetros del picker heredados del legacy (configAll.conf)
P_STD      = float(os.getenv("P_STD",      "0.15"))
S_STD      = float(os.getenv("S_STD",      "0.50"))
FLAGS_P    = os.getenv("FLAGS_P",           "T____")
FLAGS_S    = os.getenv("FLAGS_S",           "T____M")
FREQ_JUNK  = float(os.getenv("FREQ_JUNK",  "2.4"))   # Hz — período por defecto cuando freq=0
MIN_PICKS  = int(os.getenv("MIN_PICKS",    "1"))      # mínimo de fases P para intentar localizar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] worker-localizacion: %(message)s"
)
log = logging.getLogger("worker.localizacion")


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS
# ══════════════════════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def _obtener_picks(conn, evento_id: str) -> list[dict]:
    """
    Recupera todos los picks disponibles para el evento desde picking.resultados.
    En la arquitectura actual, típicamente hay 1 pick por evento_id (una estación).
    Cuando se integren múltiples estaciones, habrá N picks por evento.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT estacion, componente, t_p, t_s, amplitud, freq_dom
            FROM   picking.resultados
            WHERE  evento_id = %s
            AND    t_p > 0
            ORDER  BY created_at
            """,
            (evento_id,)
        )
        rows = cur.fetchall()
    return [
        {
            "estacion":  r[0],
            "componente": r[1],
            "t_p":       r[2],
            "t_s":       r[3],
            "amplitud":  r[4],
            "freq_dom":  r[5],
        }
        for r in rows
    ]


def guardar_resultado(conn, resultado: dict) -> None:
    sql = """
        INSERT INTO localizacion.resultados
            (evento_id, tiempo_origen, lat, lon, z_m,
             rmse, major_half_axes, minor_half_axes, dz, gap, ml, n_fases,
             n_picks_usados, autor)
        VALUES
            (%(evento_id)s, %(tiempo_origen)s, %(lat)s, %(lon)s, %(z_m)s,
             %(rmse)s, %(major_half_axes)s, %(minor_half_axes)s, %(dz)s,
             %(gap)s, %(ml)s, %(n_fases)s, %(n_picks_usados)s, %(autor)s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, {
            "evento_id":       resultado["evento_id"],
            "tiempo_origen":   resultado.get("tiempo_origen"),
            "lat":             resultado.get("lat"),
            "lon":             resultado.get("lon"),
            "z_m":             resultado.get("z_m"),
            "rmse":            resultado.get("rmse"),
            "major_half_axes": resultado.get("major_half_axes"),
            "minor_half_axes": resultado.get("minor_half_axes"),
            "dz":              resultado.get("dz"),
            "gap":             resultado.get("gap"),
            "ml":              resultado.get("ml"),
            "n_fases":         resultado.get("n_fases"),
            "n_picks_usados":  resultado.get("n_picks_usados", 0),
            "autor":           resultado.get("autor", "hyposat_auto"),
        })
    conn.commit()
    log.info("DB: resultado guardado en localizacion.resultados (evento_id=%s)",
             resultado["evento_id"])


def _guardar_json(resultado: dict) -> None:
    ruta_out = OUT_DIR / f"localizacion_{resultado['evento_id']}.json"
    with open(ruta_out, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2, default=str)
    log.info("JSON guardado: %s", ruta_out)


# ══════════════════════════════════════════════════════════════
# GENERACIÓN DE hyposat-in
# Portado desde Localization/model/dataservers.py → write2hyposat_4SQL
# ══════════════════════════════════════════════════════════════

def _codigo_estacion_hyposat(estacion: str) -> str:
    """
    Construye el código de estación para hyposat-in.
    Hyposat usa el código de 4 chars: primeros 3 de la estación + componente Z.
    Ej: CHS → CHSZ | FU2 → FU2Z | EBOZ → EBOZ (EBO + Z)
    Debe coincidir exactamente con stations.dat.
    """
    return (estacion[:3] + "Z").upper()


def _ts_a_hyposat(ts_unix: float) -> str:
    """Convierte timestamp Unix a formato hyposat-in: YYYY MM DD HH MM SS"""
    dt = datetime.utcfromtimestamp(ts_unix)
    return dt.strftime("%Y %m %d %H %M %S")


def _escribir_hyposat_in(picks: list[dict], ruta: Path) -> int:
    """
    Genera el archivo hyposat-in con las fases P y S de cada pick.
    Retorna el número de fases P escritas.
    """
    n_p = 0
    with open(ruta, "w") as f:
        f.write("Localizacion automatica OVDAS-Microservicios\n")
        f.write("*23456789 123456789 123456789 123456789 "
                "123456789 123456789 123456789 123456789 "
                "123456789 123456789 123456789 123456789\n")
        f.write("*aaaa aaaaaaaa iiii ii ii ii ii ff.fff "
                "f.fff fff.ff ff.ff ff.ff ff.ff aaaaaaa "
                "ff.fff fffffffff.ff ffff.ff aaaaaaaa ff.ff\n")

        for pick in picks:
            station = _codigo_estacion_hyposat(pick["estacion"])
            t_p     = pick["t_p"]
            t_s     = pick["t_s"]
            amp     = pick["amplitud"] or 0.0
            freq    = pick["freq_dom"] or 0.0
            period  = 1.0 / freq if freq > 0 else 1.0 / FREQ_JUNK

            # Fase P
            date_p = _ts_a_hyposat(t_p)
            vals_p = (station, "P", date_p, P_STD, -1.0, -1.0, -1.0, -1.0, FLAGS_P)
            f.write("%-5s %-8s %s %5.3f %6.2f %5.2f %5.2f %5.2f %-6s\n" % vals_p)
            n_p += 1

            # Fase S (solo si t_s válido y > t_p)
            if t_s and t_s > t_p:
                date_s = _ts_a_hyposat(t_s)
                vals_s = (station, "S", date_s, S_STD, -1.0, -1.0, -1.0, -1.0,
                          FLAGS_S, period, amp)
                f.write("%-5s %-8s %s %5.3f %6.2f %5.2f %5.2f %5.2f %-6s %6.3f %12.2f\n"
                        % vals_s)

    return n_p


# ══════════════════════════════════════════════════════════════
# EJECUCIÓN DE HYPOSAT
# ══════════════════════════════════════════════════════════════

def _run_hyposat(workdir: Path) -> tuple[bool, str]:
    """
    Ejecuta el binario hyposat en workdir.
    Retorna (éxito, contenido_hyposat_out).
    """
    hyposat_bin = HYPO_DIR / "hyposat"
    try:
        result = subprocess.run(
            [str(hyposat_bin)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=120
        )
        out_file = workdir / "hyposat-out"
        if out_file.exists() and out_file.stat().st_size > 500:
            return True, out_file.read_text(errors="replace")
        return False, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        log.warning("hyposat timeout en %s", workdir)
        return False, "timeout"
    except Exception as e:
        log.warning("Error ejecutando hyposat: %s", e)
        return False, str(e)


# ══════════════════════════════════════════════════════════════
# PARSEO DE hyposat-out
# Portado desde Localization/model/post_pro_hyposat.py → read_4table3
# ══════════════════════════════════════════════════════════════

def _parsear_hyposat_out(contenido: str) -> dict:
    """
    Extrae los parámetros de localización del archivo hyposat-out.
    Retorna dict con: tiempo_origen, lat, lon, z_m, rmse, gap, ml,
                      major_half_axes, minor_half_axes, dz, n_fases.
    Valores por defecto -999 cuando el campo no está en la salida.
    """
    resultado = {
        "tiempo_origen":   None,
        "lat":             None,
        "lon":             None,
        "z_m":             None,
        "rmse":            None,
        "dz":              None,
        "n_fases":         None,
        "gap":             -999.0,
        "ml":              -999.0,
        "major_half_axes": -999.0,
        "minor_half_axes": -999.0,
    }

    lineas = contenido.splitlines()
    for idx, linea in enumerate(lineas):

        # Tiempo origen, lat, lon, profundidad, RMSE, n_fases
        if "T0                         LAT      LON       Z" in linea:
            try:
                data = lineas[idx + 1].split()
                # data[0]=fecha  data[1]=HH  data[2]=MM  data[3]=SS.mmm
                resultado["tiempo_origen"] = (
                    data[0] + "T" + data[1] + ":" + data[2] + ":" + data[3] + "Z"
                )
                resultado["lat"]     = float(data[4])
                resultado["lon"]     = float(data[5])
                # Z en hyposat está en km, referida a un nivel cero.
                # El legado usa: Z_m = 3200 - int(float(data[6])*1000)
                resultado["z_m"]     = 3200 - int(float(data[6]) * 1000)
                resultado["rmse"]    = float(data[14])
                resultado["n_fases"] = int(data[13])
                try:
                    resultado["dz"] = int(float(data[10]) * 1000)
                except (IndexError, ValueError):
                    resultado["dz"] = None
            except (IndexError, ValueError) as e:
                log.warning("Error parseando línea de solución hyposat: %s", e)

        # Azimuthal gap
        if "Maximum azimuthal gap of defining observations" in linea:
            try:
                junk = linea.split("=")[-1].replace("[deg]", "").strip()
                resultado["gap"] = float(junk)
            except ValueError:
                pass

        # Magnitud local
        if "Magnitude:" in linea:
            try:
                junk = linea.replace("Magnitude:", "").split("(")[0].strip()
                resultado["ml"] = float(junk)
            except ValueError:
                pass

        # Ejes del elipsoide de error
        if "Major half axis:" in linea:
            try:
                partes = linea.replace(" ", "").split("[km]")
                resultado["major_half_axes"] = float(partes[0].split(":")[-1])
                resultado["minor_half_axes"] = float(partes[1].split(":")[-1])
            except (IndexError, ValueError):
                pass

    return resultado


# ══════════════════════════════════════════════════════════════
# PROCESAMIENTO PRINCIPAL
# ══════════════════════════════════════════════════════════════

def _preparar_workdir(evento_id: str) -> Path:
    """
    Crea un directorio temporal para la corrida de hyposat y copia
    todos los archivos estáticos necesarios (hyposat-parameter, stations.dat,
    modelo de velocidad).
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"hypo_{evento_id}_"))

    # Copiar archivos estáticos desde HYPO_DIR
    for nombre in ["hyposat-parameter", "stations.dat", "model_Chillan_Cardona.dat"]:
        src = HYPO_DIR / nombre
        if src.exists():
            shutil.copy2(str(src), str(workdir / nombre))
        else:
            log.warning("Archivo estático no encontrado: %s", src)

    return workdir


def procesar_localizacion(tarea: dict, conn) -> dict:
    """
    Pipeline completo de localización para un evento:
      1. Recupera picks desde picking.resultados
      2. Genera hyposat-in
      3. Ejecuta HYPOSAT
      4. Parsea hyposat-out
      5. Retorna resultado estructurado
    """
    evento_id = tarea["evento_id"]
    log.info("Localizando evento_id=%s", evento_id)

    # 1. Obtener picks desde DB
    picks = _obtener_picks(conn, evento_id)
    if not picks:
        raise ValueError(
            f"No hay picks válidos en picking.resultados para evento_id={evento_id}"
        )

    n_picks = len(picks)
    log.info("  %d pick(s) disponibles para localización", n_picks)

    # 2. Preparar directorio de trabajo hyposat
    workdir = _preparar_workdir(evento_id)
    try:
        # 3. Escribir hyposat-in
        hyposat_in = workdir / "hyposat-in"
        n_p = _escribir_hyposat_in(picks, hyposat_in)
        log.info("  hyposat-in generado con %d fases P", n_p)

        if n_p < MIN_PICKS:
            log.warning("  Pocas fases P (%d < %d). Omitiendo ejecución hyposat.",
                        n_p, MIN_PICKS)
            solucion = {}
            exitoso  = False
        else:
            # 4. Ejecutar hyposat
            exitoso, hypo_out_content = _run_hyposat(workdir)

            # Guardar hyposat-out raw en out/ para auditoría
            raw_path = OUT_DIR / f"hyposat-out_{evento_id}.txt"
            raw_path.write_text(hypo_out_content, encoding="utf-8", errors="replace")

            if exitoso:
                log.info("  HYPOSAT ejecutado OK")
                solucion = _parsear_hyposat_out(hypo_out_content)
            else:
                log.warning("  HYPOSAT sin solución válida (output insuficiente)")
                solucion = {}

    finally:
        shutil.rmtree(str(workdir), ignore_errors=True)

    resultado = {
        "evento_id":       evento_id,
        "n_picks_usados":  n_picks,
        "hyposat_exitoso": exitoso if n_p >= MIN_PICKS else False,
        # Campos de localización
        "tiempo_origen":   solucion.get("tiempo_origen"),
        "lat":             solucion.get("lat"),
        "lon":             solucion.get("lon"),
        "z_m":             solucion.get("z_m"),
        "rmse":            solucion.get("rmse"),
        "dz":              solucion.get("dz"),
        "gap":             solucion.get("gap"),
        "ml":              solucion.get("ml"),
        "major_half_axes": solucion.get("major_half_axes"),
        "minor_half_axes": solucion.get("minor_half_axes"),
        "n_fases":         solucion.get("n_fases"),
        "autor":           "hyposat_auto",
        "procesado_en":    datetime.now().isoformat(),
    }

    if exitoso:
        log.info(
            "Localización OK → lat=%.4f lon=%.4f z=%s m rmse=%s ml=%s gap=%s°",
            resultado["lat"] or 0, resultado["lon"] or 0,
            resultado["z_m"], resultado["rmse"],
            resultado["ml"], resultado["gap"]
        )
    else:
        log.warning(
            "Localización sin solución para evento_id=%s "
            "(picks=%d, min_requerido=%d)",
            evento_id, n_picks, MIN_PICKS
        )

    return resultado


# ══════════════════════════════════════════════════════════════
# RABBITMQ
# ══════════════════════════════════════════════════════════════

def publicar_callback(channel, resultado: dict) -> None:
    msg = {
        "evento_id": resultado["evento_id"],
        "tipo":      "LOCALIZACION_COMPLETADA",
        "payload": {
            "lat":             resultado.get("lat"),
            "lon":             resultado.get("lon"),
            "z_m":             resultado.get("z_m"),
            "rmse":            resultado.get("rmse"),
            "ml":              resultado.get("ml"),
            "gap":             resultado.get("gap"),
            "n_fases":         resultado.get("n_fases"),
            "hyposat_exitoso": resultado.get("hyposat_exitoso"),
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
    def on_message(ch, method, properties, body):
        evento_id = "unknown"
        try:
            tarea     = json.loads(body.decode())
            evento_id = tarea.get("evento_id", "unknown")
            log.info("Tarea recibida: evento_id=%s", evento_id)

            resultado = procesar_localizacion(tarea, db_conn)
            _guardar_json(resultado)
            guardar_resultado(db_conn, resultado)
            publicar_callback(ch, resultado)

            ch.basic_ack(delivery_tag=method.delivery_tag)
            log.info("Localización completada: evento_id=%s", evento_id)

        except Exception as e:
            log.exception("Error procesando localización evento_id=%s: %s", evento_id, e)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            try:
                err_msg = {
                    "evento_id": evento_id,
                    "tipo":      "ERROR_WORKER",
                    "payload":   {"error": str(e), "worker": "localizacion"},
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
            params           = pika.URLParameters(RABBITMQ_URL)
            params.heartbeat = 0   # hyposat puede tardar varios segundos por evento
            mq_conn = pika.BlockingConnection(params)
            ch      = mq_conn.channel()

            for q in [Q_IN, Q_CALLBACK]:
                ch.queue_declare(queue=q, durable=True)

            ch.basic_qos(prefetch_count=1)

            db_conn = get_conn()
            ch.basic_consume(
                queue=Q_IN,
                on_message_callback=_make_on_message(db_conn),
                auto_ack=False
            )

            log.info("Worker-Localizacion listo. Esperando tareas en '%s'...", Q_IN)
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
