"""
Core Orchestrator - Máquina de estados del pipeline OVDAS.

Patrón: Orchestration-based Saga
  - Worker completa tarea → publica en Q_CALLBACKS
  - Core recibe callback → avanza estado → publica siguiente tarea
"""

import json
import time
import logging
import pika
import pika.exceptions

from config import (
    RABBITMQ_URL,
    Q_DETECCION,
    Q_PICKING,
    Q_LOCALIZACION,
    Q_CALLBACKS,
    Estado,
    TipoCallback,
    MAX_RETRY_MQ,
    RETRY_DELAY,
)
import db as db

log = logging.getLogger("core.orchestrator")


def _conectar_rabbitmq(retries: int = MAX_RETRY_MQ) -> pika.BlockingConnection:
    """Conecta a RabbitMQ con reintentos (para esperar al contenedor)."""
    params = pika.URLParameters(RABBITMQ_URL)
    params.heartbeat = 60
    for intento in range(1, retries + 1):
        try:
            conn = pika.BlockingConnection(params)
            log.info("RabbitMQ conectado OK (intento %d)", intento)
            return conn
        except pika.exceptions.AMQPConnectionError as e:
            log.warning(
                "RabbitMQ no disponible (intento %d/%d): %s", intento, retries, e
            )
            if intento == retries:
                raise
            time.sleep(RETRY_DELAY)


def _declarar_colas(channel: pika.channel.Channel) -> None:
    for q in [Q_DETECCION, Q_PICKING, Q_LOCALIZACION, Q_CALLBACKS]:
        channel.queue_declare(queue=q, durable=True)


def publicar_tarea(queue: str, payload: dict) -> None:
    """Publica un mensaje en la cola indicada (crea conexión efímera)."""
    conn = _conectar_rabbitmq(retries=3)
    try:
        ch = conn.channel()
        _declarar_colas(ch)
        ch.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(payload, default=str).encode(),
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json"  # persistente en disco
            ),
        )
        log.info("Tarea publicada → %s | evento_id=%s", queue, payload.get("evento_id"))
    finally:
        conn.close()


def iniciar_evento(
    evento_id: str,
    volcan_id: str,
    estacion_id: str,
    componente: str,
    inicio_unix: float,
    duracion_seg: float,
    muestra_hz: int,
    archivo: str = None,
    grupo_id: str = None,
    n_estaciones: int = 1,
) -> None:
    """
    Fase 1 - Ingesta:
      Guarda evento en BD y lanza tarea de detección.
      grupo_id / n_estaciones permiten agrupar múltiples estaciones
      bajo el mismo evento (multi-station pipeline).
    """
    metadata = {
        "estacion_id": estacion_id,
        "componente": componente,
        "inicio_unix": inicio_unix,
        "duracion_seg": duracion_seg,
        "muestra_hz": muestra_hz,
        "grupo_id": grupo_id,
        "n_estaciones": n_estaciones,
    }
    db.crear_evento(evento_id, volcan_id, estacion_id, metadata)
    db.actualizar_estado(evento_id, Estado.DETECTANDO)

    tarea = {
        "evento_id": evento_id,
        "volcan_id": volcan_id,
        "estacion_id": estacion_id,
        "componente": componente,
        "inicio_unix": inicio_unix,
        "duracion_seg": duracion_seg,
        "muestra_hz": muestra_hz,
        "archivo": archivo,
    }
    publicar_tarea(Q_DETECCION, tarea)
    log.info("Evento iniciado: %s → DETECTANDO (archivo=%s, grupo=%s, n_est=%d)",
             evento_id, archivo, grupo_id, n_estaciones)


def agregar_estacion(
    evento_id: str,
    estacion_id: str,
    componente: str,
    inicio_unix: float,
    duracion_seg: float,
    muestra_hz: int,
    archivo: str = None,
) -> None:
    """
    Agrega una segunda (o N-ésima) estación a un evento ya existente.
    Solo publica una tarea de detección adicional bajo el mismo evento_id.
    No crea ni modifica el registro del evento.
    """
    tarea = {
        "evento_id": evento_id,
        "estacion_id": estacion_id,
        "componente": componente,
        "inicio_unix": inicio_unix,
        "duracion_seg": duracion_seg,
        "muestra_hz": muestra_hz,
        "archivo": archivo,
    }
    publicar_tarea(Q_DETECCION, tarea)
    log.info("Estación adicional %s añadida al evento %s (archivo=%s)",
             estacion_id, evento_id, archivo)


def _manejar_deteccion_completada(evento_id: str, payload: dict) -> None:
    """
    Callback: DETECCION_COMPLETADA
      → si la estación detectó eventos, avanza a PICKING y publica tarea.
      → si no detectó nada (n_eventos=0), descuenta esa estación del total
        esperado y verifica si ya se puede avanzar a LOCALIZANDO.
    """
    db.actualizar_estado(evento_id, Estado.PICKING)

    inicio_unix  = payload.get("inicio")
    n_eventos    = payload.get("n_eventos", 0)

    if not inicio_unix or n_eventos == 0:
        # Estación silenciosa: no hay nada que pickear.
        # No se inserta fila basura en picking.resultados.
        # Se descuenta del total esperado y se verifica si procede localización.
        log.info(
            "DETECCION_COMPLETADA sin eventos para estación %s (evento %s). "
            "Omitiendo picking para esta estación.",
            payload.get("estacion"), evento_id
        )
        _descontar_estacion_silenciosa(evento_id)
        return

    tarea = {
        "evento_id":   evento_id,
        "estacion_id": payload.get("estacion"),
        "componente":  payload.get("componente", "Z"),
        "inicio_unix": inicio_unix,
        "snr":         payload.get("snr"),
        "label_event": payload.get("label_event"),
    }
    publicar_tarea(Q_PICKING, tarea)
    log.info(
        "Callback DETECCION_COMPLETADA → evento %s avanza a PICKING "
        "(n_eventos=%d estacion=%s)",
        evento_id, n_eventos, payload.get("estacion")
    )


