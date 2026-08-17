"""
evaluar_detectores.py
======================
Evaluación comparativa de detectores de eventos sismo-volcánicos:
STA/LTA (ovdas-core) vs. PhaseNet vs. EQTransformer (ambos vía SeisBench),
sobre la traza continua de 10h de NVChVC con catálogo de referencia experto.

Métricas calculadas (consistentes con 03_metodologia.tex, sección
"Diseño experimental de la evaluación comparativa de detectores"):
    - Tasa de detección (recall)
    - Tasa de falsos positivos
    - Error temporal de picking (fases P y S, en segundos)

CORRE EN CPU. No requiere GPU.

────────────────────────────────────────────────────────────────
QUÉ NECESITAS HACER TÚ ANTES DE CORRER ESTO (no lo puedo hacer yo:
Zenodo no está accesible desde mi entorno de este chat):

1. Descargar "NVCh_10h_continuous_trace.zip" desde
   https://doi.org/10.5281/zenodo.17163020 (o el DOI v4 exacto que
   tengas en datos_tesis.tex) y descomprimirlo.

2. Revisar qué hay adentro y ajustar la sección CONFIG más abajo:
   - Si la traza viene como .mseed  -> usar cargar_traza_mseed()
   - Si viene como .npy (8 canales) -> usar cargar_traza_npy()
     (el propio Zenodo describe el formato como array de 8 filas,
     una por estación, sin encabezado de tiempo absoluto — vas a
     necesitar la hora de inicio de la traza, revisa si viene en
     algún metadata.json o similar dentro del zip)

3. Abrir el .csv de eventos de referencia y decirme (o ajustar tú
   mismo) los nombres reales de columnas en CATALOGO_COLUMNAS.
   Ahora mismo asumo columnas: estacion, t_inicio, t_fin, t_p, t_s, clase
   (t_p / t_s son opcionales — si no existen, el matching de fase
   cae automáticamente a comparar solo contra t_inicio)

4. pip install seisbench obspy  (con --break-system-packages si usas
   el mismo entorno de sistema que para ovdas-core)

5. La primera vez que corras PhaseNet/EQTransformer, SeisBench va a
   descargar los pesos preentrenados de internet (necesitas conexión,
   no lo puedo hacer yo desde acá). Quedan cacheados localmente después.

6. El STA/LTA de ovdas-core (cesvec.detection()) NO lo reimplemento
   acá a propósito — es el algoritmo que ya tienen corriendo, así que
   lo más correcto metodológicamente es usar exactamente esa función
   y solo pasar sus resultados a evaluar_picks() de este script (ver
   la función cargar_picks_stalta_ovdas_core() más abajo, es un
   placeholder que tienes que completar con la salida real). Si
   prefieres un STA/LTA "genérico" de referencia en vez del de
   ovdas-core, dejé classic_sta_lta de ObsPy como alternativa comentada.
────────────────────────────────────────────────────────────────
"""

import numpy as np
import pandas as pd
from obspy import read, Stream, Trace, UTCDateTime
from obspy.signal.trigger import classic_sta_lta, trigger_onset

# ══════════════════════════════════════════════════════════════
# CONFIG — AJUSTAR ANTES DE CORRER
# ══════════════════════════════════════════════════════════════

RUTA_TRAZA_CONTINUA = "./data/NVCh_10h_continuous_trace/traza.mseed"  # AJUSTAR
RUTA_CATALOGO_CSV = "./data/NVCh_10h_continuous_trace/catalogo.csv"  # AJUSTAR

# Mapeo de nombres de columna reales del catálogo -> nombres internos
# que usa este script. Ajusta los VALORES (lado derecho) a como se
# llamen de verdad las columnas en tu CSV.
CATALOGO_COLUMNAS = {
    "estacion": "estacion",
    "t_inicio": "t_inicio",
    "t_fin": "t_fin",
    "t_p": "t_p",       # poner None si el catálogo no trae fase P explícita
    "t_s": "t_s",       # poner None si el catálogo no trae fase S explícita
    "clase": "clase",
}

# Tolerancias de emparejamiento (segundos) — se reportan todas para
# poder comparar contra distintos criterios de la literatura
# (p.ej. Jiang et al. 2021 usa ventanas de este orden)
TOLERANCIAS_S = [0.5, 1.0, 2.0]

DEVICE = "cpu"  # inferencia en CPU, según restricción de hardware


# ══════════════════════════════════════════════════════════════
# 1. CARGA DE DATOS
# ══════════════════════════════════════════════════════════════

def cargar_traza_mseed(ruta: str) -> Stream:
    """Traza continua en formato MiniSEED estándar (ObsPy lee directo)."""
    return read(ruta)


