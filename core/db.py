import psycopg2
import psycopg2.extras
import logging
from config import POSTGRES_DSN

log = logging.getLogger("core.db")


def get_conn():
    return psycopg2.connect(POSTGRES_DSN)


def init_db():
    """Verifica conexión a la BD al arrancar."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        log.info("PostgreSQL conectado OK")
    except Exception as e:
        log.error("Error conectando PostgreSQL: %s", e)
        raise


def crear_evento(
    evento_id: str, volcan_id: str, estacion_id: str, traza_metadata: dict
) -> None:
    sql = """
        INSERT INTO core.pipeline_eventos
            (evento_id, volcan_id, estacion_id, estado, traza_metadata)
        VALUES (%s, %s, %s, 'INGRESADO', %s)
        ON CONFLICT (evento_id) DO NOTHING
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    evento_id,
                    volcan_id,
                    estacion_id,
                    psycopg2.extras.Json(traza_metadata),
                ),
            )


def actualizar_estado(evento_id: str, estado: str, error_msg: str = None) -> None:
    sql = """
        UPDATE core.pipeline_eventos
           SET estado = %s, error_msg = %s
         WHERE evento_id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (estado, error_msg, evento_id))


def get_evento(evento_id: str) -> dict | None:
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
            return dict(row) if row else None


def listar_eventos(limite: int = 50) -> list[dict]:
    sql = """
        SELECT evento_id, volcan_id, estacion_id, estado, created_at, updated_at
          FROM core.pipeline_eventos
         ORDER BY created_at DESC
         LIMIT %s
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (limite,))
            return [dict(r) for r in cur.fetchall()]


def actualizar_n_estaciones(evento_id: str, n: int) -> None:
    """Actualiza n_estaciones en traza_metadata (usado cuando una estación es silenciosa)."""
    sql = """
        UPDATE core.pipeline_eventos
           SET traza_metadata = jsonb_set(
                   COALESCE(traza_metadata, '{}'::jsonb),
                   '{n_estaciones}',
                   %s::jsonb
               )
         WHERE evento_id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (str(n), evento_id))


def buscar_evento_por_grupo(grupo_id: str) -> dict | None:
    """Busca un evento existente por su grupo_id (almacenado en traza_metadata)."""
    sql = """
        SELECT evento_id, estado
          FROM core.pipeline_eventos
         WHERE traza_metadata->>'grupo_id' = %s
         LIMIT 1
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (grupo_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def contar_picks(evento_id: str) -> int:
    """Cuenta picks válidos (t_p > 0) en picking.resultados para el evento."""
    sql = "SELECT COUNT(*) FROM picking.resultados WHERE evento_id = %s AND t_p > 0"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (evento_id,))
            return cur.fetchone()[0]


def validar_estacion(estacion_id: str, volcan_id: str) -> bool:
    """Valida contra el catálogo local (SSOT)."""
    sql = """
        SELECT 1 FROM core.estaciones
         WHERE estacion_id = %s AND volcan_id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (estacion_id, volcan_id))
            return cur.fetchone() is not None
