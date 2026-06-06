FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY poller.py wws_client.py ./

# Archivos .mseed de referencia para el modo dummy (copiados físicamente en este directorio)
COPY templates /templates

ENV PYTHONUNBUFFERED=1
ENV WWS_HOST=wws.sernageomin.cl
ENV WWS_PORT=29384
ENV POLL_INTERVAL_SEC=300
ENV ESTACIONES=CHS:99
ENV COMPONENTE=Z
ENV CANAL=HHZ
ENV DURACION_SEG=3600
ENV MUESTRA_HZ=100
ENV SHARED_DATA_DIR=/shared/data
ENV TEMPLATES_DIR=/templates
ENV CORE_API_URL=http://core:8000

# /shared/data → volumen compartido con worker-deteccion (lectura/escritura)
VOLUME ["/shared/data"]

CMD ["python", "poller.py"]
