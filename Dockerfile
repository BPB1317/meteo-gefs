# ── Build image ───────────────────────────────────────────────────────────────
FROM python:3.12-slim

# eccodes est nécessaire pour cfgrib (bibliothèque C de parsing GRIB2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances Python d'abord (layer Docker cachable)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Créer le répertoire pour les fichiers GRIB temporaires
RUN mkdir -p data/grib

# L'application écoute sur le port 8000
EXPOSE 8000

# Uvicorn avec 2 workers en production
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
