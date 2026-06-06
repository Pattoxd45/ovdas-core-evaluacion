FROM python:3.11-slim

WORKDIR /app

# Dependencias de sistema para obspy, scipy, matplotlib y torch CPU
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Instalar torch CPU antes de las otras dependencias (wheel CPU ~200MB vs ~2GB CUDA)
RUN pip install --no-cache-dir \
    torch==2.1.2 torchvision==0.16.2 \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código del worker y módulos ADOVE
COPY worker.py cesvec.py CLASIFICADOR_vgg16.py ./

# Filtros SOS y modelos VGG16 + OOD (copiados físicamente en este directorio)
COPY filters    /app/filters
COPY pipeline_v5 /app/pipeline_v5

RUN mkdir -p /app/out

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/shared/data
ENV OUT_DIR=/app/out
ENV UMBRAL=504
ENV COMPONENTE=Z
ENV VOLCAN=99

EXPOSE 8001

VOLUME ["/shared/data"]

CMD ["python", "worker.py"]
