-- ============================================================
-- OVDAS Microservicios - Inicialización de Base de Datos
-- BD: ovdas  |  Patrón: Database-per-Service (schemas separados)
-- ============================================================

-- -------------------------------------------------------
-- SCHEMA: core  (Núcleo Orquestador - pipeline state)
-- -------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE core.pipeline_eventos (
    evento_id       VARCHAR(25)  PRIMARY KEY,
    volcan_id       VARCHAR(3)   NOT NULL,
    estacion_id     VARCHAR(10)  NOT NULL,
    -- Estados posibles: INGRESADO | DETECTANDO | PICKING |
    --                   LOCALIZANDO | CLASIFICANDO | COMPLETADO | ERROR
    estado          VARCHAR(30)  NOT NULL DEFAULT 'INGRESADO',
    traza_metadata  JSONB,
    error_msg       TEXT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pipeline_estado  ON core.pipeline_eventos(estado);
CREATE INDEX idx_pipeline_volcan  ON core.pipeline_eventos(volcan_id);
CREATE INDEX idx_pipeline_created ON core.pipeline_eventos(created_at DESC);

-- Catálogo de volcanes (SSOT - solo lectura para workers)
CREATE TABLE core.volcanes (
    volcan_id   VARCHAR(3)  PRIMARY KEY,
    nombre      VARCHAR(45) NOT NULL,
    latitud     VARCHAR(45),
    longitud    VARCHAR(45) NOT NULL,
    altura      VARCHAR(45) NOT NULL
);

INSERT INTO core.volcanes (volcan_id, nombre, latitud, longitud, altura) VALUES
    ('VV', 'Villarrica',         '-39.420254', '-71.940728', '2847'),
    ('LL', 'Llaima',             '-38.6920',   '-71.7290',   '3125'),
    ('99', 'Nevados de Chillán', '-36.874108', '-71.370274', '3212');

-- Catálogo de estaciones (SSOT - solo lectura para workers)
CREATE TABLE core.estaciones (
    estacion_id     VARCHAR(10) PRIMARY KEY,
    nombre          VARCHAR(45) NOT NULL,
    volcan_id       VARCHAR(3)  REFERENCES core.volcanes(volcan_id),
    latitud         VARCHAR(45),
    longitud        VARCHAR(45),
    altura          INT,
    prioridad       INT         DEFAULT 1,
    distancia_crater FLOAT,
    calibracion     FLOAT       DEFAULT 1.0
);

INSERT INTO core.estaciones
    (estacion_id, nombre, volcan_id, latitud, longitud, altura, prioridad, distancia_crater, calibracion)
VALUES
    -- ── Nevados de Chillán (99) ──────────────────────────────
    ('CHS', 'Chillán',    '99', '-36.87026',   '-71.34801',  2466, 0, 2.68, 0.0019865),
    ('FU2', 'Fumarola 2', '99', '-36.90941',   '-71.34585',  2599, 0, 5.50, 0.002139),
    ('FRE', 'Fresco',     '99', '-36.868931',  '-71.395247', 2630, 0, 1.58, 0.001985),
    ('LBS', 'Los Baños',  '99', '-36.85840',   '-71.38797',  2658, 0, 1.32, 0.000403),
    ('NBL', 'Ñuble',      '99', '-36.85258',   '-71.35126',  2304, 0, 2.87, 0.002141),
    ('PHI', 'Phillipi',   '99', '-36.87703',   '-71.36652',  3125, 0, 1.50, 0.000313),
    ('PLA', 'Plan',       '99', '-36.83365',   '-71.45923',  2080, 0, 8.15, 0.001382),
    ('PTZ', 'Portezuelo', '99', '-36.85517',   '-71.37444',  2590, 0, 1.36, 0.00031767),
    ('ROB', 'Roble',      '99', '-36.79828',   '-71.22884',  1880, 0, 15.33,0.002143),
    ('SHG', 'Shangrila',  '99', '-36.884165',  '-71.38495',  2640, 0, 2.00, 0.001843),
    -- ── Villarrica (VV) ─────────────────────────────────────
    ('VN2', 'Villarrica', 'VV', '-39.420254',  '-71.940728', 2466, 0, 2.30, 0.0),
    -- ── Llaima (LL) ─────────────────────────────────────────
    ('PA2', 'Paile',      'LL', '-38.695923',  '-71.730114', 3125, 0, 2.30, 0.0);

-- -------------------------------------------------------
-- SCHEMA: deteccion  (Worker ADOVE - identificación señal)
-- Espejo lógico de: identificacion_senal (legacy)
-- -------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS deteccion;

CREATE TABLE deteccion.resultados (
    id              SERIAL       PRIMARY KEY,
    evento_id       VARCHAR(25)  NOT NULL,
    estacion        VARCHAR(10)  NOT NULL,
    componente      CHAR(1)      NOT NULL DEFAULT 'Z',
    snr             FLOAT,
    label_event     VARCHAR(2),      -- VT, LP, TR, OT
    prob_vt         FLOAT,
    prob_lp         FLOAT,
    prob_tr         FLOAT,
    prob_ot         FLOAT,
    inicio          DOUBLE PRECISION,
    fin             DOUBLE PRECISION,
    largo           FLOAT,
    prom_ruido_fondo FLOAT,
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_det_evento   ON deteccion.resultados(evento_id);
CREATE INDEX idx_det_estacion ON deteccion.resultados(estacion);

-- -------------------------------------------------------
-- SCHEMA: picking  (Worker P-S - avistamiento ondas)
-- Espejo lógico de: avistamiento_registro (legacy)
-- -------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS picking;

CREATE TABLE picking.resultados (
    id              SERIAL       PRIMARY KEY,
    evento_id       VARCHAR(25)  NOT NULL,
    estacion        VARCHAR(10)  NOT NULL,
    componente      CHAR(1)      NOT NULL DEFAULT 'Z',
    t_p             DOUBLE PRECISION,     -- tiempo arribo onda P (unix)
    t_s             DOUBLE PRECISION,     -- tiempo arribo onda S (unix)
    snr             FLOAT,               -- SNR en dB (espejo de avistamiento_registro.snr)
    amplitud        FLOAT,
    polar           VARCHAR(2),
    freq_dom        FLOAT,               -- frecuencia dominante (Hz)
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pick_evento   ON picking.resultados(evento_id);
CREATE INDEX idx_pick_estacion ON picking.resultados(estacion);

-- -------------------------------------------------------
-- SCHEMA: localizacion  (Worker Localizacion - HYPOSAT)
-- Resultado de la localización sísmica: lat/lon/z/rmse/ml
-- -------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS localizacion;

CREATE TABLE localizacion.resultados (
    id              SERIAL           PRIMARY KEY,
    evento_id       VARCHAR(25)      NOT NULL,
    tiempo_origen   VARCHAR(30),                -- ISO-8601 del origen estimado
    lat             FLOAT,                      -- latitud (grados decimales, sur = negativo)
    lon             FLOAT,                      -- longitud (grados decimales)
    z_m             INT,                        -- profundidad en metros (referida a superficie)
    rmse            FLOAT,                      -- RMS residual (s)
    major_half_axes FLOAT,                      -- semi-eje mayor del elipsoide de error (km)
    minor_half_axes FLOAT,                      -- semi-eje menor del elipsoide de error (km)
    dz              INT,                        -- incerteza vertical (m)
    gap             FLOAT,                      -- azimuthal gap (°)
    ml              FLOAT,                      -- magnitud local
    n_fases         INT,                        -- número de fases usadas
    n_picks_usados  INT          DEFAULT 0,     -- picks de picking.resultados usados
    autor           VARCHAR(30)  DEFAULT 'hyposat_auto',
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_loc_evento ON localizacion.resultados(evento_id);

-- -------------------------------------------------------
-- Función para auto-actualizar updated_at en pipeline
-- -------------------------------------------------------
CREATE OR REPLACE FUNCTION core.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_pipeline_updated_at
    BEFORE UPDATE ON core.pipeline_eventos
    FOR EACH ROW EXECUTE FUNCTION core.touch_updated_at();

-- -------------------------------------------------------
-- SCHEMA: wws  (WWSPoller - tracking de descargas)
-- Registra cada consulta al servidor WWS y su resultado.
-- -------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS wws;

CREATE TABLE wws.descargas (
    id            SERIAL           PRIMARY KEY,
    estacion      VARCHAR(10)      NOT NULL,
    componente    CHAR(1)          NOT NULL DEFAULT 'Z',
    inicio_unix   DOUBLE PRECISION NOT NULL,   -- inicio de la ventana solicitada
    fin_unix      DOUBLE PRECISION NOT NULL,   -- fin de la ventana solicitada
    archivo       VARCHAR(255)     NOT NULL,   -- nombre del archivo en /shared/data/
    estado        VARCHAR(20)      NOT NULL DEFAULT 'PENDIENTE',
    -- PENDIENTE : archivo descargado, entrega al core pendiente
    -- INGRESADO : core aceptó (202) y arrancó el pipeline
    -- ERROR     : fallo en descarga o al notificar al core
    evento_id     VARCHAR(25),                 -- referencia a core.pipeline_eventos
    worker_resp   JSONB,                       -- respuesta JSON del core (o error)
    created_at    TIMESTAMP        NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP        NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_wws_estacion ON wws.descargas(estacion);
CREATE INDEX idx_wws_estado   ON wws.descargas(estado);
CREATE INDEX idx_wws_created  ON wws.descargas(created_at DESC);

-- Reutiliza la función genérica de core para el trigger updated_at
CREATE TRIGGER trg_wws_descargas_updated_at
    BEFORE UPDATE ON wws.descargas
    FOR EACH ROW EXECUTE FUNCTION core.touch_updated_at();
