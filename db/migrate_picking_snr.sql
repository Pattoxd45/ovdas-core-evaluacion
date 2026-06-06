-- Migración: agrega columna snr a picking.resultados
-- Ejecutar solo en deployments existentes donde init.sql ya fue aplicado.
-- En deployments nuevos (docker compose down -v && up --build) no es necesario.

ALTER TABLE picking.resultados
    ADD COLUMN IF NOT EXISTS snr FLOAT;

COMMENT ON COLUMN picking.resultados.snr IS 'SNR en dB — espejo de avistamiento_registro.snr (legacy)';
