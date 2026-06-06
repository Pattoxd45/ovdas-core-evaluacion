"""
OVDAS Core Orchestrator — API REST de Ingesta.

Endpoints:
  POST /ingesta/traza   → ingresa señal sísmica al pipeline
  GET  /evento/{id}     → consulta estado del evento
  GET  /eventos         → lista eventos recientes
  GET  /health          → liveness check
"""

import logging
import threading

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from config import Estado
from models import TraceInput, EventoResponse
import db as db
import orchestrator as orch

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("core.api")

# ── Aplicación ────────────────────────────────────────────────
app = FastAPI(
    title="OVDAS Core Orchestrator",
    description="Núcleo orquestador del sistema de monitoreo volcánico OVDAS",
    version="1.0.0",
)


@app.on_event("startup")
def startup():
    db.init_db()
    # Consumidor de callbacks en hilo daemon
    t = threading.Thread(target=orch.start_consuming, daemon=True, name="mq-consumer")
    t.start()
    log.info("Core Orchestrator listo.")


# ── Endpoints ─────────────────────────────────────────────────


@app.post("/ingesta/traza", status_code=202, response_model=EventoResponse)
def ingestar_traza(body: TraceInput):
    """
    Fase 1 del pipeline: recibe traza sísmica, valida y encola para detección.
    """
    # Validar estación contra catálogo local (SSOT)
    log.info(
        "Recibida traza: volcán=%s, estación=%s, componente=%s, inicio=%d, dur=%ds",
        body.volcan_id,
        body.estacion_id,
        body.componente,
        body.inicio_unix,
        body.duracion_seg,
    )
    if not db.validar_estacion(body.estacion_id, body.volcan_id):
        raise HTTPException(
            status_code=422,
            detail=f"Estación '{body.estacion_id}' no registrada para volcán '{body.volcan_id}'",
        )

    extra       = body.extra or {}
    archivo     = extra.get("archivo")
    grupo_id    = extra.get("grupo_id")
    n_estaciones = int(extra.get("n_estaciones", 1))

    # ── Multi-estación: reutilizar evento del mismo ciclo ──────
    if grupo_id:
        evento_existente = db.buscar_evento_por_grupo(grupo_id)
        if evento_existente:
            evento_id = evento_existente["evento_id"]
            orch.agregar_estacion(
                evento_id=evento_id,
                estacion_id=body.estacion_id,
                componente=body.componente,
                inicio_unix=body.inicio_unix,
                duracion_seg=body.duracion_seg,
                muestra_hz=body.muestra_hz,
                archivo=archivo,
            )
            log.info("Estación %s añadida al evento existente %s (grupo=%s)",
                     body.estacion_id, evento_id, grupo_id)
            return EventoResponse(
                evento_id=evento_id,
                estado=evento_existente["estado"],
                mensaje=f"Estación {body.estacion_id} añadida al evento del ciclo.",
            )

    # ── Primer POST del ciclo (o single-station): crear nuevo evento ──
    evento_id = body.generar_evento_id()

    orch.iniciar_evento(
        evento_id=evento_id,
        volcan_id=body.volcan_id,
        estacion_id=body.estacion_id,
        componente=body.componente,
        inicio_unix=body.inicio_unix,
        duracion_seg=body.duracion_seg,
        muestra_hz=body.muestra_hz,
        archivo=archivo,
        grupo_id=grupo_id,
        n_estaciones=n_estaciones,
    )

    return EventoResponse(
        evento_id=evento_id,
        estado=Estado.DETECTANDO,
        mensaje="Traza aceptada. Pipeline iniciado.",
    )


@app.get("/evento/{evento_id}")
def get_evento(evento_id: str):
    """Consulta el estado actual de un evento en el pipeline."""
    ev = db.get_evento(evento_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    return JSONResponse(
        content={
            **ev,
            "created_at": str(ev["created_at"]),
            "updated_at": str(ev["updated_at"]),
        }
    )


@app.get("/eventos")
def listar_eventos(limite: int = 50):
    """Lista los últimos eventos procesados."""
    eventos = db.listar_eventos(limite)
    return [
        {**e, "created_at": str(e["created_at"]), "updated_at": str(e["updated_at"])}
        for e in eventos
    ]


@app.get("/health")
def health():
    try:
        db.get_conn().close()
        return {"status": "ok", "db": "up"}
    except Exception as e:
        return JSONResponse(
            status_code=503, content={"status": "error", "detail": str(e)}
        )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
