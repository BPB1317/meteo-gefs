"""
Calculs statistiques sur l'ensemble des membres GEFS.

Principe de l'ensemble météo :
  Chaque membre GEFS représente une évolution possible de l'atmosphère,
  obtenue en perturbant légèrement les conditions initiales.
  En agrégeant N membres, on obtient une distribution de probabilités
  au lieu d'une prévision déterministe unique.

Exemple avec 11 membres sur la température à Lyon J+5 :
  Membres : [18.2, 17.9, 19.1, 16.8, 18.5, 17.3, 20.1, 18.8, 17.5, 19.4, 18.0]
  → Moyenne  : 18.3 °C
  → P10 (froid) : 17.1 °C   ← 10% de chances qu'il fasse moins chaud
  → P90 (chaud) : 19.8 °C   ← 10% de chances qu'il fasse plus chaud
  → Incertitude totale : ±2.7 °C (P90 - P10)
"""

from dataclasses import dataclass
import numpy as np


@dataclass
class DailyStats:
    """Résumé statistique de l'ensemble pour un lieu et un jour donnés."""
    temp_mean:    float  # Moyenne des températures moyennes journalières (°C)
    temp_min:     float  # P10 des moyennes journalières — scénario frais
    temp_max:     float  # P90 des moyennes journalières — scénario chaud
    temp_p25:     float
    temp_p50:     float
    temp_p75:     float
    tmax_mean:    float  # Pic journalier moyen (max des 24h, moyenné sur les membres)
    tmax_p10:     float  # P10 des pics journaliers
    tmax_p90:     float  # P90 des pics journaliers
    tmin_mean:    float  # Creux journalier moyen (min des 24h, moyenné sur les membres)
    tmin_p10:     float  # P10 des creux journaliers
    tmin_p90:     float  # P90 des creux journaliers
    precip_prob:  float  # Fraction 0–1 de membres avec pluie > seuil
    precip_mean:  float  # Cumul journalier moyen (mm)
    precip_p75:   float
    precip_p90:   float
    member_count: int


def compute_ensemble_stats(
    temperatures: list[float | None],
    precipitations: list[float | None],
    rain_threshold_mm: float = 1.0,
    temp_maxima: list[float | None] | None = None,
    temp_minima: list[float | None] | None = None,
) -> DailyStats:
    """
    Calcule les statistiques d'ensemble pour un lieu et un jour.

    Args:
        temperatures: Températures à 2m en °C, une valeur par membre GEFS.
        precipitations: Cumuls journaliers en mm, une valeur par membre.
        rain_threshold_mm: Seuil en mm pour définir un "jour de pluie".

    Returns:
        DailyStats contenant percentiles température et probabilité de pluie.

    Raises:
        ValueError: Si aucune donnée de température valide n'est disponible.
    """
    temps   = np.array([t for t in temperatures if t is not None], dtype=float)
    precips = np.array([p for p in precipitations if p is not None and p >= 0], dtype=float)

    if len(temps) == 0:
        raise ValueError("Aucune donnée de température valide pour calculer les statistiques.")
    if len(precips) == 0:
        precips = np.zeros(len(temps))

    # Pics et creux journaliers — si non fournis, on approche depuis la moyenne
    tmaxs = np.array([t for t in (temp_maxima or []) if t is not None], dtype=float)
    tmins = np.array([t for t in (temp_minima or []) if t is not None], dtype=float)
    if len(tmaxs) == 0:
        tmaxs = temps
    if len(tmins) == 0:
        tmins = temps

    return DailyStats(
        temp_mean=float(np.mean(temps)),
        temp_min=float(np.percentile(temps, 10)),
        temp_max=float(np.percentile(temps, 90)),
        temp_p25=float(np.percentile(temps, 25)),
        temp_p50=float(np.percentile(temps, 50)),
        temp_p75=float(np.percentile(temps, 75)),
        tmax_mean=float(np.mean(tmaxs)),
        tmax_p10=float(np.percentile(tmaxs, 10)),
        tmax_p90=float(np.percentile(tmaxs, 90)),
        tmin_mean=float(np.mean(tmins)),
        tmin_p10=float(np.percentile(tmins, 10)),
        tmin_p90=float(np.percentile(tmins, 90)),
        precip_prob=float(np.mean(precips >= rain_threshold_mm)),
        precip_mean=float(np.mean(precips)),
        precip_p75=float(np.percentile(precips, 75)),
        precip_p90=float(np.percentile(precips, 90)),
        member_count=int(len(temps)),
    )


def daily_precipitation(
    accum_current: float | None,
    accum_previous: float | None,
) -> float:
    """
    Calcule la précipitation journalière à partir des cumuls accumulés.

    Le GEFS fournit APCP = cumul total depuis le début du run.
    Pour obtenir la pluie du jour N :
        P(jour N) = APCP(heure 24*N) − APCP(heure 24*(N−1))

    Args:
        accum_current: APCP à l'heure actuelle (mm).
        accum_previous: APCP à l'heure précédente (mm). None pour J+1.

    Returns:
        Précipitation journalière en mm (≥ 0).
    """
    if accum_current is None:
        return 0.0
    if accum_previous is None:
        return max(0.0, accum_current)
    return max(0.0, accum_current - accum_previous)
