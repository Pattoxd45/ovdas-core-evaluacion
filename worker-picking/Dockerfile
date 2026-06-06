FROM python:3.11-slim

WORKDIR /app

# ObsPy requiere libgfortran (backend de scipy/numpy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "worker.py"]
