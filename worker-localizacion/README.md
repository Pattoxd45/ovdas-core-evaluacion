# Worker Localización (HYPOSAT)

Tercer y último worker del pipeline sísmico. Determina el **hipocenter** del evento
(latitud, longitud, profundidad) usando el software de localización **HYPOSAT**,
a partir de los tiempos de arribo P y S calculados por el worker-picking.

Portado desde `Localization/model/dataservers.py` y `post_pro_hyposat.py`.

---

## Posición en el pipeline

```mermaid
flowchart LR
    WP[worker-picking] -->|PICKING_COMPLETADO| CORE[core\norchestrador]
    CORE -->|n_picks == n_estaciones| LOC[worker-localizacion]
    LOC -->|LOCALIZACION_COMPLETADA| CORE
    CORE --> DONE([COMPLETADO])
```

El core lanza la localización **solo cuando todos los picks esperados han llegado**
(controlado por `n_estaciones` en el metadata del evento). Con una sola estación,
`n_estaciones=1` y se lanza inmediatamente; con 4 estaciones, espera los 4 picks.

---

## Entradas

| Fuente | Descripción |
|---|---|
| **RabbitMQ** `ovdas.localizacion` | Tarea con solo `evento_id` |
| **PostgreSQL** `picking.resultados` | Picks t_p / t_s por estación |

### Mensaje de entrada (JSON)

```json
{
  "evento_id": "99-20260307-a3f9c2"
}
```

### Picks consultados desde DB

```sql
SELECT estacion, componente, t_p, t_s, amplitud, freq_dom
FROM   picking.resultados
WHERE  evento_id = ? AND t_p > 0
ORDER  BY created_at
```

---

## Pipeline de procesamiento

```mermaid
flowchart TD
    IN[Mensaje RabbitMQ] --> PICKS[Consulta picking.resultados\nt_p y t_s por estación]
    PICKS --> N{n_picks >= MIN_PICKS?}
    N -->|no| EMPTY[Sin solución\nn_picks insuficientes]
    N -->|sí| WORKDIR[Crear directorio temporal\n/tmp/hypo_{evento_id}_XXXX/]
    WORKDIR --> COPY[Copiar archivos estáticos:\nhyposat-parameter\nstations.dat\nmodel_Chillan_Cardona.dat]
    COPY --> HYIN[Generar hyposat-in\nFases P y S por estación]
    HYIN --> RUN[Ejecutar binario HYPOSAT\ntimeout=120s]
    RUN --> CHECK{hyposat-out\n> 500 bytes?}
    CHECK -->|no| NOSOL[Sin solución válida]
    CHECK -->|sí| PARSE[Parsear hyposat-out\nlat, lon, z, rmse, gap, ml, ejes]
    PARSE --> RAWOUT[Guardar hyposat-out raw\n/app/out/hyposat-out_{evento_id}.txt]
    RAWOUT --> CLEANUP[Eliminar directorio temporal]
    NOSOL --> CLEANUP
    EMPTY --> RESULT
    CLEANUP --> RESULT[Construir resultado]
    RESULT --> JSON[Guardar JSON\n/app/out/localizacion_{evento_id}.json]
    JSON --> DB[INSERT localizacion.resultados]
    DB --> CB[Publicar LOCALIZACION_COMPLETADA\novdas.callbacks]
    CB --> ACK[basic_ack]
```

---

## Formato hyposat-in

HYPOSAT requiere un archivo de texto con las fases sísmicas en formato fijo:

```
Localizacion automatica OVDAS-Microservicios
*23456789 123456789 ...
CHSZ P    2026 03 07 03 28 02  0.150  -1.00 -1.00 -1.00 -1.00 T____
CHSZ S    2026 03 07 03 28 07  0.500  -1.00 -1.00 -1.00 -1.00 T____M  0.207  853.84
FU2Z P    2026 03 07 03 28 04  0.150  -1.00 -1.00 -1.00 -1.00 T____
FU2Z S    2026 03 07 03 28 09  0.500  -1.00 -1.00 -1.00 -1.00 T____M  0.207  721.30
```

### Código de estación HYPOSAT

La función `_codigo_estacion_hyposat()` construye el código de 4 chars para `stations.dat`:

```
estacion[:3].upper() + "Z"
CHS  → CHSZ
FU2  → FU2Z
NBL  → NBLZ
PLA  → PLAZ
```

> Asegurarse de que el código generado exista en `hypo/stations.dat`.

---

## Parseo de hyposat-out

El archivo de salida de HYPOSAT contiene varias secciones. Se extraen:

| Campo en hyposat-out | Variable | Descripción |
|---|---|---|
| `T0 LAT LON Z` | `tiempo_origen`, `lat`, `lon`, `z_m` | Hipocenter principal |
| `data[14]` | `rmse` | RMS de residuos (segundos) |
| `data[13]` | `n_fases` | Número de fases usadas |
| `data[10]` | `dz` | Incerteza vertical |
| `Maximum azimuthal gap` | `gap` | Gap azimutal (°) |
| `Magnitude:` | `ml` | Magnitud local |
| `Major half axis:` | `major_half_axes` | Semi-eje mayor del elipsoide de error (km) |
| `Minor half axis:` | `minor_half_axes` | Semi-eje menor del elipsoide de error (km) |

**Conversión de profundidad:**
```
z_m = 3200 - int(z_hyposat_km × 1000)
```
(Referencia al nivel de la superficie del volcán, no al nivel del mar)

---

## Salidas

### 1. Base de datos — `localizacion.resultados`

