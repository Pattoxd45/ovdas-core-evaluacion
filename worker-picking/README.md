# Worker Picking P-S

Segundo worker del pipeline sísmico. Implementa el **Castilla Picker**:
detección automática de los tiempos de arribo de las ondas sísmicas **P** y **S**,
y cálculo de parámetros de la señal (SNR, frecuencia dominante, amplitud).

Portado fielmente desde `Castilla_Picker.py` y `newPrecotev2.py` del sistema legacy.

---

## Entradas

| Fuente | Descripción |
|---|---|
| **RabbitMQ** `ovdas.picking` | Tarea con `evento_id`, `estacion_id`, `inicio_unix` |
| **PostgreSQL** `wws.descargas` | Nombre del archivo para el `evento_id` |
| **Volumen** `/shared/data/` | Archivo MiniSEED original |

### Mensaje de entrada (JSON)

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "estacion_id": "FU2",
  "componente": "Z",
  "inicio_unix": 1741305700.5,
  "snr": 12.4,
  "label_event": "VT"
}
```

> `inicio_unix` proviene del primer evento detectado en el worker-deteccion.

---

## Pipeline de procesamiento

```mermaid
flowchart TD
    IN[Mensaje RabbitMQ] --> ARCH[Consulta wws.descargas\narchivo = ? WHERE evento_id]
    ARCH --> READ[ObsPy: lee .mseed\nventana de 60s desde inicio_unix]
    READ --> RS{¿fs ≠ 100 Hz?}
    RS -->|sí| RES[resample a 100 Hz]
    RS -->|no| BP
    RES --> BP[Bandpass 0.9–12 Hz\nButterworth orden 6\n+ padding 500 muestras]
    BP --> NORM[Normalizar por amplitud máxima]
    NORM --> SHORT{¿traza < 700 muestras\n7 segundos?}
    SHORT -->|sí| SKIP[Devuelve t_p=0, t_s=0\nSolo calcula freq y amp]
    SHORT -->|no| WP[waveP: arribo onda P\nSTA/LTA de amplitud]
    WP --> WS[waveS: arribo onda S\nInflexión en espectrograma]
    WS --> SNR[Calcula SNR en dB\nseñal post-P vs ruido pre-P]
    SNR --> FREQ[frec_fun: FFT → frecuencia dominante]
    FREQ --> RES2[Resultado: t_p, t_s, snr, freq, amp]
    SKIP --> RES2
    RES2 --> JSON[Guardar JSON auditoría\n/app/out/picking_evento_id.json]
    JSON --> DB[INSERT picking.resultados]
    DB --> CB[Publicar PICKING_COMPLETADO\novdas.callbacks]
    CB --> ACK[basic_ack]
```

---

## Algoritmos

### Bandpass (0.9–12 Hz)

Filtro Butterworth de orden 6 aplicado con padding inverso de 500 muestras
para evitar artefactos de borde (ringing):

```
tmp = [flip(data[:500]) | data]
yf  = lfilter(b, a, tmp)[500:]
```

### waveP — Arribo onda P

Ratio de amplitud máxima en ventana larga vs ventana corta (variante STA/LTA):

```
ws = 400 muestras (4s),  wl = 100 muestras (1s)