def _descontar_estacion_silenciosa(evento_id: str) -> None:
    """
    Una estación no detectó eventos → no habrá pick de ella.
    Decrementa n_estaciones en metadata y verifica si el resto
    de picks ya llegaron para poder disparar la localización.
    """
    evento   = db.get_evento(evento_id)
    metadata = ((evento or {}).get("traza_metadata") or {})

    n_esperadas = int(metadata.get("n_estaciones", 1))
    n_esperadas = max(0, n_esperadas - 1)          # descuenta esta estación
    db.actualizar_n_estaciones(evento_id, n_esperadas)

    n_recibidas = db.contar_picks(evento_id)
    log.info(
        "Estación silenciosa descontada. Evento %s: picks %d/%d",
        evento_id, n_recibidas, n_esperadas
    )
    if n_recibidas >= n_esperadas:
        if n_esperadas == 0:
            db.actualizar_estado(evento_id, Estado.COMPLETADO)
            log.info(
                "Evento %s sin picks útiles (todas las estaciones silenciosas) → "
                "COMPLETADO sin localización", evento_id
            )
        else:
            db.actualizar_estado(evento_id, Estado.LOCALIZANDO)
            publicar_tarea(Q_LOCALIZACION, {"evento_id": evento_id})
            log.info("Picks suficientes tras descuento → evento %s LOCALIZANDO", evento_id)


def _manejar_picking_completado(evento_id: str, payload: dict) -> None:
    """
    Callback: PICKING_COMPLETADO
      → si llegaron picks de TODAS las estaciones esperadas, avanza a
        LOCALIZANDO y publica tarea; si no, espera más picks.
    """
    evento = db.get_evento(evento_id)
    metadata = (evento or {}).get("traza_metadata") or {}
    n_esperadas = int(metadata.get("n_estaciones", 1))
    n_recibidas = db.contar_picks(evento_id)

    log.info("PICKING_COMPLETADO evento=%s picks=%d/%d",
             evento_id, n_recibidas, n_esperadas)

    if n_recibidas >= n_esperadas:
        db.actualizar_estado(evento_id, Estado.LOCALIZANDO)
        publicar_tarea(Q_LOCALIZACION, {"evento_id": evento_id})
        log.info("Todos los picks recibidos → evento %s avanza a LOCALIZANDO", evento_id)
    else:
        log.info("Esperando picks de más estaciones (%d/%d) para evento %s",
                 n_recibidas, n_esperadas, evento_id)


def _manejar_localizacion_completada(evento_id: str, payload: dict) -> None:
    """
    Callback: LOCALIZACION_COMPLETADA
      → avanza a COMPLETADO. Fin del pipeline.
    """
    db.actualizar_estado(evento_id, Estado.COMPLETADO)
    log.info(
        "Callback LOCALIZACION_COMPLETADA → evento %s COMPLETADO "
        "(lat=%s lon=%s z=%s m rmse=%s ml=%s)",
        evento_id,
        payload.get("lat"), payload.get("lon"), payload.get("z_m"),
        payload.get("rmse"), payload.get("ml"),
    )


def _manejar_error(evento_id: str, payload: dict) -> None:
    error_msg = payload.get("error", "Error desconocido en worker")
    db.actualizar_estado(evento_id, Estado.ERROR, error_msg=error_msg)
    log.error("ERROR en evento %s: %s", evento_id, error_msg)


def _procesar_callback(ch, method, properties, body):
    """Callback de pika al recibir mensaje en Q_CALLBACKS."""
    try:
        msg = json.loads(body.decode())
        evento_id = msg.get("evento_id")
        tipo = msg.get("tipo")

        if not evento_id or not tipo:
            log.warning("Mensaje inválido (sin evento_id o tipo): %s", msg)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        log.info("Callback recibido: tipo=%s evento_id=%s", tipo, evento_id)

        if tipo == TipoCallback.DETECCION_COMPLETADA:
            _manejar_deteccion_completada(evento_id, msg.get("payload", {}))
        elif tipo == TipoCallback.PICKING_COMPLETADO:
            _manejar_picking_completado(evento_id, msg.get("payload", {}))
        elif tipo == TipoCallback.LOCALIZACION_COMPLETADA:
            _manejar_localizacion_completada(evento_id, msg.get("payload", {}))
        elif tipo == TipoCallback.ERROR_WORKER:
            _manejar_error(evento_id, msg.get("payload", {}))
        else:
            log.warning("Tipo de callback desconocido: %s", tipo)

        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        log.exception("Error procesando callback: %s", e)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def start_consuming():
    """
    Loop del consumidor de callbacks (corre en hilo separado).
    Se reconecta automáticamente si pierde conexión.
    """
    log.info("Iniciando consumidor de callbacks en Q_CALLBACKS...")
    while True:
        try:
            conn = _conectar_rabbitmq()
            ch = conn.channel()
            _declarar_colas(ch)
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(
                queue=Q_CALLBACKS,
                on_message_callback=_procesar_callback,
                auto_ack=False,
            )
            log.info("Esperando callbacks en %s ...", Q_CALLBACKS)
            ch.start_consuming()

        except pika.exceptions.AMQPConnectionError:
            log.warning(
                "Conexión RabbitMQ perdida. Reintentando en %ds...", RETRY_DELAY
            )
            time.sleep(RETRY_DELAY)
        except Exception as e:
            log.exception("Error inesperado en consumidor: %s. Reintentando...", e)
            time.sleep(RETRY_DELAY)
