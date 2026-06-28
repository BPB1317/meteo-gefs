"""
Configuration centrale. Toutes les valeurs viennent des variables d'environnement.
En production (Railway) : DATABASE_URL est injecté automatiquement.
En local : copier .env.example → .env et remplir DATABASE_URL.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Base de données PostgreSQL ────────────────────────────────────────────
    DATABASE_URL: str  # ex: postgresql://user:pass@host:5432/dbname

    # ── Emprise géographique (AuRA + BFC + marge) ────────────────────────────
    GEO_LEFT_LON:   float = 1.0
    GEO_RIGHT_LON:  float = 9.0
    GEO_TOP_LAT:    float = 50.0
    GEO_BOTTOM_LAT: float = 43.0

    # ── Seuils météo ──────────────────────────────────────────────────────────
    RAIN_THRESHOLD_MM: float = 1.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
