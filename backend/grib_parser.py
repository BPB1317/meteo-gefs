"""
Parsing des fichiers GRIB2 GEFS avec cfgrib + xarray.

Prérequis système : eccodes (bibliothèque C de l'ECMWF)
  Linux  : sudo apt-get install libeccodes-dev
  macOS  : brew install eccodes
  Windows: conda install -c conda-forge eccodes  (recommandé via Conda)

Structure d'un fichier GRIB2 GEFS pgrb2s :
  Chaque fichier contient plusieurs "messages" GRIB, un par variable/niveau.
  cfgrib ouvre chaque message comme un Dataset xarray distinct.
  On filtre via filter_by_keys pour cibler exactement ce qu'on veut.

Coordonnées GEFS :
  - latitude  : décroissant de 90°N à -90°S (90, 89.75, ..., -90)
  - longitude : croissant de 0° à 359.75° (convention 0–360, pas -180–180)
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    import xarray as xr
    XARRAY_AVAILABLE = True
except ImportError:
    XARRAY_AVAILABLE = False
    logger.error("xarray non disponible — pip install xarray")

try:
    import cfgrib  # noqa: F401 (import vérifié, l'engine est utilisé via xarray)
    CFGRIB_AVAILABLE = True
except ImportError:
    CFGRIB_AVAILABLE = False
    logger.error("cfgrib non disponible — pip install cfgrib (+ eccodes système)")


def _check_deps() -> None:
    if not XARRAY_AVAILABLE or not CFGRIB_AVAILABLE:
        raise RuntimeError(
            "Dépendances manquantes : installez xarray et cfgrib (+ eccodes système).\n"
            "Voir : https://github.com/ecmwf/cfgrib#installation"
        )


def _open_grib(file_path: Path, filter_keys: dict) -> Optional["xr.Dataset"]:
    """
    Ouvre un fichier GRIB2 et filtre sur les clés demandées.
    Retourne None si le message correspondant n'existe pas.
    """
    _check_deps()
    try:
        ds = xr.open_dataset(
            str(file_path),
            engine="cfgrib",
            filter_by_keys=filter_keys,
            backend_kwargs={"indexing": False},  # évite la création de fichiers .idx
            indexpath=None,
        )
        return ds
    except Exception as e:
        logger.debug(f"Impossible d'ouvrir {file_path.name} avec {filter_keys} : {e}")
        return None


def _nearest_point(ds: "xr.Dataset", lat: float, lon: float) -> dict:
    """
    Trouve le point de grille le plus proche du (lat, lon) demandé.

    GEFS utilise la convention 0–360° pour la longitude.
    On convertit la longitude négative si nécessaire.
    """
    grib_lon = lon % 360  # ex: -5° → 355°

    lat_vals = ds.latitude.values
    lon_vals = ds.longitude.values

    lat_idx = int(np.argmin(np.abs(lat_vals - lat)))
    lon_idx = int(np.argmin(np.abs(lon_vals - grib_lon)))

    return {"lat_idx": lat_idx, "lon_idx": lon_idx}


def extract_temperature(file_path: Path, lat: float, lon: float) -> Optional[float]:
    """
    Extrait la température à 2m (°C) au point de grille le plus proche.

    Le GEFS stocke la température en Kelvin. cfgrib la nomme souvent 't2m'
    mais le nom exact peut varier selon les tables GRIB utilisées.
    On tente plusieurs noms pour robustesse.

    Returns:
        Température en °C, ou None si données absentes/invalides.
    """
    ds = _open_grib(file_path, {
        "typeOfLevel": "heightAboveGround",
        "level": 2,
    })

    if ds is None:
        return None

    # Le nom de la variable peut différer selon la version des tables GRIB
    temp_var = next((v for v in ["t2m", "2t", "TMP", "t"] if v in ds.data_vars), None)
    if temp_var is None:
        logger.debug(f"{file_path.name} — variables dispo : {list(ds.data_vars)}")
        ds.close()
        return None

    try:
        idx = _nearest_point(ds, lat, lon)
        value = float(ds[temp_var].isel(
            latitude=idx["lat_idx"],
            longitude=idx["lon_idx"],
        ).values)

        if np.isnan(value):
            return None

        # Conversion Kelvin → Celsius (GEFS stocke en K, plage valide 150–340K)
        return value - 273.15 if value > 100 else value  # déjà en °C si < 100

    except Exception as e:
        logger.error(f"Erreur extraction température {file_path.name} : {e}")
        return None
    finally:
        ds.close()


def extract_precipitation(file_path: Path, lat: float, lon: float) -> Optional[float]:
    """
    Extrait le cumul de précipitations accumulées (mm) depuis le début du run.

    GEFS fournit APCP = "Total Accumulated Precipitation" depuis t=0.
    Pour obtenir la pluie d'un intervalle, on fait la différence en dehors
    de cette fonction (voir statistics.daily_precipitation).

    Returns:
        Précipitations cumulées en mm, ou None si données absentes.
    """
    # On essaie plusieurs filtres car les tables GRIB varient entre runs
    filters_to_try = [
        {"typeOfLevel": "surface", "shortName": "tp"},    # CF convention
        {"typeOfLevel": "surface", "shortName": "acpcp"}, # NCEP convention
        {"typeOfLevel": "surface", "stepType": "accum"},  # Filtre par type d'étape
    ]

    ds = None
    for filt in filters_to_try:
        ds = _open_grib(file_path, filt)
        if ds is not None:
            break

    if ds is None:
        return None

    precip_var = next(
        (v for v in ["tp", "acpcp", "APCP", "unknown"] if v in ds.data_vars),
        None,
    )
    if precip_var is None:
        ds.close()
        return None

    try:
        idx = _nearest_point(ds, lat, lon)
        value = float(ds[precip_var].isel(
            latitude=idx["lat_idx"],
            longitude=idx["lon_idx"],
        ).values)

        if np.isnan(value) or value < 0:
            return None

        # GEFS exprime APCP en kg/m² (= mm) — pas de conversion nécessaire
        return value

    except Exception as e:
        logger.error(f"Erreur extraction précipitation {file_path.name} : {e}")
        return None
    finally:
        ds.close()
