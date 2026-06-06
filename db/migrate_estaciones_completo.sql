-- Migración: agrega estaciones reales desde el catálogo legacy (ufro_ovdas_v3.estacion)
-- Ejecutar en deployments existentes donde init.sql ya fue aplicado.

INSERT INTO core.estaciones
    (estacion_id, nombre, volcan_id, latitud, longitud, altura, prioridad, distancia_crater, calibracion)
VALUES
    -- Nevados de Chillán (99)
    ('FU2', 'Fumarola 2', '99', '-36.90941',   '-71.34585',  2599, 0, 5.50, 0.002139),
    ('FRE', 'Fresco',     '99', '-36.868931',  '-71.395247', 2630, 0, 1.58, 0.001985),
    ('LBS', 'Los Baños',  '99', '-36.85840',   '-71.38797',  2658, 0, 1.32, 0.000403),
    ('NBL', 'Ñuble',      '99', '-36.85258',   '-71.35126',  2304, 0, 2.87, 0.002141),
    ('PHI', 'Phillipi',   '99', '-36.87703',   '-71.36652',  3125, 0, 1.50, 0.000313),
    ('PLA', 'Plan',       '99', '-36.83365',   '-71.45923',  2080, 0, 8.15, 0.001382),
    ('PTZ', 'Portezuelo', '99', '-36.85517',   '-71.37444',  2590, 0, 1.36, 0.00031767),
    ('ROB', 'Roble',      '99', '-36.79828',   '-71.22884',  1880, 0, 15.33,0.002143),
    ('SHG', 'Shangrila',  '99', '-36.884165',  '-71.38495',  2640, 0, 2.00, 0.001843),
    -- Llaima (LL)
    ('PA2', 'Paile',      'LL', '-38.695923',  '-71.730114', 3125, 0, 2.30, 0.0)
ON CONFLICT (estacion_id) DO UPDATE
    SET nombre           = EXCLUDED.nombre,
        latitud          = EXCLUDED.latitud,
        longitud         = EXCLUDED.longitud,
        altura           = EXCLUDED.altura,
        distancia_crater = EXCLUDED.distancia_crater,
        calibracion      = EXCLUDED.calibracion;

-- Corrige distancia_crater de CHS (legacy: 2.68, init.sql tenía 2.66)
UPDATE core.estaciones SET distancia_crater = 2.68 WHERE estacion_id = 'CHS';
