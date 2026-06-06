-- Migración: crea el schema y tabla localizacion.resultados
-- Ejecutar en deployments existentes donde init.sql ya fue aplicado.

CREATE SCHEMA IF NOT EXISTS localizacion;

CREATE TABLE IF NOT EXISTS localizacion.resultados (
    id              SERIAL           PRIMARY KEY,
    evento_id       VARCHAR(25)      NOT NULL,
    tiempo_origen   VARCHAR(30),
    lat             FLOAT,
    lon             FLOAT,
    z_m             INT,
    rmse            FLOAT,
    major_half_axes FLOAT,
    minor_half_axes FLOAT,
    dz              INT,
    gap             FLOAT,
    ml              FLOAT,
    n_fases         INT,
    n_picks_usados  INT          DEFAULT 0,
    autor           VARCHAR(30)  DEFAULT 'hyposat_auto',
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loc_evento ON localizacion.resultados(evento_id);
