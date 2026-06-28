"""
Orchestrateur du pipeline météo — version Open-Meteo.

Pipeline simplifié (vs GRIB) :
  Pour chaque ville :
    1. Appel API Open-Meteo (JSON, 1 requête = 31 membres × 16 jours)
    2. Calcul des statistiques d'ensemble
    3. Stockage en base SQLite
"""

import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import upsert_city, upsert_forecast, upsert_hourly
from .openmeteo_fetch import fetch_city_forecast, get_current_run_time

logger = logging.getLogger(__name__)

# ── Référentiel des villes ────────────────────────────────────────────────────
CITIES = [
    # Auvergne-Rhône-Alpes
    {"name": "Lyon",                      "lat": 45.748, "lon": 4.847,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Grenoble",                  "lat": 45.188, "lon": 5.724,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Clermont-Ferrand",          "lat": 45.777, "lon": 3.087,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Saint-Étienne",             "lat": 45.439, "lon": 4.387,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Chambéry",                  "lat": 45.564, "lon": 5.917,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Annecy",                    "lat": 45.899, "lon": 6.129,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Valence",                   "lat": 44.933, "lon": 4.892,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Roanne",                    "lat": 46.034, "lon": 4.070,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Aurillac",                  "lat": 44.926, "lon": 2.448,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Le Puy-en-Velay",           "lat": 45.043, "lon": 3.885,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Châtillon-sur-Chalaronne",  "lat": 46.117, "lon": 4.954,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Chanoz-Châtenay",           "lat": 46.008, "lon": 5.080,  "region": "Auvergne-Rhône-Alpes"},
    {"name": "Belleville-sur-Saône",      "lat": 46.108, "lon": 4.748,  "region": "Auvergne-Rhône-Alpes"},
    # Bourgogne-Franche-Comté
    {"name": "Dijon",            "lat": 47.322, "lon": 5.041, "region": "Bourgogne-Franche-Comté"},
    {"name": "Besançon",         "lat": 47.237, "lon": 6.024, "region": "Bourgogne-Franche-Comté"},
    {"name": "Chalon-sur-Saône", "lat": 46.781, "lon": 4.853, "region": "Bourgogne-Franche-Comté"},
    {"name": "Mâcon",            "lat": 46.306, "lon": 4.828, "region": "Bourgogne-Franche-Comté"},
    {"name": "Montbéliard",      "lat": 47.510, "lon": 6.797, "region": "Bourgogne-Franche-Comté"},
    {"name": "Belfort",          "lat": 47.638, "lon": 6.863, "region": "Bourgogne-Franche-Comté"},
    {"name": "Auxerre",          "lat": 47.798, "lon": 3.571, "region": "Bourgogne-Franche-Comté"},
    {"name": "Nevers",           "lat": 46.993, "lon": 3.157, "region": "Bourgogne-Franche-Comté"},
]


async def ensure_cities_in_db() -> dict[str, int]:
    """Synchronise les villes en base. Retourne {nom: id}."""
    city_ids: dict[str, int] = {}
    for city in CITIES:
        row = upsert_city(city["name"], city["lat"], city["lon"], city["region"])
        city_ids[city["name"]] = row["id"]
    logger.info(f"{len(city_ids)} villes synchronisées")
    return city_ids


async def process_all_cities() -> None:
    """
    Pipeline complet pour toutes les villes.
    Une seule session HTTP partagée pour toutes les requêtes Open-Meteo.
    """
    city_ids = await ensure_cities_in_db()
    run_time = get_current_run_time()
    logger.info(f"Début mise à jour — run {run_time} — {len(CITIES)} villes")

    async with httpx.AsyncClient() as client:
        for city in CITIES:
            result = await fetch_city_forecast(
                city["lat"], city["lon"], days=16, client=client
            )

            daily   = result.get("daily", [])
            hourly  = result.get("hourly", [])

            if not daily:
                logger.warning(f"Pas de données Open-Meteo pour {city['name']}")
                continue

            city_id = city_ids[city["name"]]

            for day in daily:
                s = day["stats"]
                upsert_forecast(city_id, run_time, day["date"], {
                    "temp_mean":    round(s.temp_mean, 2),
                    "temp_min":     round(s.temp_min, 2),
                    "temp_max":     round(s.temp_max, 2),
                    "temp_p25":     round(s.temp_p25, 2),
                    "temp_p50":     round(s.temp_p50, 2),
                    "temp_p75":     round(s.temp_p75, 2),
                    "tmax_mean":    round(s.tmax_mean, 2),
                    "tmax_p10":     round(s.tmax_p10, 2),
                    "tmax_p90":     round(s.tmax_p90, 2),
                    "tmin_mean":    round(s.tmin_mean, 2),
                    "tmin_p10":     round(s.tmin_p10, 2),
                    "tmin_p90":     round(s.tmin_p90, 2),
                    "precip_prob":  round(s.precip_prob, 3),
                    "precip_mean":  round(s.precip_mean, 2),
                    "precip_p75":   round(s.precip_p75, 2),
                    "precip_p90":   round(s.precip_p90, 2),
                    "member_count": s.member_count,
                })

            if hourly:
                upsert_hourly(city_id, run_time, hourly)

            logger.info(f"{city['name']} : {len(daily)} jours, {len(hourly)} heures")

    logger.info("Mise à jour terminée")


async def scheduled_update() -> None:
    """Point d'entrée pour le scheduler et l'endpoint /admin/update."""
    try:
        await process_all_cities()
    except Exception as e:
        logger.error(f"Échec de la mise à jour : {e}", exc_info=True)


def create_scheduler() -> AsyncIOScheduler:
    """Déclenche la mise à jour toutes les 6h (calé sur les runs GFS)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_update,
        trigger=CronTrigger(hour="1,7,13,19", minute=0),
        id="weather_update",
        name="Mise à jour météo",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler
