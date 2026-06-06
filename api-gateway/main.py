"""
OVDAS API Gateway — Agregación de datos (Patrón API Composition).

Consulta en paralelo los micro-schemas de cada worker y
devuelve un objeto JSON consolidado al frontend (Vue.js legacy).

Endpoints:
  GET /reporte/evento/{id}   → reporte completo de un evento
  GET /eventos               → lista paginada con estado
  GET /volcanes              → catálogo de volcanes
  GET /health
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import os

# ── Config ────────────────────────────────────────────────────
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "host=postgres port=5432 dbname=ovdas user=ovdas password=ovdas"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] api-gateway: %(message)s"
)
log = logging.getLogger("api-gateway")

app = FastAPI(
    title="OVDAS API Gateway",
    description="Agregación de datos distribuidos del sistema OVDAS",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers DB ────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def _query_core(evento_id: str) -> dict | None:
    sql = """
        SELECT evento_id, volcan_id, estacion_id, estado,
               traza_metadata, error_msg, created_at, updated_at
          FROM core.pipeline_eventos
         WHERE evento_id = %s
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (evento_id,))
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["created_at"] = str(d["created_at"])
                d["updated_at"] = str(d["updated_at"])
                return d
    return None


def _query_deteccion(evento_id: str) -> dict | None:
    sql = """
        SELECT snr, label_event, prob_vt, prob_lp, prob_tr, prob_ot,
               inicio, fin, largo, prom_ruido_fondo, created_at
          FROM deteccion.resultados
         WHERE evento_id = %s
         ORDER BY id DESC LIMIT 1
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (evento_id,))
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["created_at"] = str(d["created_at"])
                return d
    return None


def _query_picking(evento_id: str) -> dict | None:
    sql = """
        SELECT estacion, componente, t_p, t_s, amplitud, polar, freq_dom, created_at
          FROM picking.resultados
         WHERE evento_id = %s
         ORDER BY id DESC LIMIT 1
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (evento_id,))
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["created_at"] = str(d["created_at"])
                # Calcular delta_ts (útil para Hyposat cuando se integre)
                if d.get("t_p") and d.get("t_s"):
                    d["delta_ts"] = round(d["t_s"] - d["t_p"], 4)
                return d
    return None


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/reporte/evento/{evento_id}")
def reporte_evento(evento_id: str):
    """
    Patrón API Composition: consultas paralelas a micro-schemas
    y fusión (stitching) en un único objeto JSON.
    """
    # Consultas paralelas a los tres schemas
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_query_core, evento_id):       "core",
            executor.submit(_query_deteccion, evento_id):  "deteccion",
            executor.submit(_query_picking, evento_id):    "picking",
        }
        resultados = {}
        for future in as_completed(futures):
            nombre = futures[future]
            try:
                resultados[nombre] = future.result()
            except Exception as e:
                log.warning("Error consultando %s: %s", nombre, e)
                resultados[nombre] = None

    if resultados["core"] is None:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    # Fusión de datos (Stitching)
    return {
        "evento_id":  evento_id,
        "pipeline":   resultados["core"],
        "deteccion":  resultados["deteccion"],
        "picking":    resultados["picking"],
        # Placeholder para futuros workers
        "localizacion": None,
        "clasificacion": None,
        "parametros": None
    }


@app.get("/eventos")
def listar_eventos(limite: int = 50, estado: str = None):
    """Lista eventos del pipeline, opcionalmente filtrado por estado."""
    sql = """
        SELECT e.evento_id, e.volcan_id, e.estacion_id, e.estado,
               e.created_at, e.updated_at,
               d.label_event, d.snr,
               p.t_p, p.t_s
          FROM core.pipeline_eventos e
          LEFT JOIN LATERAL (
              SELECT label_event, snr FROM deteccion.resultados
               WHERE evento_id = e.evento_id
               ORDER BY id DESC LIMIT 1
          ) d ON true
          LEFT JOIN LATERAL (
              SELECT t_p, t_s FROM picking.resultados
               WHERE evento_id = e.evento_id
               ORDER BY id DESC LIMIT 1
          ) p ON true
    """
    params = []
    if estado:
        sql += " WHERE e.estado = %s"
        params.append(estado)
    sql += " ORDER BY e.created_at DESC LIMIT %s"
    params.append(limite)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [
                {**dict(r),
                 "created_at": str(r["created_at"]),
                 "updated_at": str(r["updated_at"])}
                for r in rows
            ]


@app.get("/volcanes")
def listar_volcanes():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM core.volcanes ORDER BY nombre")
            return [dict(r) for r in cur.fetchall()]


@app.get("/health")
def health():
    try:
        get_conn().close()
        return {"status": "ok", "db": "up"}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503,
                            content={"status": "error", "detail": str(e)})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
