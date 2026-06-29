"""
Récupération des prévisions d'ensemble multi-modèles via Open-Meteo.
Super-ensemble : GEFS (31) + ICON-EPS DWD (~40) + GEPS Canada (21) ≈ 92 membres.
Les 3 modèles sont fetché en parallèle par ville ; les membres sont fusionnés avant
le calcul des percentiles.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

import httpx
import numpy as np

from .statistics import compute_ensemble_stats

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
HOURLY_DAYS = 16

# Modèles à combiner et leur horizon maximal en jours
MODELS = {
    "gfs_seamless":  16,   # GEFS NOAA    — 31 membres, jusqu'à 35 jours
    "icon_seamless":  7,   # ICON-EPS DWD — ~40 membres, ~7 jours
    "gem_global":    16,   # GEPS Canada  — 21 membres, jusqu'à 16 jours
}


async def _fetch_model(
    lat: float,
    lon: float,
    model: str,
    max_days: int,
    client: httpx.AsyncClient,
) -> dict:
    """Appel Open-Meteo pour un modèle. Retourne {} si erreur non-bloquante."""
    try:
        resp = await client.get(
            ENSEMBLE_API_URL,
            params={
                "latitude":      lat,
                "longitude":     lon,
                "models":        model,
                "hourly":        "temperature_2m,precipitation",
                "forecast_days": min(max_days, HOURLY_DAYS),
                "timezone":      "UTC",
            },
            timeout=60.0,
        )
        if not resp.is_success:
            logger.warning(f"{model} HTTP {resp.status_code} ({lat},{lon}): {resp.text[:120]}")
            return {}
        return resp.json()
    except Exception as e:
        logger.warning(f"{model} erreur ({lat},{lon}): {e}")
        return {}


async def fetch_city_forecast(
    lat: float,
    lon: float,
    days: int = 16,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """
    Récupère et fusionne les prévisions de tous les modèles pour une ville.

    Returns:
        {
          "daily":  [ {date, stats: DailyStats}, ... ],         # jusqu'à 16 jours
          "hourly": [ {time, t_p10/p25/p50/p75/p90, …}, ... ]  # jusqu'à 16 jours horaire
        }
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        raw_results = await asyncio.gather(*(
            _fetch_model(lat, lon, model, max_days, client)
            for model, max_days in MODELS.items()
        ))
    finally:
        if own_client:
            await client.aclose()

    # ── Fusion des membres par timestamp ──────────────────────────────────────
    # time_to_temps[t_str]     = [val_mbr_gefs_0, ..., val_mbr_icon_0, ..., val_mbr_geps_0, ...]
    # date_member_temps[date]  = [ [h0,h1,...] par membre ] — pour les stats journalières
    time_to_temps       = defaultdict(list)
    time_to_precips     = defaultdict(list)
    date_member_temps   = defaultdict(list)
    date_member_precips = defaultdict(list)
    total_members = 0

    for model_name, data in zip(MODELS.keys(), raw_results):
        if not data:
            continue
        hourly = data.get("hourly", {})
        times  = hourly.get("time", [])
        t_keys = sorted(k for k in hourly if k == "temperature_2m" or k.startswith("temperature_2m_member"))
        p_keys = sorted(k for k in hourly if k == "precipitation"  or k.startswith("precipitation_member"))

        total_members += len(t_keys)
        logger.info(f"  {model_name}: {len(t_keys)} membres, {len(times)} heures")

        for k in t_keys:
            by_date: dict[str, list] = defaultdict(list)
            for t_str, v in zip(times, hourly[k]):
                if v is not None:
                    fv = float(v)
                    time_to_temps[t_str].append(fv)
                    by_date[t_str[:10]].append(fv)
            for date_str, vals in by_date.items():
                date_member_temps[date_str].append(vals)

        for k in p_keys:
            by_date = defaultdict(list)
            for t_str, v in zip(times, hourly[k]):
                if v is not None:
                    fv = float(v)
                    time_to_precips[t_str].append(fv)
                    by_date[t_str[:10]].append(fv)
            for date_str, vals in by_date.items():
                date_member_precips[date_str].append(vals)

    if not time_to_temps:
        logger.error(f"Aucun modèle n'a retourné de données pour ({lat},{lon})")
        return {"daily": [], "hourly": []}

    logger.info(f"  → {total_members} membres fusionnés, {len(time_to_temps)} timestamps couverts")

    # ── Stats journalières ────────────────────────────────────────────────────
    daily = []
    for date_str in sorted(date_member_temps.keys())[:days]:
        m_means = [float(np.mean(v)) for v in date_member_temps[date_str]   if v]
        m_maxes = [float(np.max(v))  for v in date_member_temps[date_str]   if v]
        m_mins  = [float(np.min(v))  for v in date_member_temps[date_str]   if v]
        m_prec  = [float(np.sum(v))  for v in date_member_precips[date_str] if v]
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

    # ── Stats horaires ────────────────────────────────────────────────────────
    max_h = HOURLY_DAYS * 24
    hourly_stats = []

    for t_str in sorted(time_to_temps.keys())[:max_h]:
        t_vals = np.array(time_to_temps[t_str])
        p_vals = np.array(time_to_precips.get(t_str, []))

        if len(t_vals) == 0:
            continue

        hourly_stats.append({
            "time":        t_str,
            "t_p10":       round(float(np.percentile(t_vals, 10)), 2),
            "t_p25":       round(float(np.percentile(t_vals, 25)), 2),
            "t_p50":       round(float(np.percentile(t_vals, 50)), 2),
            "t_p75":       round(float(np.percentile(t_vals, 75)), 2),
            "t_p90":       round(float(np.percentile(t_vals, 90)), 2),
            "precip_prob": round(float(np.mean(p_vals > 0.1)) if len(p_vals) > 0 else 0.0, 3),
            "precip_mean": round(float(np.mean(p_vals))       if len(p_vals) > 0 else 0.0, 3),
        })

    logger.info(f"  → {len(daily)} jours journaliers, {len(hourly_stats)} heures horaires")
    return {"daily": daily, "hourly": hourly_stats}


def get_current_run_time() -> str:
    now = datetime.now(timezone.utc)
    run_hour = (now.hour // 6) * 6
    return now.replace(hour=run_hour, minute=0, second=0, microsecond=0).isoformat()