| Columna | Tipo | Descripción |
|---|---|---|
| `evento_id` | VARCHAR(25) | ID del pipeline |
| `tiempo_origen` | VARCHAR(30) | ISO-8601 del origen estimado |
| `lat` | FLOAT | Latitud (°, negativo = sur) |
| `lon` | FLOAT | Longitud (°, negativo = oeste) |
| `z_m` | INT | Profundidad en metros (ref. superficie) |
| `rmse` | FLOAT | RMS residual en segundos |
| `major_half_axes` | FLOAT | Semi-eje mayor del error (km) |
| `minor_half_axes` | FLOAT | Semi-eje menor del error (km) |
| `dz` | INT | Incerteza vertical (m) |
| `gap` | FLOAT | Gap azimutal (°) |
| `ml` | FLOAT | Magnitud local |
| `n_fases` | INT | Número de fases usadas por HYPOSAT |
| `n_picks_usados` | INT | Picks de picking.resultados consultados |
| `autor` | VARCHAR(30) | `"hyposat_auto"` |

### 2. JSON de auditoría — `/app/out/localizacion_{evento_id}.json`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "n_picks_usados": 4,
  "hyposat_exitoso": true,
  "tiempo_origen": "2026-03-07T03:28:01.5Z",
  "lat": -36.872,
  "lon": -71.361,
  "z_m": 2800,
  "rmse": 0.08,
  "gap": 142.5,
  "ml": 0.9,
  "major_half_axes": 2.1,
  "minor_half_axes": 1.4,
  "n_fases": 8,
  "autor": "hyposat_auto",
  "procesado_en": "2026-03-07T03:28:29.1"
}
```

### 3. Raw de HYPOSAT — `/app/out/hyposat-out_{evento_id}.txt`

Output crudo del binario HYPOSAT para auditoría completa.

### 4. Callback RabbitMQ — `ovdas.callbacks`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "tipo": "LOCALIZACION_COMPLETADA",
  "payload": {
    "lat": -36.872,
    "lon": -71.361,
    "z_m": 2800,
    "rmse": 0.08,
    "ml": 0.9,
    "gap": 142.5,
    "n_fases": 8,
    "hyposat_exitoso": true
  }
}
```

---

## Archivos estáticos (hypo/)

| Archivo | Descripción |
|---|---|
| `hyposat` | Binario ELF x86-64 (Fortran compilado) |
| `hyposat-parameter` | Configuración de la corrida (modelo de velocidad, iteraciones) |
| `stations.dat` | Catálogo de estaciones en coordenadas sexagesimales |
| `model_Chillan_Cardona.dat` | Modelo de velocidad local para Nevados de Chillán |
| `model_Chillan.dat` | Modelo alternativo (Chillán) |
| `ak135_A.{hed,tbl}` | Tablas de tiempos de viaje globales AK135 |
| `iasp91_A.{hed,tbl}` | Tablas de tiempos de viaje globales IASP91 |
| `prem_A.{hed,tbl}` | Tablas PREM |
| `MLCORR.TABLE` | Correcciones de magnitud local |

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `RABBITMQ_URL` | `amqp://ovdas:ovdas@rabbitmq:5672/` | — |
| `POSTGRES_DSN` | `host=postgres ...` | — |
| `OUT_DIR` | `/app/out` | Directorio para JSON y raw HYPOSAT |
| `HYPO_DIR` | `/app/hypo` | Binario + archivos estáticos de HYPOSAT |
| `P_STD` | `0.15` | Incerteza estándar de la fase P (segundos) |
| `S_STD` | `0.50` | Incerteza estándar de la fase S (segundos) |
| `FLAGS_P` | `T____` | Flags de peso para fase P en hyposat-in |
| `FLAGS_S` | `T____M` | Flags de peso para fase S en hyposat-in |
| `FREQ_JUNK` | `2.4` | Frecuencia por defecto cuando freq_dom=0 (Hz) |
| `MIN_PICKS` | `1` | Mínimo de fases P para intentar localizar |

---

## Dependencias Python

```
pika==1.3.2
psycopg2-binary==2.9.9
```

No requiere ObsPy ni SciPy: trabaja con los timestamps ya calculados por picking.

---

## Limitaciones con pocas estaciones

| Estaciones | Resultado esperado |
|---|---|
| 1 (solo P) | Sin solución (ni distancia ni azimut) |
| 1 (P + S) | Distancia estimada, azimut desconocido |
| 2 (P + S c/u) | Localización aproximada, profundidad incierta |
| 3+ | Hipocenter real con buena determinación de profundidad |

Para producción se recomienda al menos 3 estaciones.

---

## Estaciones en stations.dat (Chillán)

| Código HYPOSAT | Estación | Lat | Lon |
|---|---|---|---|
| CHSZ | CHS | -36°52'12.7"S | -71°20'52.8"W |
| FU2Z | FU2 | -36°54'44.1"S | -71°21'46.5"W |
| NBLZ | NBL | -36°51'10.8"S | -71°21'03.6"W |
| PLAZ | PLA | -36°50'01.0"S | -71°27'33.5"W |
| FREZ | FRE | -36°52'08.0"S | -71°23'42.7"W |
| SHGZ | SHG | -36°53'22.7"S | -71°23'06.1"W |
| PTZZ | PTZ | -36°51'00.0"S | -71°22'26.4"W |
| PHIZ | PHI | -36°52'37.2"S | -71°21'57.6"W |
| ROBZ | ROB | -36°47'53.8"S | -71°13'43.8"W |
