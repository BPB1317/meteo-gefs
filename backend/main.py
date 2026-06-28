"""
Application FastAPI — Météo Probabiliste AuRA & BFC

Routes :
  GET  /            → Infos API
  GET  /health      → Santé du service
  GET  /cities      → Liste des villes disponibles
  GET  /forecast    → Prévision pour une ville ou des coordonnées
  POST /admin/update → Déclenche une mise à jour manuelle (utile au démarrage)
"""

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import get_settings
from .database import (
    get_city_by_name, get_forecast, get_hourly_forecast,
    get_all_cities_map_data, list_cities, init_db,
)
from .scheduler import CITIES, create_scheduler, scheduled_update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = create_scheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle FastAPI : initialise la DB, démarre/arrête le scheduler."""
    init_db()
    scheduler.start()
    logger.info("Scheduler APScheduler démarré")
    # Charge les données dès le démarrage (utile après un redéploiement)
    asyncio.create_task(scheduled_update())
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler arrêté")


app = FastAPI(
    title="Météo Probabiliste",
    description="Prévisions d'ensemble GEFS pour AuRA & BFC — 16 jours",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS permissif pour le développement local
# En production, remplacer allow_origins=["*"] par votre domaine front
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/app", StaticFiles(directory="frontend", html=True), name="frontend")


# ── Modèles de réponse ────────────────────────────────────────────────────────

class DailyForecast(BaseModel):
    date:         str
    temp_mean:    float
    temp_min:     float
    temp_max:     float
    temp_p25:     float
    temp_p75:     float
    tmax_mean:    Optional[float] = None
    tmax_p10:     Optional[float] = None
    tmax_p90:     Optional[float] = None
    tmin_mean:    Optional[float] = None
    tmin_p10:     Optional[float] = None
    tmin_p90:     Optional[float] = None
    precip_prob:  float
    precip_mean:  float
    member_count: int


class HourlyForecast(BaseModel):
    time:        str
    t_p10:       float
    t_p25:       float
    t_p50:       float
    t_p75:       float
    t_p90:       float
    precip_prob: float
    precip_mean: float


class HourlyResponse(BaseModel):
    city:     str
    run_time: str
    forecast: list[HourlyForecast]


class ForecastResponse(BaseModel):
    city:         str
    lat:          float
    lon:          float
    region:       str
    run_time:     str    # Horodatage du run GEFS utilisé
    days:         int
    forecast:     list[DailyForecast]


class CityInfo(BaseModel):
    name:   str
    lat:    float
    lon:    float
    region: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique (grand cercle) en kilomètres."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_city(lat: float, lon: float, max_km: float = 150.0) -> Optional[dict]:
    """Renvoie la ville prédéfinie la plus proche dans un rayon de max_km."""
    best, best_dist = None, float("inf")
    for city in CITIES:
        d = _haversine_km(lat, lon, city["lat"], city["lon"])
        if d < best_dist:
            best_dist, best = d, city
    return best if best_dist <= max_km else None


def _validate_region(lat: float, lon: float) -> None:
    s = get_settings()
    if not (s.GEO_BOTTOM_LAT <= lat <= s.GEO_TOP_LAT):
        raise HTTPException(
            400,
            f"Latitude {lat}° hors de la zone couverte ({s.GEO_BOTTOM_LAT}°–{s.GEO_TOP_LAT}°N)",
        )
    if not (s.GEO_LEFT_LON <= lon <= s.GEO_RIGHT_LON):
        raise HTTPException(
            400,
            f"Longitude {lon}° hors de la zone couverte ({s.GEO_LEFT_LON}°–{s.GEO_RIGHT_LON}°E)",
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "Météo Probabiliste — AuRA & BFC",
        "version": "1.0.0",
        "docs": "/docs",
        "routes": {
            "cities": "/cities",
            "forecast": "/forecast?city=Lyon  ou  /forecast?lat=45.75&lon=4.85",
            "update": "POST /admin/update",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "utc": datetime.utcnow().isoformat()}


@app.get("/cities", response_model=list[CityInfo])
def cities():
    """Liste toutes les villes pour lesquelles des prévisions sont disponibles."""
    rows = list_cities()
    if not rows:
        # Retourner la liste statique si la DB est vide (avant la 1re mise à jour)
        return [CityInfo(**{k: v for k, v in c.items() if k != "id"}) for c in CITIES]
    return rows


@app.get("/forecast", response_model=ForecastResponse)
def forecast(
    city: Optional[str] = Query(None, description="Nom de la ville (ex: Lyon)"),
    lat:  Optional[float] = Query(None, description="Latitude en °N (ex: 45.75)", ge=40.0, le=52.0),
    lon:  Optional[float] = Query(None, description="Longitude en °E (ex: 4.85)", ge=-5.0, le=15.0),
    days: int = Query(7, description="Nombre de jours de prévision", ge=1, le=16),
):
    """
    Retourne les prévisions probabilistes pour une ville ou des coordonnées.

    - Avec `city` : recherche exacte sur le nom (insensible à la casse).
    - Avec `lat` + `lon` : accrochage à la ville la plus proche dans un rayon de 150 km.
    - `days` : 1 à 16 (défaut 7).
    """
    # ── Résolution du lieu ────────────────────────────────────────────────────
    if city:
        # Recherche insensible à la casse
        city_row = get_city_by_name(city) or get_city_by_name(city.title())
        if not city_row:
            available = ", ".join(c["name"] for c in CITIES)
            raise HTTPException(
                404,
                f"Ville '{city}' inconnue. Villes disponibles : {available}",
            )

    elif lat is not None and lon is not None:
        _validate_region(lat, lon)
        nearest = _nearest_city(lat, lon)
        if not nearest:
            raise HTTPException(
                404,
                "Aucune ville dans un rayon de 150 km. Utilisez /cities pour la liste.",
            )
        city_row = get_city_by_name(nearest["name"])
        if not city_row:
            raise HTTPException(503, "Ville trouvée mais absente de la base — réessayez dans quelques instants.")

    else:
        raise HTTPException(
            400,
            "Fournissez soit `city` soit `lat` ET `lon`.",
        )

    # ── Récupération des données ──────────────────────────────────────────────
    rows = get_forecast(city_row["id"], limit=days)
    if not rows:
        raise HTTPException(
            503,
            "Données non encore disponibles. La 1ʳᵉ mise à jour est lancée toutes les 6h. "
            "Déclenchez-la manuellement via POST /admin/update.",
        )

    run_time = rows[0]["run_time"]
    daily = [
        DailyForecast(
            date=row["valid_date"],
            temp_mean=row["temp_mean"],
            temp_min=row["temp_min"],
            temp_max=row["temp_max"],
            temp_p25=row["temp_p25"],
            temp_p75=row["temp_p75"],
            tmax_mean=row["tmax_mean"] if row["tmax_mean"] is not None else row["temp_max"],
            tmax_p10=row["tmax_p10"]  if row["tmax_p10"]  is not None else row["temp_min"],
            tmax_p90=row["tmax_p90"]  if row["tmax_p90"]  is not None else row["temp_max"],
            tmin_mean=row["tmin_mean"] if row["tmin_mean"] is not None else row["temp_min"],
            tmin_p10=row["tmin_p10"]  if row["tmin_p10"]  is not None else row["temp_min"],
            tmin_p90=row["tmin_p90"]  if row["tmin_p90"]  is not None else row["temp_max"],
            precip_prob=row["precip_prob"],
            precip_mean=row["precip_mean"],
            member_count=row["member_count"],
        )
        for row in rows
    ]

    return ForecastResponse(
        city=city_row["name"],
        lat=city_row["lat"],
        lon=city_row["lon"],
        region=city_row["region"],
        run_time=run_time,
        days=len(daily),
        forecast=daily,
    )


@app.get("/forecast/map")
def forecast_map():
    """
    Données horaires de toutes les villes pour la carte animée.
    Un seul appel retourne 18 villes × 120 heures — optimisé pour l'animation front.
    """
    data = get_all_cities_map_data()
    if not data:
        raise HTTPException(503, "Données cartographiques non disponibles. Lancez POST /admin/update.")
    return data


@app.get("/forecast/hourly", response_model=HourlyResponse)
def forecast_hourly(
    city: Optional[str] = Query(None),
    lat:  Optional[float] = Query(None, ge=40.0, le=52.0),
    lon:  Optional[float] = Query(None, ge=-5.0, le=15.0),
    hours: int = Query(384, ge=24, le=384),
):
    """Certitudes de température heure par heure (5 jours max)."""
    if city:
        city_row = get_city_by_name(city)
        if not city_row:
            raise HTTPException(404, f"Ville '{city}' inconnue.")
    elif lat is not None and lon is not None:
        nearest = _nearest_city(lat, lon)
        if not nearest:
            raise HTTPException(404, "Aucune ville dans un rayon de 150 km.")
        city_row = get_city_by_name(nearest["name"])
    else:
        raise HTTPException(400, "Fournissez `city` ou `lat`+`lon`.")

    rows = get_hourly_forecast(city_row["id"], hours=hours)
    if not rows:
        raise HTTPException(503, "Données horaires non disponibles. Lancez POST /admin/update.")

    return HourlyResponse(
        city=city_row["name"],
        run_time=rows[0]["run_time"],
        forecast=[
            HourlyForecast(
                time=row["valid_time"],
                t_p10=row["t_p10"],
                t_p25=row["t_p25"],
                t_p50=row["t_p50"],
                t_p75=row["t_p75"],
                t_p90=row["t_p90"],
                precip_prob=row["precip_prob"],
                precip_mean=row["precip_mean"],
            )
            for row in rows
        ],
    )


@app.post("/admin/update")
async def trigger_update(background_tasks: BackgroundTasks):
    """
    Déclenche manuellement la mise à jour météo (utile au premier démarrage).
    S'exécute en arrière-plan — la réponse est immédiate.
    """
    background_tasks.add_task(scheduled_update)
    return {"message": "Mise à jour lancée en arrière-plan (Open-Meteo → SQLite)"}
