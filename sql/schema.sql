-- ============================================================
-- Schéma Supabase — Météo Probabiliste
-- À exécuter dans l'éditeur SQL de Supabase
-- ============================================================

-- Table des villes supportées
CREATE TABLE IF NOT EXISTS cities (
    id      SERIAL PRIMARY KEY,
    name    VARCHAR(100) NOT NULL UNIQUE,
    lat     REAL NOT NULL,
    lon     REAL NOT NULL,
    region  VARCHAR(100)
);

-- Table des prévisions journalières (statistiques d'ensemble pré-calculées)
-- Une ligne = une ville × un run GEFS × un jour de validité
CREATE TABLE IF NOT EXISTS daily_forecasts (
    id           BIGSERIAL PRIMARY KEY,
    city_id      INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    run_time     TIMESTAMPTZ NOT NULL,   -- Heure de départ du modèle GEFS (ex: 2024-01-15T00:00:00Z)
    valid_date   DATE NOT NULL,          -- Date de la prévision
    -- Température (°C) — statistiques sur l'ensemble des membres
    temp_mean    REAL,   -- Moyenne
    temp_min     REAL,   -- Percentile 10 (queue froide)
    temp_max     REAL,   -- Percentile 90 (queue chaude)
    temp_p25     REAL,
    temp_p50     REAL,
    temp_p75     REAL,
    -- Précipitations (mm/jour)
    precip_prob  REAL,   -- Fraction de membres avec pluie > seuil (0.0 à 1.0)
    precip_mean  REAL,   -- Cumul moyen
    precip_p75   REAL,
    precip_p90   REAL,
    member_count INTEGER, -- Nombre de membres ayant fourni des données valides
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(city_id, run_time, valid_date)
);

-- Index pour accélérer les requêtes fréquentes
CREATE INDEX IF NOT EXISTS idx_forecasts_city_run
    ON daily_forecasts(city_id, run_time DESC);
CREATE INDEX IF NOT EXISTS idx_forecasts_valid_date
    ON daily_forecasts(valid_date);

-- ────────────────────────────────────────────────────────────
-- Fonction RPC : trouve la ville la plus proche d'un point
-- Utilisé par l'API quand l'utilisateur fournit lat/lon
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION find_nearest_city(p_lat REAL, p_lon REAL)
RETURNS TABLE(id INTEGER, name VARCHAR, lat REAL, lon REAL, region VARCHAR)
LANGUAGE SQL STABLE AS $$
    SELECT id, name, lat, lon, region
    FROM cities
    ORDER BY (lat - p_lat)^2 + (lon - p_lon)^2  -- distance euclidienne (approx. suffisante)
    LIMIT 1;
$$;

-- ────────────────────────────────────────────────────────────
-- Vue : dernière prévision disponible par ville
-- Commode pour les tableaux de bord
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW latest_forecasts AS
SELECT DISTINCT ON (df.city_id, df.valid_date)
    c.name       AS city_name,
    c.region,
    df.run_time,
    df.valid_date,
    df.temp_mean,
    df.temp_min,
    df.temp_max,
    df.precip_prob,
    df.precip_mean,
    df.member_count
FROM daily_forecasts df
JOIN cities c ON c.id = df.city_id
ORDER BY df.city_id, df.valid_date, df.run_time DESC;
