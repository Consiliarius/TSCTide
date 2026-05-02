FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Operational scripts (calibration analysis, admin tooling, etc.) are
# bundled into the image so they can be invoked via:
#   docker exec tidal-access python /app/scripts/<scriptname>.py
# These are not part of the runtime web app but are convenient to ship
# inside the same image so operators don't need separate docker cp calls
# every time the container is rebuilt.
COPY scripts/ ./scripts/

RUN mkdir -p /app/data

EXPOSE 8866

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8866"]
