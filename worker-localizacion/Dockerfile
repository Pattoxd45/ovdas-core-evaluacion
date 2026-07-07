FROM python:3.11-slim

# Dependencias del sistema (libgfortran para hyposat ELF Fortran)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Worker
COPY workerlock.py .

# ── HYPOSAT: binario + archivos estáticos ──────────────────────
# hypo/ contiene: binario hyposat, tablas de tiempos de viaje,
# hyposat-parameter, stations.dat y modelos de velocidad.
COPY hypo/ /app/hypo/
RUN chmod +x /app/hypo/hyposat

# Directorio para outputs JSON
RUN mkdir -p /app/out

ENV PYTHONUNBUFFERED=1

CMD ["python", "workerlock.py"]
