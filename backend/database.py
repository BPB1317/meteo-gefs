"""
Couche d'accès PostgreSQL.
Tables : cities, daily_forecasts, hourly_forecasts.
"""

import logging
import os
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor, execute_values

logger = logging.getLogger(__name__)


def get_conn() -> psycopg2.extensions.connection:
    url = os.environ["DATABASE_URL"]
    # Railway préfixe parfois avec "postgres://" — psycopg2 veut "postgresql://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    return conn


def init_db() -> None:
    """Crée les tables au démarrage si elles n'existent pas."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cities (
                    id     SERIAL PRIMARY KEY,
                    name   TEXT NOT NULL UNIQUE,
                    lat    FLOAT NOT NULL,
                    lon    FLOAT NOT NULL,
                    region TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_forecasts (
                    id           SERIAL PRIMARY KEY,
                    city_id      INTEGER NOT NULL REFERENCES cities(id),
                    run_time     TEXT NOT NULL,
                    valid_date   TEXT NOT NULL,
                    temp_mean    FLOAT,
                    temp_min     FLOAT,
                    temp_max     FLOAT,
                    temp_p25     FLOAT,
                    temp_p50     FLOAT,
                    temp_p75     FLOAT,
                    tmax_mean    FLOAT,
                    tmax_p10     FLOAT,
                    tmax_p90     FLOAT,
                    tmin_mean    FLOAT,
                    tmin_p10     FLOAT,
                    tmin_p90     FLOAT,
                    precip_prob  FLOAT,
                    precip_mean  FLOAT,
                    precip_p75   FLOAT,
                    precip_p90   FLOAT,
                    member_count INTEGER,
                    created_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE(city_id, run_time, valid_date)
                );

                CREATE TABLE IF NOT EXISTS hourly_forecasts (
                    id           SERIAL PRIMARY KEY,
                    city_id      INTEGER NOT NULL REFERENCES cities(id),
                    run_time     TEXT NOT NULL,
                    valid_time   TEXT NOT NULL,
                    t_p10        FLOAT,
                    t_p25        FLOAT,
                    t_p50        FLOAT,
                    t_p75        FLOAT,
                    t_p90        FLOAT,
                    precip_prob  FLOAT,
                    precip_mean  FLOAT,
                    created_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE(city_id, run_time, valid_time)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_city_run
                    ON daily_forecasts(city_id, run_time DESC);
                CREATE INDEX IF NOT EXISTS idx_hourly_city_run
                    ON hourly_forecasts(city_id, run_time DESC);
            """)
        conn.commit()
        logger.info("Base PostgreSQL initialisée")
    finally:
        conn.close()


# ── Villes ────────────────────────────────────────────────────────────────────

def upsert_city(name: str, lat: float, lon: float, region: str) -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO cities(name, lat, lon, region) VALUES(%s,%s,%s,%s)
                   ON CONFLICT(name) DO UPDATE
                   SET lat=EXCLUDED.lat, lon=EXCLUDED.lon, region=EXCLUDED.region
                   RETURNING *""",
                (name, lat, lon, region),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def get_city_by_name(name: str) -> Optional[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cities WHERE LOWER(name)=LOWER(%s)", (name,))
            row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_cities() -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cities ORDER BY name")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Prévisions journalières ───────────────────────────────────────────────────

def upsert_forecast(city_id: int, run_time: str, valid_date: str, stats: dict) -> None:
    cols = ["city_id", "run_time", "valid_date"] + list(stats.keys())
    vals = [city_id, run_time, valid_date] + list(stats.values())
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in stats.keys())
    sql = (
        f"INSERT INTO daily_forecasts({', '.join(cols)}) VALUES({placeholders}) "
        f"ON CONFLICT(city_id, run_time, valid_date) DO UPDATE SET {updates}"
    )
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()
    finally:
        conn.close()


def get_forecast(city_id: int, limit: int = 16) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_time FROM daily_forecasts WHERE city_id=%s ORDER BY run_time DESC LIMIT 1",
                (city_id,),
            )
            latest = cur.fetchone()
            if not latest:
                return []
            cur.execute(
                "SELECT * FROM daily_forecasts WHERE city_id=%s AND run_time=%s ORDER BY valid_date LIMIT %s",
                (city_id, latest["run_time"], limit),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Prévisions horaires ───────────────────────────────────────────────────────

def upsert_hourly(city_id: int, run_time: str, rows: list[dict]) -> None:
    """Insère ou remplace toutes les heures d'un run pour une ville (batch)."""
    values = [
        (city_id, run_time, r["time"],
         r["t_p10"], r["t_p25"], r["t_p50"], r["t_p75"], r["t_p90"],
         r["precip_prob"], r["precip_mean"])
        for r in rows
    ]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO hourly_forecasts
                (city_id, run_time, valid_time, t_p10, t_p25, t_p50, t_p75, t_p90, precip_prob, precip_mean)
                VALUES %s
                ON CONFLICT(city_id, run_time, valid_time) DO UPDATE SET
                  t_p10=EXCLUDED.t_p10, t_p25=EXCLUDED.t_p25, t_p50=EXCLUDED.t_p50,
                  t_p75=EXCLUDED.t_p75, t_p90=EXCLUDED.t_p90,
                  precip_prob=EXCLUDED.precip_prob, precip_mean=EXCLUDED.precip_mean
            """, values)
        conn.commit()
    finally:
        conn.close()


def get_hourly_forecast(city_id: int, hours: int = 384) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_time FROM hourly_forecasts WHERE city_id=%s ORDER BY run_time DESC LIMIT 1",
                (city_id,),
            )
            latest = cur.fetchone()
            if not latest:
                return []
            cur.execute(
                "SELECT * FROM hourly_forecasts WHERE city_id=%s AND run_time=%s "
                "ORDER BY valid_time LIMIT %s",
                (city_id, latest["run_time"], hours),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_all_cities_map_data() -> dict:
    """Retourne les données horaires de toutes les villes pour la carte animée."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_time FROM hourly_forecasts ORDER BY run_time DESC LIMIT 1"
            )
            latest = cur.fetchone()
            if not latest:
                return {}
            run_time = latest["run_time"]

            cur.execute("SELECT * FROM cities ORDER BY name")
            cities = cur.fetchall()

            result = {"run_time": run_time, "times": [], "cities": []}

            for city in cities:
                cur.execute(
                    """SELECT valid_time, t_p10, t_p25, t_p50, t_p75, t_p90, precip_prob, precip_mean
                       FROM hourly_forecasts WHERE city_id=%s AND run_time=%s ORDER BY valid_time""",
                    (city["id"], run_time),
                )
                rows = cur.fetchall()
                if not rows:
                    continue

                if not result["times"]:
                    result["times"] = [r["valid_time"] for r in rows]

                result["cities"].append({
                    "name":        city["name"],
                    "lat":         city["lat"],
                    "lon":         city["lon"],
                    "region":      city["region"],
                    "t_p10":       [r["t_p10"]       for r in rows],
                    "t_p25":       [r["t_p25"]       for r in rows],
                    "t_p50":       [r["t_p50"]       for r in rows],
                    "t_p75":       [r["t_p75"]       for r in rows],
                    "t_p90":       [r["t_p90"]       for r in rows],
                    "precip_prob": [r["precip_prob"] for r in rows],
                    "precip_mean": [r["precip_mean"] for r in rows],
                })

        return result
    finally:
        conn.close()
