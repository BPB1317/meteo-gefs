"""
Téléchargement des fichiers GRIB2 GEFS depuis NOAA NOMADS.

Architecture du GEFS :
  - 4 runs par jour : 00Z, 06Z, 12Z, 18Z
  - 30 membres perturbés (p01–p30) + 1 membre contrôle (c00)
  - Résolution : 0.25° (~28 km)
  - Horizon : 16 jours (384 h)

Astuce clé — filtre géographique NOMADS :
  NOMADS propose un endpoint HTTP qui découpe le GRIB2 à la volée.
  On demande uniquement la zone AuRA+BFC au lieu de télécharger la planète.
  Réduction typique : 200 MB → 2 MB par fichier.

URL type :
  https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25_ens.pl
    ?dir=%2Fgefs.20240115%2F00%2Fatmos%2Fpgrb2sp25
    &file=gep01.t00z.pgrb2s.0p25.f024
    &var_TMP=on&var_APCP=on
    &lev_2_m_above_ground=on&lev_surface=on
    &subregion=&leftlon=1&rightlon=9&toplat=50&bottomlat=43
"""

import asyncio
import logging
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


# ── Utilitaires de nommage ────────────────────────────────────────────────────

def _gefs_filename(member: str, run_hour: str, forecast_hour: int) -> str:
    """
    Construit le nom de fichier GEFS selon la convention NOAA.

    Convention : ge[c|p][00|01-30].tHHz.pgrb2s.0p25.fFFF
    Exemples :
      gec00.t00z.pgrb2s.0p25.f024  → contrôle, run 00Z, échéance +24h
      gep03.t12z.pgrb2s.0p25.f120  → membre 3, run 12Z, échéance +120h
    """
    prefix = "gec00" if member == "c00" else f"ge{member}"
    return f"{prefix}.t{run_hour}z.pgrb2s.0p25.f{forecast_hour:03d}"


def _nomads_dir(run_date: str, run_hour: str) -> str:
    """Chemin NOMADS — httpx se charge de l'encodage URL, on passe le slash brut."""
    return f"/gefs.{run_date}/{run_hour}/atmos/pgrb2sp25"


# ── Téléchargement d'un seul fichier ─────────────────────────────────────────

async def _download_one(
    client: httpx.AsyncClient,
    run_date: str,
    run_hour: str,
    member: str,
    forecast_hour: int,
    output_dir: Path,
) -> Path | None:
    """
    Télécharge un fichier GRIB2 depuis NOMADS avec filtre géographique.
    Retourne le chemin local si succès, None sinon.
    Le fichier est ignoré s'il est déjà en cache.
    """
    s = get_settings()
    filename = _gefs_filename(member, run_hour, forecast_hour)
    local_path = output_dir / f"{run_date}_{run_hour}_{member}_f{forecast_hour:03d}.grib2"

    # Cache hit — évite de re-télécharger
    if local_path.exists() and local_path.stat().st_size > 500:
        return local_path

    params = {
        "dir":                    _nomads_dir(run_date, run_hour),
        "file":                   filename,
        "var_TMP":                "on",   # Température 2m
        "var_APCP":               "on",   # Précip. accumulées
        "lev_2_m_above_ground":   "on",
        "lev_surface":            "on",
        "subregion":              "",
        "leftlon":                str(int(s.GEO_LEFT_LON)),
        "rightlon":               str(int(s.GEO_RIGHT_LON)),
        "toplat":                 str(int(s.GEO_TOP_LAT)),
        "bottomlat":              str(int(s.GEO_BOTTOM_LAT)),
    }

    try:
        resp = await client.get(s.NOMADS_FILTER_URL, params=params, timeout=120.0)
        resp.raise_for_status()

        # NOMADS retourne une page HTML si la donnée n'est pas disponible
        if len(resp.content) < 500 or resp.content[:4] == b"<htm":
            logger.warning(f"NOMADS : données indisponibles pour {filename}")
            return None

        local_path.write_bytes(resp.content)
        logger.debug(f"OK {filename} ({local_path.stat().st_size:,} octets)")
        return local_path

    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP {e.response.status_code} pour {filename}")
        return None
    except Exception as e:
        logger.error(f"Erreur lors du téléchargement de {filename} : {e}")
        return None


# ── Téléchargement d'un run complet ──────────────────────────────────────────

async def download_gefs_run(
    run_date: str,
    run_hour: str,
    output_dir: Path | None = None,
) -> dict[tuple[str, int], Path]:
    """
    Télécharge tous les membres GEFS configurés pour un run donné.

    Args:
        run_date: Date au format YYYYMMDD (ex: '20240115').
        run_hour: Heure UTC du run : '00', '06', '12' ou '18'.
        output_dir: Répertoire de stockage local (créé si absent).

    Returns:
        Dictionnaire {(membre, échéance_h): chemin_local} pour les fichiers téléchargés.
    """
    s = get_settings()
    if output_dir is None:
        output_dir = Path(s.GRIB_DATA_DIR) / run_date / run_hour
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[tuple[str, int], Path] = {}
    semaphore = asyncio.Semaphore(s.MAX_CONCURRENT_DOWNLOADS)

    async def bounded_download(member: str, fh: int) -> None:
        async with semaphore:
            path = await _download_one(client, run_date, run_hour, member, fh, output_dir)
            if path:
                results[(member, fh)] = path

    total = len(s.GEFS_MEMBERS) * len(s.GEFS_FORECAST_HOURS)
    logger.info(f"Démarrage téléchargement GEFS : {total} fichiers — run {run_date}/{run_hour}Z")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [
            bounded_download(m, fh)
            for m in s.GEFS_MEMBERS
            for fh in s.GEFS_FORECAST_HOURS
        ]
        await asyncio.gather(*tasks)

    logger.info(f"Téléchargés : {len(results)}/{total} fichiers")
    return results


# ── Utilitaires ───────────────────────────────────────────────────────────────

def get_latest_gefs_run() -> tuple[str, str]:
    """
    Calcule le run GEFS le plus récent disponible.

    Les runs sont publiés à 00Z, 06Z, 12Z, 18Z.
    Les données sont disponibles environ 5h après le début du run.
    On prend donc toujours le run précédent si le run courant est trop récent.
    """
    now = datetime.now(timezone.utc)
    run_hour = (now.hour // 6) * 6
    run_dt = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)

    if (now - run_dt) < timedelta(hours=5):
        run_dt -= timedelta(hours=6)

    return run_dt.strftime("%Y%m%d"), f"{run_dt.hour:02d}"


def cleanup_old_runs(base_dir: Path, keep_days: int = 2) -> None:
    """Supprime les fichiers GRIB plus vieux que keep_days pour libérer l'espace disque."""
    if not base_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for date_dir in base_dir.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y%m%d")
            if dir_date < cutoff:
                shutil.rmtree(date_dir)
                logger.info(f"Nettoyage GRIB : {date_dir} supprimé")
        except ValueError:
            pass  # Répertoire non daté, ignoré
