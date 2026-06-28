"""
Récupération des prévisions d'ensemble GEFS via Open-Meteo.

Un seul appel API par ville retourne tous les membres (hourly).
On produit deux niveaux de résolution :
  - Journalier (16 jours) : Tmax/Tmin/Tmean + probabilité de pluie
  - Horaire (5 jours)     : percentiles T heure par heure + pluie/heure
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np

from .statistics import compute_ensemble_stats

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
HOURLY_DAYS = 16  # Horizon pour les certitudes horaires (max Open-Meteo)


async def _fetch_raw(
    lat: float,
    lon: float,
    days: int,
    client: httpx.AsyncClient,
) -> dict:
    """Appel brut Open-Meteo — retourne le JSON complet."""
    resp = await client.get(
        ENSEMBLE_API_URL,
        params={
            "latitude":      lat,
            "longitude":     lon,
            "models":        "gfs_seamless",
            "hourly":        "temperature_2m,precipitation",
            "forecast_days": min(days, 16),
            "timezone":      "UTC",
        },
        timeout=60.0,
    )
    if not resp.is_success:
        logger.error(f"Open-Meteo {resp.status_code} ({lat},{lon}): {resp.text[:200]}")
        return {}
    return resp.json()


async def fetch_city_forecast(
    lat: float,
    lon: float,
    days: int = 16,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """
    Récupère les prévisions ensemble pour une ville.

    Returns:
        {
          "daily":  [ {date, stats: DailyStats}, ... ],   # 16 jours
          "hourly": [ {time, t_p10, t_p25, t_p50, t_p75, t_p90,
                        precip_prob, precip_mean}, ... ]  # 5 jours horaire
        }
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        data = await _fetch_raw(lat, lon, days, client)
    except Exception as e:
        logger.error(f"Open-Meteo erreur ({lat},{lon}): {e}")
        data = {}
    finally:
        if own_client:
            await client.aclose()

    if not data:
        return {"daily": [], "hourly": []}

    hourly  = data.get("hourly", {})
    times   = hourly.get("time", [])

    # Clés membres détectées automatiquement
    t_keys = [k for k in hourly if k == "temperature_2m" or k.startswith("temperature_2m_member")]
    p_keys = [k for k in hourly if k == "precipitation"  or k.startswith("precipitation_member")]
    logger.debug(f"  → {len(t_keys)} membres température, {len(p_keys)} précip")

    # ── Collecte horaire ──────────────────────────────────────────────────────
    # Pour chaque heure i : liste des valeurs de tous les membres
    n = len(times)
    hour_temps   = [[] for _ in range(n)]  # hour_temps[i]   = [val_m1, val_m2, ...]
    hour_precips = [[] for _ in range(n)]

    for k in t_keys:
        for i, v in enumerate(hourly[k]):
            if v is not None:
                hour_temps[i].append(float(v))

    for k in p_keys:
        for i, v in enumerate(hourly[k]):
            if v is not None:
                hour_precips[i].append(float(v))

    # ── Stats journalières ────────────────────────────────────────────────────
    # Regrouper les valeurs horaires par (date, membre)
    date_member_temps   = defaultdict(lambda: defaultdict(list))
    date_member_precips = defaultdict(lambda: defaultdict(list))

    for i, t_str in enumerate(times):
        date_str = t_str[:10]
        for ki, k in enumerate(t_keys):
            v = hourly[k][i]
            if v is not None:
                date_member_temps[date_str][ki].append(float(v))
        for ki, k in enumerate(p_keys):
            v = hourly[k][i]
            if v is not None:
                date_member_precips[date_str][ki].append(float(v))

    daily = []
    for date_str in sorted(date_member_temps.keys())[:days]:
        # Par membre : moyenne, max, min journaliers
        m_means = [float(np.mean(v)) for v in date_member_temps[date_str].values() if v]
        m_maxes = [float(np.max(v))  for v in date_member_temps[date_str].values() if v]
        m_mins  = [float(np.min(v))  for v in date_member_temps[date_str].values() if v]
        m_prec  = [float(np.sum(v))  for v in date_member_precips[date_str].values() if v]

        if not m_means:
            continue
        try:
            stats = compute_ensemble_stats(
                m_means, m_prec,
                temp_maxima=m_maxes,
                temp_minima=m_mins,
            )
            daily.append({"date": date_str, "stats": stats})
        except ValueError as e:
            logger.warning(f"Stats journalières {date_str}: {e}")

    # ── Stats horaires (5 premiers jours) ─────────────────────────────────────
    max_h = HOURLY_DAYS * 24
    hourly_stats = []
    for i, t_str in enumerate(times[:max_h]):
        t_vals = np.array(hour_temps[i])
        p_vals = np.array(hour_precips[i])

        if len(t_vals) == 0:
            continue

        hourly_stats.append({
            "time":        t_str,
            "t_p10":       round(float(np.percentile(t_vals, 10)), 2),
            "t_p25":       round(float(np.percentile(t_vals, 25)), 2),
            "t_p50":       round(float(np.percentile(t_vals, 50)), 2),
            "t_p75":       round(float(np.percentile(t_vals, 75)), 2),
            "t_p90":       round(float(np.percentile(t_vals, 90)), 2),
            "precip_prob": round(float(np.mean(p_vals > 0.1)), 3),
            "precip_mean": round(float(np.mean(p_vals)), 3),
        })

    logger.info(f"  → {len(daily)} jours journaliers, {len(hourly_stats)} heures")
    return {"daily": daily, "hourly": hourly_stats}


def get_current_run_time() -> str:
    now = datetime.now(timezone.utc)
    run_hour = (now.hour // 6) * 6
    return now.replace(hour=run_hour, minute=0, second=0, microsecond=0).isoformat()