def cargar_traza_npy(ruta_npy: str, t_inicio_iso: str, fs: float = 100.0,
                      nombres_estaciones=None) -> Stream:
    """
    Traza continua en formato .npy (n_canales x n_muestras), como la
    describe el dataset de Zenodo (8 filas = 8 estaciones).

    t_inicio_iso: hora de inicio de la traza en formato ISO
    (ej. "2022-01-01T00:00:00"). Confirma este valor en la
    documentación/metadata que venga con el .npy — no lo puedo
    inferir yo sin ver el archivo.
    """
    data = np.load(ruta_npy)
    if nombres_estaciones is None:
        nombres_estaciones = [f"EST{i+1}" for i in range(data.shape[0])]

    st = Stream()
    for i, nombre in enumerate(nombres_estaciones):
        tr = Trace(data=data[i].astype(np.float32))
        tr.stats.sampling_rate = fs
        tr.stats.starttime = UTCDateTime(t_inicio_iso)
        tr.stats.station = nombre
        tr.stats.channel = "HHZ"
        st += tr
    return st


def cargar_catalogo(ruta_csv: str) -> pd.DataFrame:
    """
    Carga el catálogo experto y lo normaliza a columnas internas:
    estacion, t_inicio, t_fin, t_p, t_s, clase (UTCDateTime en las
    columnas de tiempo).
    """
    df_raw = pd.read_csv(ruta_csv)
    df = pd.DataFrame()

    for col_interna, col_real in CATALOGO_COLUMNAS.items():
        if col_real is None or col_real not in df_raw.columns:
            df[col_interna] = None
        else:
            df[col_interna] = df_raw[col_real]

    for col_tiempo in ["t_inicio", "t_fin", "t_p", "t_s"]:
        df[col_tiempo] = df[col_tiempo].apply(
            lambda x: UTCDateTime(x) if pd.notna(x) else None
        )
    return df


# ══════════════════════════════════════════════════════════════
# 2. DETECTORES
# ══════════════════════════════════════════════════════════════

def run_stalta_generico(stream: Stream, sta_s=1.0, lta_s=10.0,
                         umbral_on=3.5, umbral_off=1.0) -> list[dict]:
    """
    STA/LTA GENÉRICO de referencia (ObsPy classic_sta_lta), NO es el
    de ovdas-core. Útil solo si quieres un baseline "de libro" además
    del que ya corre en el sistema. sta_s/lta_s en segundos.
    """
    picks = []
    for tr in stream:
        fs = tr.stats.sampling_rate
        cft = classic_sta_lta(tr.data, int(sta_s * fs), int(lta_s * fs))
        onsets = trigger_onset(cft, umbral_on, umbral_off)
        for on, off in onsets:
            picks.append({
                "estacion": tr.stats.station,
                "t_inicio": tr.stats.starttime + on / fs,
                "t_fin": tr.stats.starttime + off / fs,
                "t_p": None,
                "t_s": None,
                "fuente": "STA/LTA_generico",
            })
    return picks


def cargar_picks_stalta_ovdas_core(salida_cesvec_detection) -> list[dict]:
    """
    PLACEHOLDER — completar con la salida real de cesvec.detection()
    corrida sobre esta misma traza continua en tu entorno ovdas-core.

    cesvec.detection(filtered_data2, reduced_data, UMBRAL) devuelve
    (según worker.py) un arreglo `ev` donde cada fila trae índices de
    inicio/fin de muestra por evento. Conviértelos a UTCDateTime
    usando la starttime + fs de la traza, y arma la misma estructura
    de diccionario que usan las otras funciones de este archivo:
    {"estacion":, "t_inicio":, "t_fin":, "t_p": None, "t_s": None, "fuente": "STA/LTA_ovdas"}
    """
    raise NotImplementedError(
        "Completar con la salida real de cesvec.detection() sobre la traza continua."
    )


def run_phasenet(stream: Stream, weights="original") -> list[dict]:
    import seisbench.models as sbm

    model = sbm.PhaseNet.from_pretrained(weights)
    model.to(DEVICE)
    output = model.classify(stream)

    picks = []
    for p in output.picks:
        picks.append({
            "estacion": p.trace_id,
            "t_p": p.peak_time if p.phase == "P" else None,
            "t_s": p.peak_time if p.phase == "S" else None,
            "t_inicio": p.start_time,
            "t_fin": p.end_time,
            "fuente": "PhaseNet",
        })
    return picks


def run_eqtransformer(stream: Stream, weights="original") -> list[dict]:
    import seisbench.models as sbm

    model = sbm.EQTransformer.from_pretrained(weights)
    model.to(DEVICE)
    output = model.classify(stream)

    picks = []
    for p in output.picks:
        picks.append({
            "estacion": p.trace_id,
            "t_p": p.peak_time if p.phase == "P" else None,
            "t_s": p.peak_time if p.phase == "S" else None,
            "t_inicio": p.start_time,
            "t_fin": p.end_time,
            "fuente": "EQTransformer",
        })
    return picks


# ══════════════════════════════════════════════════════════════
# 3. MÉTRICAS
# ══════════════════════════════════════════════════════════════

def _mejor_match(t_ref: UTCDateTime, tiempos_detectados: list, tolerancia_s: float):
    """Encuentra la detección más cercana a t_ref dentro de la tolerancia."""
    candidatos = [t for t in tiempos_detectados if t is not None]
    if not candidatos or t_ref is None:
        return None
    diffs = [(abs(t - t_ref), t) for t in candidatos]
    diffs.sort(key=lambda x: x[0])
    mejor_diff, mejor_t = diffs[0]
    return mejor_t if mejor_diff <= tolerancia_s else None


def evaluar_picks(picks: list[dict], catalogo: pd.DataFrame,
                   tolerancia_s: float) -> dict:
    """
    Calcula recall, tasa de falsos positivos y error temporal medio
    (P y S por separado) de un conjunto de picks contra el catálogo,
    para una tolerancia de emparejamiento dada.
    """
    df_picks = pd.DataFrame(picks)

    resultados = {"tolerancia_s": tolerancia_s}
    n_catalogo = len(catalogo)
    detectados = 0
    errores_p, errores_s, errores_inicio = [], [], []

    for _, ev in catalogo.iterrows():
        match_p = match_s = match_inicio = None

        if ev["t_p"] is not None and "t_p" in df_picks.columns:
            match_p = _mejor_match(ev["t_p"], df_picks["t_p"].tolist(), tolerancia_s)
        if ev["t_s"] is not None and "t_s" in df_picks.columns:
            match_s = _mejor_match(ev["t_s"], df_picks["t_s"].tolist(), tolerancia_s)
        if "t_inicio" in df_picks.columns:
            match_inicio = _mejor_match(ev["t_inicio"], df_picks["t_inicio"].tolist(), tolerancia_s)

        if match_p is not None:
            errores_p.append(match_p - ev["t_p"])
        if match_s is not None:
            errores_s.append(match_s - ev["t_s"])
        if match_inicio is not None:
            errores_inicio.append(match_inicio - ev["t_inicio"])

        if match_p is not None or match_s is not None or match_inicio is not None:
            detectados += 1

    n_picks = len(df_picks)
    falsos_positivos = max(n_picks - detectados, 0)

    resultados["recall"] = detectados / n_catalogo if n_catalogo else float("nan")
    resultados["tasa_falsos_positivos"] = (
        falsos_positivos / n_picks if n_picks else float("nan")
    )
    resultados["error_medio_P_s"] = float(np.mean(np.abs(errores_p))) if errores_p else None
    resultados["error_medio_S_s"] = float(np.mean(np.abs(errores_s))) if errores_s else None
    resultados["error_medio_inicio_s"] = (
        float(np.mean(np.abs(errores_inicio))) if errores_inicio else None
    )
    resultados["n_eventos_catalogo"] = n_catalogo
    resultados["n_picks_detector"] = n_picks
    resultados["n_detectados"] = detectados
    return resultados


# ══════════════════════════════════════════════════════════════
# 4. ORQUESTACIÓN
# ══════════════════════════════════════════════════════════════

def main():
    print("Cargando traza continua...")
    stream = cargar_traza_mseed(RUTA_TRAZA_CONTINUA)
    # stream = cargar_traza_npy(RUTA_TRAZA_NPY, t_inicio_iso="AJUSTAR")

    print("Cargando catálogo de referencia...")
    catalogo = cargar_catalogo(RUTA_CATALOGO_CSV)
    print(f"  {len(catalogo)} eventos de referencia")

    detectores = {}

    print("\nCorriendo PhaseNet...")
    detectores["PhaseNet"] = run_phasenet(stream)

    print("Corriendo EQTransformer...")
    detectores["EQTransformer"] = run_eqtransformer(stream)

    # print("Corriendo STA/LTA genérico...")
    # detectores["STA/LTA_generico"] = run_stalta_generico(stream)

    # detectores["STA/LTA_ovdas"] = cargar_picks_stalta_ovdas_core(...)

    filas = []
    for nombre, picks in detectores.items():
        for tol in TOLERANCIAS_S:
            m = evaluar_picks(picks, catalogo, tol)
            m["detector"] = nombre
            filas.append(m)

    tabla = pd.DataFrame(filas)
    cols = ["detector", "tolerancia_s", "recall", "tasa_falsos_positivos",
            "error_medio_P_s", "error_medio_S_s", "error_medio_inicio_s",
            "n_eventos_catalogo", "n_picks_detector", "n_detectados"]
    tabla = tabla[cols]

    print("\n=== Resultados comparativos ===")
    print(tabla.to_string(index=False))
    tabla.to_csv("resultados_comparacion_detectores.csv", index=False)
    print("\nGuardado en resultados_comparacion_detectores.csv")


if __name__ == "__main__":
    main()