stmltm[i] = max|y[i:i+ws]| / max(|y[i:i+wl]|, 0.05)
t_P = argmax(stmltm) + ws - wl
```

Retorna el índice de muestra del arribo P.

### waveS — Arribo onda S

Punto de inflexión máximo en la distribución acumulada de energía espectral:

```
1. Espectrograma: nperseg=300, noverlap=299 (1 muestra por bin de tiempo)
2. Normalizar Sxx: Sn = (S - min) / (max - min)
3. Umbral u = mean(Sn); máscara binaria B = Sn >= u
4. Energía espectral ponderada: SE = v * Ev (activación × energía)
5. CDF acumulada: p = cumsum(SE) / sum(SE)
6. Suavizar con gaussiana sigma=25, calcular segunda derivada
7. t_S = t[argmax(p'')]   (máxima curvatura = inflexión máxima)
```

### SNR

```
SNR (dB) = 10 × log10(|E_señal - E_ruido| / E_ruido)
E_señal = mean(y[iP+50:-100]²)
E_ruido = mean(y[101:iP-100]²)
```

### frec_fun — Frecuencia dominante

FFT sobre la señal filtrada completa. Retorna la frecuencia con máxima
potencia en el espectro de potencia unilateral.

---

## Salidas

### 1. Base de datos — `picking.resultados`

| Columna | Tipo | Descripción |
|---|---|---|
| `evento_id` | VARCHAR(25) | ID del pipeline |
| `estacion` | VARCHAR(10) | Código de estación (ej: `FU2`) |
| `componente` | CHAR(1) | Componente analizada (`Z`) |
| `t_p` | DOUBLE PRECISION | Timestamp Unix del arribo P |
| `t_s` | DOUBLE PRECISION | Timestamp Unix del arribo S |
| `snr` | FLOAT | SNR en dB |
| `amplitud` | FLOAT | Amplitud máxima de la señal filtrada |
| `polar` | VARCHAR(2) | Polaridad (siempre `null` con 1 componente) |
| `freq_dom` | FLOAT | Frecuencia dominante en Hz |

### 2. JSON de auditoría — `/app/out/picking_{evento_id}.json`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "estacion": "FU2",
  "componente": "Z",
  "t_p": 1741305702.49,
  "t_s": 1741305707.35,
  "snr": -4.45,
  "amplitud": 853.84057441,
  "polar": null,
  "freq_dom": 4.8333,
  "label_event": "VT",
  "archivo": "FU2_Z_20260307_030000.mseed",
  "procesado_en": "2026-03-07T03:28:28.548346"
}
```

> El JSON se guarda **antes** de la escritura en DB para garantizar auditoría
> incluso si la BD falla.

### 3. Callback RabbitMQ — `ovdas.callbacks`

```json
{
  "evento_id": "99-20260307-a3f9c2",
  "tipo": "PICKING_COMPLETADO",
  "payload": {
    "estacion": "FU2",
    "componente": "Z",
    "t_p": 1741305702.49,
    "t_s": 1741305707.35,
    "snr": -4.45,
    "freq_dom": 4.8333,
    "amplitud": 853.84
  }
}
```

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `RABBITMQ_URL` | `amqp://ovdas:ovdas@rabbitmq:5672/` | — |
| `POSTGRES_DSN` | `host=postgres ...` | — |
| `DATA_DIR` | `/shared/data` | Volumen compartido |
| `OUT_DIR` | `/app/out` | Directorio para JSON |

---

## Dependencias Python

```
pika==1.3.2
psycopg2-binary==2.9.9
obspy>=1.4.0
numpy>=1.24.0
scipy>=1.10.0
```

---

## Notas de implementación

### Conexión DB persistente

La conexión PostgreSQL se abre **una vez** al conectar RabbitMQ y se reutiliza
en todos los mensajes del mismo ciclo de vida, evitando el overhead de reconexión
por evento (crítico con 600+ eventos en cola).

### heartbeat=0

El espectrograma y la FFT bloquean el hilo IO de pika durante varios segundos
por evento. Con 600 eventos en cola esto supera cualquier heartbeat razonable.
`heartbeat=0` deshabilita el mecanismo, evitando que RabbitMQ cierre la conexión.

### Remuestreo

Si la traza no está a 100 Hz, se remuestrea automáticamente antes del picking,
ya que los parámetros de los algoritmos (ws=400, wl=100, nperseg=300) asumen
exactamente 100 Hz.

### Compatibilidad NumPy ≥ 1.25

`np.argwhere` retorna un array de shape `(N, 1)`. Para extraer el escalar
de forma robusta:
```python
ts = np.round(t[int(i[-1].flat[0])] * FS_TARGET)
```
`.flat[0]` funciona correctamente independiente de la forma del array.

### Origen de los algoritmos

| Función | Archivo legacy de origen |
|---|---|
| `_wave_p` | `Castilla_Picker.py` → `waveP()` |
| `_wave_s` | `Castilla_Picker.py` → `waveS()` |
| `_frec_fun` | `Castilla_Picker.py` → `frec_fun()` |
| `_butter_bandpass_lfilter` | `newPrecotev2.py` → filtro bandpass |
| `_picker` | Integración de las anteriores |
