-- Migración: amplía estacion_id / estacion de VARCHAR(3) a VARCHAR(10)
-- Necesario para soportar códigos de estación de 4+ caracteres (ej: EBOZ, INVZ).
-- Ejecutar en deployments existentes donde init.sql ya fue aplicado.

ALTER TABLE core.pipeline_eventos  ALTER COLUMN estacion_id  TYPE VARCHAR(10);
ALTER TABLE core.estaciones        ALTER COLUMN estacion_id  TYPE VARCHAR(10);
ALTER TABLE deteccion.resultados   ALTER COLUMN estacion     TYPE VARCHAR(10);
ALTER TABLE picking.resultados     ALTER COLUMN estacion     TYPE VARCHAR(10);
