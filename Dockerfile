FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping \
        traceroute \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

ENV PYTHONUNBUFFERED=1
WORKDIR /app/src

CMD ["python", "main.py"]
