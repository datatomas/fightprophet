#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Gold Layer ETL: Materialize dashboard-serving Parquet datasets.

Reads from DuckDB gold tables and writes pre-computed Parquet files that the
Streamlit front-end reads directly — no DuckDB dependency in the front container.

Supports writing to:
  - Local filesystem (default)
  - Azure Blob Storage (access-key auth via env vars or CLI)
  - Optional local cache when writing to Azure (dual-write)

Datasets produced:
    dashboard_upcoming_cards/
    dashboard_upcoming_events/
    dashboard_hist_historical_all/
    dashboard_calibration_buckets/
    dashboard_model_stats/

Each write creates a new versioned sub-folder:
    <dataset>/export_version=YYYYMMDD_HHMMSS/<files>.parquet

After all files land, a LATEST.json is written atomically so readers can
discover the freshest snapshot without listing directories.

Usage (local):
    source bootstrap_scripts/envloader.sh
    python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \\
        --target dev --dataset all

    # dry-run
    python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \\
        --target dev --dataset all --dry-run

Usage (Azure Blob Storage):
    export AZURE_STORAGE_ACCOUNT="youraccount"
    export AZURE_STORAGE_KEY="your-key"
    export AZURE_STORAGE_CONTAINER="fightprophet-dashboard"  # optional

    python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \\
        --target prod --dataset all

    # with local cache (writes to both Azure and local)
    python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \\
        --target prod --dataset all \\
        --local-cache /home/ares/db/duck/warehouse/lake

Docker / scheduled job:
    ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard
    ETL_ARGS="--target prod --dataset all"
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from ml_kuda_sports_lab.etl.gold import mma_gold_fighter_status as fighter_status_mod

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATASET_NAMES = (
    "upcoming",
    "upcoming_ensemble",
    "upcoming_catboost",
    "upcoming_logreg",
    "events",
    "historical",
    "historical_ensemble",
    "historical_catboost",
    "historical_logreg",
    "calibration",
    "calibration_ensemble",
    "calibration_catboost",
    "calibration_logreg",
    "stats",
    "stats_ensemble",
    "stats_catboost",
    "stats_logreg",
    "rankings",
    "overall_rankings",
    "fighter_profiles",
    "fighter_history",
    "belt_holders",
    "title_fight_history",
    "manual_title_vacates",
)
ALL_ALIAS = "all"
HOMEPAGE_RANKINGS_WALL_FILENAME = "rankings_wall.json"
DIVISION_ORDER = [
    "Flyweight",
    "Bantamweight",
    "Featherweight",
    "Lightweight",
    "Welterweight",
    "Middleweight",
    "Light Heavyweight",
    "Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
]

# ---------------------------------------------------------------------------
# SQL – exactly the queries the Streamlit app needs
# ---------------------------------------------------------------------------
SQL_UPCOMING_CARDS = """
SELECT
    fighter_name_display,
    opponent_name_display,
    mfc_f.country                                 AS fighter_country,
    mfc_o.country                                 AS opponent_country,
    event_date::DATE                            AS event_date,
    event_name,
    location,
    ROUND(ensemble_fighter_win_prob_model, 3)   AS model_prob,
    ROUND(market_implied_fighter_prob, 3)        AS market_prob,
    ROUND(betting_edge_odds, 3)                  AS edge,
    signal_strength,
    recommended_bet,
    bet_on_name,
    fighter_odds,
    opponent_odds,
    -- canonical active/inactive (gold.fighter_effective_status). Same source
    -- the rankings/profiles datasets use, so streamlit fight cards and astro
    -- rankings can never disagree on a fighter's status.
    es_f.fighter_status                          AS fighter_status,
    es_o.fighter_status                          AS opponent_status,
    -- partition cols derived from event_date
    YEAR(event_date::DATE)                       AS event_year,
    MONTH(event_date::DATE)                      AS event_month
FROM gold.prefight_features_scored_upcoming p
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = p.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = p.opponent_id
LEFT JOIN gold.fighter_effective_status es_f
    ON es_f.fighter_id = p.fighter_id
LEFT JOIN gold.fighter_effective_status es_o
    ON es_o.fighter_id = p.opponent_id
"""

SQL_UPCOMING_CARDS_ENSEMBLE = """
SELECT
    fighter_name_display,
    opponent_name_display,
    mfc_f.country                                 AS fighter_country,
    mfc_o.country                                 AS opponent_country,
    event_date::DATE                                        AS event_date,
    event_name,
    location,
    ROUND(COALESCE(ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 3) AS model_prob,
    ROUND(market_implied_fighter_prob, 3)                  AS market_prob,
    ROUND(COALESCE(betting_edge_model, betting_edge, betting_edge_odds), 3) AS edge,
    COALESCE(signal_strength_model, signal_strength)       AS signal_strength,
    COALESCE(recommended_bet_model, recommended_bet)       AS recommended_bet,
    COALESCE(bet_on_name_model, bet_on_name)               AS bet_on_name,
    fighter_odds,
    opponent_odds,
    es_f.fighter_status                                    AS fighter_status,
    es_o.fighter_status                                    AS opponent_status,
    YEAR(event_date::DATE)                                 AS event_year,
    MONTH(event_date::DATE)                                AS event_month
FROM gold.prefight_features_scored_upcoming p
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = p.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = p.opponent_id
LEFT JOIN gold.fighter_effective_status es_f
    ON es_f.fighter_id = p.fighter_id
LEFT JOIN gold.fighter_effective_status es_o
    ON es_o.fighter_id = p.opponent_id
"""

SQL_UPCOMING_CARDS_CATBOOST = """
SELECT
    fighter_name_display,
    opponent_name_display,
    mfc_f.country                                 AS fighter_country,
    mfc_o.country                                 AS opponent_country,
    event_date::DATE                                        AS event_date,
    event_name,
    location,
    ROUND(COALESCE(catboost_fighter_win_prob_model, catboost_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 3) AS model_prob,
    ROUND(market_implied_fighter_prob, 3)                  AS market_prob,
    ROUND(COALESCE(betting_edge_catboost_model, betting_edge_model, betting_edge, betting_edge_odds), 3) AS edge,
    COALESCE(signal_strength_catboost_model, signal_strength_model, signal_strength) AS signal_strength,
    COALESCE(recommended_bet_catboost_model, recommended_bet_model, recommended_bet) AS recommended_bet,
    COALESCE(bet_on_name_catboost_model, bet_on_name_model, bet_on_name) AS bet_on_name,
    fighter_odds,
    opponent_odds,
    es_f.fighter_status                                    AS fighter_status,
    es_o.fighter_status                                    AS opponent_status,
    YEAR(event_date::DATE)                                 AS event_year,
    MONTH(event_date::DATE)                                AS event_month
FROM gold.prefight_features_scored_upcoming p
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = p.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = p.opponent_id
LEFT JOIN gold.fighter_effective_status es_f
    ON es_f.fighter_id = p.fighter_id
LEFT JOIN gold.fighter_effective_status es_o
    ON es_o.fighter_id = p.opponent_id
"""

SQL_UPCOMING_CARDS_LOGREG = """
SELECT
    fighter_name_display,
    opponent_name_display,
    mfc_f.country                                 AS fighter_country,
    mfc_o.country                                 AS opponent_country,
    event_date::DATE                                        AS event_date,
    event_name,
    location,
    ROUND(COALESCE(logistic_fighter_win_prob_model, logistic_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 3) AS model_prob,
    ROUND(market_implied_fighter_prob, 3)                  AS market_prob,
    ROUND(COALESCE(betting_edge_logreg_model, betting_edge_model, betting_edge, betting_edge_odds), 3) AS edge,
    COALESCE(signal_strength_logreg_model, signal_strength_model, signal_strength) AS signal_strength,
    COALESCE(recommended_bet_logreg_model, recommended_bet_model, recommended_bet) AS recommended_bet,
    COALESCE(bet_on_name_logreg_model, bet_on_name_model, bet_on_name) AS bet_on_name,
    fighter_odds,
    opponent_odds,
    es_f.fighter_status                                    AS fighter_status,
    es_o.fighter_status                                    AS opponent_status,
    YEAR(event_date::DATE)                                 AS event_year,
    MONTH(event_date::DATE)                                AS event_month
FROM gold.prefight_features_scored_upcoming p
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = p.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = p.opponent_id
LEFT JOIN gold.fighter_effective_status es_f
    ON es_f.fighter_id = p.fighter_id
LEFT JOIN gold.fighter_effective_status es_o
    ON es_o.fighter_id = p.opponent_id
"""

SQL_UPCOMING_EVENTS = """
SELECT DISTINCT
    event_name,
    event_date::DATE    AS event_date,
    location,
    YEAR(event_date::DATE)  AS event_year,
    MONTH(event_date::DATE) AS event_month
FROM silver.events
WHERE event_name IS NOT NULL
"""

SQL_HIST_HISTORICAL_ALL = """
SELECT
    fighter_name_display,
    opponent_name_display,
    mfc_f.country                                 AS fighter_country,
    mfc_o.country                                 AS opponent_country,
    event_date::DATE                            AS event_date,
    event_name,
    location,
    result,
    winner_name_display,
    bet_on_name,
    CASE WHEN winner_name_display = bet_on_name THEN 1 ELSE 0 END AS model_correct,
    ROUND(ensemble_fighter_win_prob_model, 3)   AS model_prob,
    ROUND(market_implied_fighter_prob, 3)        AS market_prob,
    ROUND(betting_edge_odds, 3)                  AS edge,
    signal_strength,
    recommended_bet,
    YEAR(event_date::DATE)                       AS event_year,
    MONTH(event_date::DATE)                      AS event_month
FROM gold.prefight_features_scored p
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = p.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = p.opponent_id
WHERE (
    COALESCE(NULLIF(TRIM(CAST(result AS VARCHAR)), ''), NULL) IS NOT NULL
    OR (
        event_date::DATE < CURRENT_DATE
        AND COALESCE(NULLIF(TRIM(CAST(winner_name_display AS VARCHAR)), ''), NULL) IS NOT NULL
        AND LOWER(TRIM(CAST(winner_name_display AS VARCHAR))) NOT IN ('nan', 'nat', 'none', 'draw', 'no contest')
    )
)
"""

SQL_HIST_HISTORICAL_ALL_ENSEMBLE = """
SELECT
    fighter_name_display,
    opponent_name_display,
    fighter_country,
    opponent_country,
    event_date::DATE                            AS event_date,
    event_name,
    location,
    result,
    winner_name_display,
    bet_on_name,
    CASE WHEN winner_name_display = bet_on_name THEN 1 ELSE 0 END AS model_correct,
    ROUND(model_prob_raw, 3)                    AS model_prob,
    ROUND(market_implied_fighter_prob, 3)       AS market_prob,
    ROUND(edge_raw, 3)                          AS edge,
    signal_strength,
    recommended_bet,
    YEAR(event_date::DATE)                      AS event_year,
    MONTH(event_date::DATE)                     AS event_month
FROM (
    SELECT
        fighter_name_display,
        opponent_name_display,
        mfc_f.country AS fighter_country,
        mfc_o.country AS opponent_country,
        event_date,
        event_name,
        location,
        result,
        winner_name_display,
        COALESCE(bet_on_name_model, bet_on_name) AS bet_on_name,
        COALESCE(ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob) AS model_prob_raw,
        market_implied_fighter_prob,
        COALESCE(betting_edge_model, betting_edge, betting_edge_odds) AS edge_raw,
        COALESCE(signal_strength_model, signal_strength) AS signal_strength,
        COALESCE(recommended_bet_model, recommended_bet) AS recommended_bet
    FROM gold.prefight_features_scored p
    LEFT JOIN gold.manual_fighter_countries mfc_f
        ON mfc_f.fighter_id = p.fighter_id
    LEFT JOIN gold.manual_fighter_countries mfc_o
        ON mfc_o.fighter_id = p.opponent_id
    WHERE (
        COALESCE(NULLIF(TRIM(CAST(result AS VARCHAR)), ''), NULL) IS NOT NULL
        OR (
            event_date::DATE < CURRENT_DATE
            AND COALESCE(NULLIF(TRIM(CAST(winner_name_display AS VARCHAR)), ''), NULL) IS NOT NULL
            AND LOWER(TRIM(CAST(winner_name_display AS VARCHAR))) NOT IN ('nan', 'nat', 'none', 'draw', 'no contest')
        )
    )
) t
"""

SQL_HIST_HISTORICAL_ALL_CATBOOST = """
SELECT
    fighter_name_display,
    opponent_name_display,
    fighter_country,
    opponent_country,
    event_date::DATE                            AS event_date,
    event_name,
    location,
    result,
    winner_name_display,
    bet_on_name,
    CASE WHEN winner_name_display = bet_on_name THEN 1 ELSE 0 END AS model_correct,
    ROUND(model_prob_raw, 3)                    AS model_prob,
    ROUND(market_implied_fighter_prob, 3)       AS market_prob,
    ROUND(edge_raw, 3)                          AS edge,
    signal_strength,
    recommended_bet,
    YEAR(event_date::DATE)                      AS event_year,
    MONTH(event_date::DATE)                     AS event_month
FROM (
    SELECT
        fighter_name_display,
        opponent_name_display,
        mfc_f.country AS fighter_country,
        mfc_o.country AS opponent_country,
        event_date,
        event_name,
        location,
        result,
        winner_name_display,
        COALESCE(bet_on_name_catboost_model, bet_on_name_model, bet_on_name) AS bet_on_name,
        COALESCE(catboost_fighter_win_prob_model, catboost_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob) AS model_prob_raw,
        market_implied_fighter_prob,
        COALESCE(betting_edge_catboost_model, betting_edge_model, betting_edge, betting_edge_odds) AS edge_raw,
        COALESCE(signal_strength_catboost_model, signal_strength_model, signal_strength) AS signal_strength,
        COALESCE(recommended_bet_catboost_model, recommended_bet_model, recommended_bet) AS recommended_bet
    FROM gold.prefight_features_scored p
    LEFT JOIN gold.manual_fighter_countries mfc_f
        ON mfc_f.fighter_id = p.fighter_id
    LEFT JOIN gold.manual_fighter_countries mfc_o
        ON mfc_o.fighter_id = p.opponent_id
    WHERE (
        COALESCE(NULLIF(TRIM(CAST(result AS VARCHAR)), ''), NULL) IS NOT NULL
        OR (
            event_date::DATE < CURRENT_DATE
            AND COALESCE(NULLIF(TRIM(CAST(winner_name_display AS VARCHAR)), ''), NULL) IS NOT NULL
            AND LOWER(TRIM(CAST(winner_name_display AS VARCHAR))) NOT IN ('nan', 'nat', 'none', 'draw', 'no contest')
        )
    )
) t
"""

SQL_HIST_HISTORICAL_ALL_LOGREG = """
SELECT
    fighter_name_display,
    opponent_name_display,
    fighter_country,
    opponent_country,
    event_date::DATE                            AS event_date,
    event_name,
    location,
    result,
    winner_name_display,
    bet_on_name,
    CASE WHEN winner_name_display = bet_on_name THEN 1 ELSE 0 END AS model_correct,
    ROUND(model_prob_raw, 3)                    AS model_prob,
    ROUND(market_implied_fighter_prob, 3)       AS market_prob,
    ROUND(edge_raw, 3)                          AS edge,
    signal_strength,
    recommended_bet,
    YEAR(event_date::DATE)                      AS event_year,
    MONTH(event_date::DATE)                     AS event_month
FROM (
    SELECT
        fighter_name_display,
        opponent_name_display,
        mfc_f.country AS fighter_country,
        mfc_o.country AS opponent_country,
        event_date,
        event_name,
        location,
        result,
        winner_name_display,
        COALESCE(bet_on_name_logreg_model, bet_on_name_model, bet_on_name) AS bet_on_name,
        COALESCE(logistic_fighter_win_prob_model, logistic_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob) AS model_prob_raw,
        market_implied_fighter_prob,
        COALESCE(betting_edge_logreg_model, betting_edge_model, betting_edge, betting_edge_odds) AS edge_raw,
        COALESCE(signal_strength_logreg_model, signal_strength_model, signal_strength) AS signal_strength,
        COALESCE(recommended_bet_logreg_model, recommended_bet_model, recommended_bet) AS recommended_bet
    FROM gold.prefight_features_scored p
    LEFT JOIN gold.manual_fighter_countries mfc_f
        ON mfc_f.fighter_id = p.fighter_id
    LEFT JOIN gold.manual_fighter_countries mfc_o
        ON mfc_o.fighter_id = p.opponent_id
    WHERE (
        COALESCE(NULLIF(TRIM(CAST(result AS VARCHAR)), ''), NULL) IS NOT NULL
        OR (
            event_date::DATE < CURRENT_DATE
            AND COALESCE(NULLIF(TRIM(CAST(winner_name_display AS VARCHAR)), ''), NULL) IS NOT NULL
            AND LOWER(TRIM(CAST(winner_name_display AS VARCHAR))) NOT IN ('nan', 'nat', 'none', 'draw', 'no contest')
        )
    )
) t
"""

SQL_CALIBRATION = """
SELECT
    ROUND(ensemble_fighter_win_prob_model, 1)  AS prob_bucket,
    COUNT(*)                                    AS n_fights,
    ROUND(AVG(CASE WHEN winner_name_display = bet_on_name
                   THEN 1.0 ELSE 0.0 END), 3)  AS actual_hit_rate
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
GROUP BY 1
"""

SQL_CALIBRATION_ENSEMBLE = """
SELECT
    ROUND(COALESCE(ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 1) AS prob_bucket,
    COUNT(*)                                    AS n_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_model, bet_on_name)
                   THEN 1.0 ELSE 0.0 END), 3)  AS actual_hit_rate
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
GROUP BY 1
"""

SQL_CALIBRATION_CATBOOST = """
SELECT
    ROUND(COALESCE(catboost_fighter_win_prob_model, catboost_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 1) AS prob_bucket,
    COUNT(*)                                    AS n_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_catboost_model, bet_on_name_model, bet_on_name)
                   THEN 1.0 ELSE 0.0 END), 3)  AS actual_hit_rate
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
GROUP BY 1
"""

SQL_CALIBRATION_LOGREG = """
SELECT
    ROUND(COALESCE(logistic_fighter_win_prob_model, logistic_fighter_win_prob, ensemble_fighter_win_prob_model, ensemble_model_fighter_win_prob, ensemble_fighter_win_prob), 1) AS prob_bucket,
    COUNT(*)                                    AS n_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_logreg_model, bet_on_name_model, bet_on_name)
                   THEN 1.0 ELSE 0.0 END), 3)  AS actual_hit_rate
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
GROUP BY 1
"""

SQL_MODEL_STATS = """
SELECT
    COUNT(*)                                                                           AS total_fights,
    ROUND(AVG(CASE WHEN winner_name_display = bet_on_name THEN 1.0 ELSE 0.0 END), 3)  AS accuracy,
    COUNT(DISTINCT event_name)                                                         AS events_covered
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
"""

SQL_MODEL_STATS_ENSEMBLE = """
SELECT
    COUNT(*)                                                                                                   AS total_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_model, bet_on_name) THEN 1.0 ELSE 0.0 END), 3)  AS accuracy,
    COUNT(DISTINCT event_name)                                                                                 AS events_covered
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
"""

SQL_MODEL_STATS_CATBOOST = """
SELECT
    COUNT(*)                                                                                                                   AS total_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_catboost_model, bet_on_name_model, bet_on_name) THEN 1.0 ELSE 0.0 END), 3)  AS accuracy,
    COUNT(DISTINCT event_name)                                                                                                 AS events_covered
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
"""

SQL_MODEL_STATS_LOGREG = """
SELECT
    COUNT(*)                                                                                                                 AS total_fights,
    ROUND(AVG(CASE WHEN winner_name_display = COALESCE(bet_on_name_logreg_model, bet_on_name_model, bet_on_name) THEN 1.0 ELSE 0.0 END), 3)  AS accuracy,
    COUNT(DISTINCT event_name)                                                                                               AS events_covered
FROM gold.prefight_features_scored
WHERE result IS NOT NULL
"""

SQL_RANKINGS = """
WITH manual_country_one AS (
    SELECT
        fighter_id,
        any_value(
            NULLIF(TRIM(country), '')
            ORDER BY
                CASE WHEN NULLIF(TRIM(country), '') IS NULL THEN 1 ELSE 0 END,
                country
        ) AS country
    FROM gold.manual_fighter_countries
    WHERE fighter_id IS NOT NULL
    GROUP BY fighter_id
),
effective_status_one AS (
    SELECT fighter_id, fighter_status
    FROM (
        SELECT
            fighter_id,
            fighter_status,
            ROW_NUMBER() OVER (
                PARTITION BY fighter_id
                ORDER BY
                    COALESCE(resolved_at, TIMESTAMP '1900-01-01') DESC,
                    COALESCE(as_of_date, DATE '1900-01-01') DESC
            ) AS rn
        FROM gold.fighter_effective_status
        WHERE fighter_id IS NOT NULL
    ) _status
    WHERE rn = 1
)
SELECT
    r.rank,
    r.global_rank,
    r.fighter_id,
    r.fighter_name,
    mfc.country,
    r.weight_class,
    r.organization,
    ROUND(r.points, 1)             AS points,
    ROUND(r.global_points, 1)      AS global_points,
    ROUND(r.normalized_global_score, 1) AS normalized_global_score,
    r.fights_count,
    r.wins_count,
    r.losses_count,
    r.draws_count,
    r.win_streak,
    r.loss_streak,
    r.title_defenses_count,
    r.has_won_title,
    -- fighter_status comes from gold.fighter_effective_status (canonical, single
    -- source of truth — manual override JSON > 730-day inactivity rule). The
    -- ranking table's own fighter_status column is kept as a fallback only for
    -- rare cases where the canonical table is missing a row.
    COALESCE(es.fighter_status, r.fighter_status) AS fighter_status,
    r.last_fight_date,
    r.as_of_date
FROM gold.mma_rankings r
LEFT JOIN manual_country_one mfc
    ON mfc.fighter_id = r.fighter_id
LEFT JOIN effective_status_one es
    ON es.fighter_id = r.fighter_id
WHERE r.as_of_date = (SELECT MAX(as_of_date) FROM gold.mma_rankings)
ORDER BY r.weight_class, r.rank
"""

SQL_OVERALL_RANKINGS = """
WITH manual_country_one AS (
    SELECT
        fighter_id,
        any_value(
            NULLIF(TRIM(country), '')
            ORDER BY
                CASE WHEN NULLIF(TRIM(country), '') IS NULL THEN 1 ELSE 0 END,
                country
        ) AS country
    FROM gold.manual_fighter_countries
    WHERE fighter_id IS NOT NULL
    GROUP BY fighter_id
),
effective_status_one AS (
    SELECT fighter_id, fighter_status
    FROM (
        SELECT
            fighter_id,
            fighter_status,
            ROW_NUMBER() OVER (
                PARTITION BY fighter_id
                ORDER BY
                    COALESCE(resolved_at, TIMESTAMP '1900-01-01') DESC,
                    COALESCE(as_of_date, DATE '1900-01-01') DESC
            ) AS rn
        FROM gold.fighter_effective_status
        WHERE fighter_id IS NOT NULL
    ) _status
    WHERE rn = 1
)
SELECT
    r.rank AS global_rank,
    r.fighter_id,
    r.fighter_name,
    mfc.country,
    r.organization,
    ROUND(r.points, 1) AS global_points,
    ROUND(r.normalized_global_score, 1) AS normalized_global_score,
    r.fights_count,
    r.wins_count,
    r.losses_count,
    r.draws_count,
    r.win_streak,
    r.loss_streak,
    r.title_defenses_count,
    r.has_won_title,
    COALESCE(es.fighter_status, r.fighter_status) AS fighter_status,
    r.last_fight_date,
    r.as_of_date
FROM gold.mma_overall_rankings r
LEFT JOIN manual_country_one mfc
    ON mfc.fighter_id = r.fighter_id
LEFT JOIN effective_status_one es
    ON es.fighter_id = r.fighter_id
WHERE r.as_of_date = (SELECT MAX(as_of_date) FROM gold.mma_overall_rankings)
ORDER BY r.rank
"""

SQL_FIGHTER_PROFILES = """
WITH fights_base AS (
    SELECT
        sf.organization,
        sf.fighter_id,
        COALESCE(sf.fighter_name_display, sf.fighter_name_plain, sf.fighter_name, sf.fighter_id) AS fighter_name_display,
        sf.event_date::DATE AS event_date,
        sf.result,
        sf.method_category,
        sf.is_title_fight,
        sf.bonus_tags,
        sf.weight_class
    FROM silver.fights sf
    WHERE sf.organization = 'UFC'
      AND sf.fighter_id IS NOT NULL
      AND sf.event_date IS NOT NULL
      AND sf.event_date::DATE <= CURRENT_DATE
),
agg AS (
    SELECT
        organization,
        fighter_id,
        MAX(fighter_name_display) AS fighter_name_display,
        MIN(event_date) AS first_fight_date,
        MAX(event_date) AS last_fight_date,
        COUNT(*) FILTER (WHERE result IN ('win','loss','draw')) AS total_fights,
        SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) AS draws,
                SUM(
                        CASE
                                WHEN lower(coalesce(result, '')) = 'nc'
                                    OR upper(coalesce(method_category, '')) LIKE '%NC%'
                                THEN 1
                                ELSE 0
                        END
                ) AS no_contests,
        SUM(CASE WHEN result = 'win' AND method_category = 'KO_TKO' THEN 1 ELSE 0 END) AS ko_wins,
        SUM(CASE WHEN result = 'win' AND method_category = 'SUB' THEN 1 ELSE 0 END) AS sub_wins,
        SUM(CASE WHEN result = 'win' AND method_category = 'DEC' THEN 1 ELSE 0 END) AS dec_wins,
        SUM(
            CASE
                WHEN COALESCE(NULLIF(TRIM(CAST(bonus_tags AS VARCHAR)), ''), NULL) IS NOT NULL THEN 1
                ELSE 0
            END
        ) AS bonuses_won_count,
        SUM(CASE WHEN is_title_fight THEN 1 ELSE 0 END) AS title_fights,
        COUNT(DISTINCT weight_class) AS weight_classes_fought
    FROM fights_base
    GROUP BY 1, 2
),
fights_ordered AS (
    SELECT
        fb.organization,
        fb.fighter_id,
        fb.event_date,
        lower(coalesce(fb.result, '')) AS result,
        SUM(CASE WHEN lower(coalesce(fb.result, '')) = 'win' THEN 0 ELSE 1 END)
            OVER (
                PARTITION BY fb.organization, fb.fighter_id
                ORDER BY fb.event_date DESC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS win_break_group,
        SUM(CASE WHEN lower(coalesce(fb.result, '')) = 'loss' THEN 0 ELSE 1 END)
            OVER (
                PARTITION BY fb.organization, fb.fighter_id
                ORDER BY fb.event_date DESC
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS loss_break_group
    FROM fights_base fb
    WHERE lower(coalesce(fb.result, '')) IN ('win', 'loss', 'draw')
),
streaks AS (
    SELECT
        fo.organization,
        fo.fighter_id,
        SUM(CASE WHEN fo.result = 'win' AND fo.win_break_group = 0 THEN 1 ELSE 0 END) AS win_streak,
        SUM(CASE WHEN fo.result = 'loss' AND fo.loss_break_group = 0 THEN 1 ELSE 0 END) AS loss_streak
    FROM fights_ordered fo
    GROUP BY 1, 2
),
streak_groups AS (
    SELECT
        fb.organization,
        fb.fighter_id,
        lower(coalesce(fb.result, '')) AS result,
        fb.event_date,
        ROW_NUMBER() OVER (
            PARTITION BY fb.organization, fb.fighter_id
            ORDER BY fb.event_date
        ) AS seq_all,
        ROW_NUMBER() OVER (
            PARTITION BY fb.organization, fb.fighter_id, lower(coalesce(fb.result, ''))
            ORDER BY fb.event_date
        ) AS seq_result
    FROM fights_base fb
    WHERE lower(coalesce(fb.result, '')) IN ('win', 'loss')
),
streak_runs AS (
    SELECT
        organization,
        fighter_id,
        result,
        COUNT(*) AS streak_len
    FROM streak_groups
    GROUP BY organization, fighter_id, result, (seq_all - seq_result)
),
longest_streaks AS (
    SELECT
        organization,
        fighter_id,
        MAX(CASE WHEN result = 'win' THEN streak_len ELSE 0 END) AS longest_win_streak,
        MAX(CASE WHEN result = 'loss' THEN streak_len ELSE 0 END) AS longest_loss_streak
    FROM streak_runs
    GROUP BY 1, 2
),
title_fights_all AS (
    SELECT
        fb.organization,
        fb.fighter_id,
        fb.event_date,
        lower(coalesce(fb.result, '')) AS result_norm,
        TRIM(CAST(fb.weight_class AS VARCHAR)) AS weight_class_raw,
        CASE
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), 'women.?s?\\s+strawweight') THEN 'women strawweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), 'women.?s?\\s+flyweight') THEN 'women flyweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), 'women.?s?\\s+bantamweight') THEN 'women bantamweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), 'women.?s?\\s+featherweight') THEN 'women featherweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\blight\\s+heavyweight\\b') THEN 'light heavyweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bheavyweight\\b') THEN 'heavyweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bmiddleweight\\b') THEN 'middleweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bwelterweight\\b') THEN 'welterweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\blightweight\\b') THEN 'lightweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bfeatherweight\\b') THEN 'featherweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bbantamweight\\b') THEN 'bantamweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bflyweight\\b') THEN 'flyweight'
            WHEN regexp_matches(lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' ')), '\\bstrawweight\\b') THEN 'strawweight'
            ELSE lower(regexp_replace(TRIM(CAST(fb.weight_class AS VARCHAR)), '\\s+', ' '))
        END AS weight_class_key
    FROM fights_base fb
    WHERE (
            fb.is_title_fight = TRUE
            OR lower(TRIM(CAST(fb.is_title_fight AS VARCHAR))) IN ('1', 'true', 't', 'yes', 'y')
        )
      AND COALESCE(NULLIF(TRIM(CAST(fb.weight_class AS VARCHAR)), ''), NULL) IS NOT NULL
),
latest_title_fight_dates AS (
    SELECT
        tfa.organization,
        tfa.weight_class_key,
        MAX(tfa.event_date) AS latest_title_fight_date
    FROM title_fights_all tfa
    WHERE tfa.event_date <= CURRENT_DATE
      AND tfa.result_norm IN ('win', 'w', 'winner')
    GROUP BY 1, 2
),
latest_title_winners AS (
    SELECT
        tfa.organization,
        tfa.fighter_id,
        tfa.weight_class_raw,
        tfa.weight_class_key,
        tfa.event_date
    FROM title_fights_all tfa
    INNER JOIN latest_title_fight_dates ltd
        ON ltd.organization = tfa.organization
       AND ltd.weight_class_key = tfa.weight_class_key
       AND ltd.latest_title_fight_date = tfa.event_date
    WHERE tfa.result_norm IN ('win', 'w', 'winner')
),
current_belt_holders AS (
    SELECT
        'UFC' AS organization,
        bh.champion_fighter_id AS fighter_id,
        COUNT(DISTINCT bh.weight_class) AS current_belts_count,
        string_agg(DISTINCT bh.weight_class, ', ' ORDER BY bh.weight_class) AS current_belt_weight_classes,
        MAX(bh.title_won_date) AS latest_title_win_date
    FROM gold.belt_holders bh
    WHERE COALESCE(bh.is_vacant, FALSE) = FALSE
      AND COALESCE(NULLIF(TRIM(CAST(bh.champion_fighter_id AS VARCHAR)), ''), NULL) IS NOT NULL
    GROUP BY 1, 2
),
prefight_latest AS (
    SELECT
        pf.fighter_id,
        pf.ko_win_rate_shrunk,
        pf.sub_win_rate_shrunk,
        pf.finish_win_rate_shrunk,
        pf.fights_count,
        ROW_NUMBER() OVER (
            PARTITION BY pf.fighter_id
            ORDER BY pf.event_date DESC NULLS LAST
        ) AS rn
    FROM gold.prefight_features pf
    WHERE pf.organization = 'UFC'
      AND pf.fighter_id IS NOT NULL
      AND pf.event_date IS NOT NULL
),
prefight_summary AS (
    SELECT
        fighter_id,
        ko_win_rate_shrunk,
        sub_win_rate_shrunk,
        finish_win_rate_shrunk,
        fights_count
    FROM prefight_latest
    WHERE rn = 1
),
canonical_status AS (
    -- Single source of truth for active/inactive: gold.fighter_effective_status
    -- already merged the manual override JSON with the inactivity rule. Don't
    -- re-derive here; just pass it through.
    SELECT
        fighter_id,
        fighter_status
    FROM gold.fighter_effective_status
    WHERE fighter_id IS NOT NULL
      AND fighter_status IN ('active', 'inactive')
)
SELECT
    a.organization,
    a.fighter_id,
    a.fighter_name_display,
    f.fighter_name,
    f.fighter_name_plain,
    f.dob,
    f.stance,
    f.reach,
    f.weight,
    f.height,
    f.belt,
    COALESCE(cb.current_belts_count, 0) AS current_belts_count,
    COALESCE(cb.current_belts_count, 0) > 0 AS is_current_champion,
    cb.current_belt_weight_classes,
    cb.latest_title_win_date,
    f.age,
    COALESCE(
        cs.fighter_status,
        -- Last-resort fallback for fighters never written to the canonical table
        -- (shouldn't happen once gold.fighter_effective_status is built, but we
        -- keep silver/inactivity rule as a safety net rather than emitting NULL).
        CASE
            WHEN lower(trim(CAST(f.fighter_status AS VARCHAR))) IN ('active', 'inactive')
                THEN lower(trim(CAST(f.fighter_status AS VARCHAR)))
            WHEN a.last_fight_date IS NULL THEN 'active'
            WHEN date_diff('day', a.last_fight_date, CURRENT_DATE) > 730 THEN 'inactive'
            ELSE 'active'
        END
    ) AS fighter_status,
    mfc.country,
    fs.slpm,
    fs.str_acc,
    fs.sapm,
    fs.str_def,
    fs.td_avg,
    fs.td_acc,
    fs.td_def,
    fs.sub_avg,
    COALESCE(ps.ko_win_rate_shrunk, fs.ko_rate_win_shrunk) AS ko_rate_win_shrunk,
    COALESCE(ps.sub_win_rate_shrunk, fs.sub_rate_win_shrunk) AS sub_rate_win_shrunk,
    COALESCE(ps.finish_win_rate_shrunk, fs.finish_rate_win_shrunk) AS finish_rate_win_shrunk,
    COALESCE(ps.fights_count, fs.wins_method_known_count) AS wins_method_known_count,
    a.first_fight_date,
    a.last_fight_date,
    a.total_fights,
    COALESCE(f.wins, a.wins) AS wins,
    COALESCE(f.losses, a.losses) AS losses,
    COALESCE(f.draws, a.draws) AS draws,
    a.no_contests,
    a.bonuses_won_count,
    a.ko_wins,
    a.sub_wins,
    a.dec_wins,
    COALESCE(st.win_streak, 0) AS win_streak,
    COALESCE(st.loss_streak, 0) AS loss_streak,
    COALESCE(ls.longest_win_streak, 0) AS longest_win_streak,
    COALESCE(ls.longest_loss_streak, 0) AS longest_loss_streak,
    a.title_fights,
    a.weight_classes_fought,
    ROUND(
        CASE
            WHEN a.total_fights > 0 THEN CAST(a.wins AS DOUBLE) / a.total_fights
            ELSE NULL
        END,
        3
    ) AS win_rate,
    ROUND(
        CASE
            WHEN a.wins > 0 THEN CAST(a.ko_wins + a.sub_wins AS DOUBLE) / a.wins
            ELSE NULL
        END,
        3
    ) AS finish_rate
FROM agg a
LEFT JOIN silver.fighters f
  ON f.organization = a.organization
 AND f.fighter_id = a.fighter_id
LEFT JOIN silver.fighter_stats fs
    ON fs.organization = a.organization
 AND fs.fighter_id = a.fighter_id
LEFT JOIN prefight_summary ps
    ON ps.fighter_id = a.fighter_id
LEFT JOIN canonical_status cs
    ON cs.fighter_id = a.fighter_id
LEFT JOIN streaks st
    ON st.organization = a.organization
 AND st.fighter_id = a.fighter_id
LEFT JOIN longest_streaks ls
        ON ls.organization = a.organization
 AND ls.fighter_id = a.fighter_id
LEFT JOIN current_belt_holders cb
    ON cb.organization = a.organization
 AND cb.fighter_id = a.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc
    ON mfc.fighter_id = a.fighter_id
ORDER BY a.fighter_name_display
"""

SQL_FIGHTER_HISTORY = """
SELECT
    sf.organization,
    sf.fighter_id,
    COALESCE(sf.fighter_name_display, sf.fighter_name_plain, sf.fighter_name, sf.fighter_id) AS fighter_name_display,
    mfc_f.country AS fighter_country,
    sf.opponent_id,
    COALESCE(sf.opponent_name_display, sf.opponent_name_plain, sf.opponent_name, sf.opponent_id) AS opponent_name_display,
    mfc_o.country AS opponent_country,
    sf.event_date::DATE AS event_date,
    sf.event_name,
    sf.weight_class,
    sf.is_title_fight,
    sf.result,
    CASE
        WHEN lower(COALESCE(sf.result, '')) = 'win' THEN COALESCE(sf.fighter_name_display, sf.fighter_name_plain, sf.fighter_name, sf.fighter_id)
        WHEN lower(COALESCE(sf.result, '')) = 'loss' THEN COALESCE(sf.opponent_name_display, sf.opponent_name_plain, sf.opponent_name, sf.opponent_id)
        WHEN lower(COALESCE(sf.result, '')) = 'draw' THEN 'Draw'
        ELSE 'No Contest'
    END AS winner_name_display,
    sf.method,
    sf.method_category,
    sf.round,
    sf.time,
    sf.kd_for,
    sf.str_for,
    sf.td_for,
    sf.sub_for,
    sf.kd_against,
    sf.str_against,
    sf.td_against,
    sf.sub_against,
    YEAR(sf.event_date::DATE) AS event_year,
    MONTH(sf.event_date::DATE) AS event_month
FROM silver.fights sf
LEFT JOIN gold.manual_fighter_countries mfc_f
        ON mfc_f.fighter_id = sf.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
        ON mfc_o.fighter_id = sf.opponent_id
WHERE sf.organization = 'UFC'
  AND sf.fighter_id IS NOT NULL
  AND sf.event_date IS NOT NULL
"""

SQL_BELT_HOLDERS = """
SELECT
    bh.weight_class,
    bh.champion_fighter_id,
    bh.champion_fighter_name,
    mfc.country AS champion_country,
    bh.title_won_date,
    bh.title_won_event,
    bh.title_defenses,
    bh.last_title_fight_date,
    bh.is_vacant,
    bh.computed_at
FROM gold.belt_holders bh
LEFT JOIN gold.manual_fighter_countries mfc
    ON mfc.fighter_id = bh.champion_fighter_id
ORDER BY bh.weight_class
"""

SQL_TITLE_FIGHT_HISTORY = """
SELECT
    tfh.event_date,
    tfh.event_name,
    tfh.weight_class,
    tfh.champion_before_name,
    tfh.fighter_name,
    mfc_f.country AS fighter_country,
    tfh.opponent_name,
    mfc_o.country AS opponent_country,
    tfh.winner_name,
    mfc_w.country AS winner_country,
    tfh.loser_name,
    mfc_l.country AS loser_country,
    tfh.winner_id,
    tfh.loser_id,
    tfh.fighter_id,
    tfh.opponent_id,
    tfh.result,
    tfh.method,
    tfh.method_category,
    tfh.fight_round,
    tfh.fight_time,
    tfh.title_changed_hands,
    tfh.was_vacant,
    tfh.title_defense_number,
    YEAR(tfh.event_date) AS event_year
FROM gold.title_fight_history tfh
LEFT JOIN gold.manual_fighter_countries mfc_f
    ON mfc_f.fighter_id = tfh.fighter_id
LEFT JOIN gold.manual_fighter_countries mfc_o
    ON mfc_o.fighter_id = tfh.opponent_id
LEFT JOIN gold.manual_fighter_countries mfc_w
    ON mfc_w.fighter_id = tfh.winner_id
LEFT JOIN gold.manual_fighter_countries mfc_l
    ON mfc_l.fighter_id = tfh.loser_id
ORDER BY tfh.weight_class, tfh.event_date DESC
"""

SQL_MANUAL_TITLE_VACATES = """
SELECT
    mtv.weight_class,
    mtv.champion_name,
    mtv.vacated_on,
    mtv.reason,
    mtv.notes,
    mtv.loaded_at
FROM gold.manual_title_vacates mtv
ORDER BY mtv.vacated_on DESC, mtv.weight_class, mtv.champion_name
"""

SQL_FEATURE_IMPORTANCE_CATBOOST = """
SELECT
    fi.created_at,
    fi.target,
    fi.model_name,
    fi.feature,
    fi.feature_kind,
    fi.importance,
    fi.loss_change,
    fi.rank,
    fi.n_train
FROM gold.feature_importance_catboost fi
ORDER BY fi.target, fi.model_name, fi.rank
"""

SQL_HPARAM_IMPORTANCE_CATBOOST = """
SELECT
    hp.created_at,
    hp.target,
    hp.model_name,
    hp.metric,
    hp.param,
    hp.importance,
    hp.rank,
    hp.n_trials,
    hp.best_value
FROM gold.hparam_importance_catboost hp
ORDER BY hp.target, hp.model_name, hp.rank
"""

SQL_TUNE_TRIALS_CATBOOST = """
SELECT
    tt.created_at,
    tt.target,
    tt.model_name,
    tt.metric,
    tt.trial_number,
    tt.state,
    tt.value,
    tt.best_value_so_far,
    tt.is_best,
    tt.duration_seconds,
    tt.params_json
FROM gold.tune_trials_catboost tt
ORDER BY tt.target, tt.model_name, tt.trial_number
"""

# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------

DATASETS: dict[str, dict] = {
    "upcoming": {
        "folder": "dashboard_upcoming_cards",
        "sql": SQL_UPCOMING_CARDS,
        "partition_by": ["event_year", "event_month", "event_date"],
    },
    "upcoming_ensemble": {
        "folder": "dashboard_upcoming_cards_ensemble",
        "sql": SQL_UPCOMING_CARDS_ENSEMBLE,
        "partition_by": ["event_year", "event_month", "event_date"],
    },
    "upcoming_catboost": {
        "folder": "dashboard_upcoming_cards_catboost",
        "sql": SQL_UPCOMING_CARDS_CATBOOST,
        "partition_by": ["event_year", "event_month", "event_date"],
    },
    "upcoming_logreg": {
        "folder": "dashboard_upcoming_cards_logreg",
        "sql": SQL_UPCOMING_CARDS_LOGREG,
        "partition_by": ["event_year", "event_month", "event_date"],
    },
    "events": {
        "folder": "dashboard_upcoming_events",
        "sql": SQL_UPCOMING_EVENTS,
        "partition_by": ["event_year", "event_month"],
    },
    "historical": {
        "folder": "dashboard_hist_historical_all",
        "sql": SQL_HIST_HISTORICAL_ALL,
        "partition_by": None,
    },
    "historical_ensemble": {
        "folder": "dashboard_hist_historical_all_ensemble",
        "sql": SQL_HIST_HISTORICAL_ALL_ENSEMBLE,
        "partition_by": None,
    },
    "historical_catboost": {
        "folder": "dashboard_hist_historical_all_catboost",
        "sql": SQL_HIST_HISTORICAL_ALL_CATBOOST,
        "partition_by": None,
    },
    "historical_logreg": {
        "folder": "dashboard_hist_historical_all_logreg",
        "sql": SQL_HIST_HISTORICAL_ALL_LOGREG,
        "partition_by": None,
    },
    "calibration": {
        "folder": "dashboard_calibration_buckets",
        "sql": SQL_CALIBRATION,
        "partition_by": None,
    },
    "calibration_ensemble": {
        "folder": "dashboard_calibration_buckets_ensemble",
        "sql": SQL_CALIBRATION_ENSEMBLE,
        "partition_by": None,
    },
    "calibration_catboost": {
        "folder": "dashboard_calibration_buckets_catboost",
        "sql": SQL_CALIBRATION_CATBOOST,
        "partition_by": None,
    },
    "calibration_logreg": {
        "folder": "dashboard_calibration_buckets_logreg",
        "sql": SQL_CALIBRATION_LOGREG,
        "partition_by": None,
    },
    "stats": {
        "folder": "dashboard_model_stats",
        "sql": SQL_MODEL_STATS,
        "partition_by": None,
    },
    "stats_ensemble": {
        "folder": "dashboard_model_stats_ensemble",
        "sql": SQL_MODEL_STATS_ENSEMBLE,
        "partition_by": None,
    },
    "stats_catboost": {
        "folder": "dashboard_model_stats_catboost",
        "sql": SQL_MODEL_STATS_CATBOOST,
        "partition_by": None,
    },
    "stats_logreg": {
        "folder": "dashboard_model_stats_logreg",
        "sql": SQL_MODEL_STATS_LOGREG,
        "partition_by": None,
    },
    "rankings": {
        "folder": "dashboard_rankings",
        "sql": SQL_RANKINGS,
        "partition_by": ["weight_class"],
    },
    "overall_rankings": {
        "folder": "dashboard_overall_rankings",
        "sql": SQL_OVERALL_RANKINGS,
        "partition_by": None,
    },
    "fighter_profiles": {
        "folder": "dashboard_fighter_profiles",
        "sql": SQL_FIGHTER_PROFILES,
        "partition_by": None,
    },
    "fighter_history": {
        "folder": "dashboard_fighter_history",
        "sql": SQL_FIGHTER_HISTORY,
        "partition_by": ["event_year"],
    },
    "belt_holders": {
        "folder": "dashboard_belt_holders",
        "sql": SQL_BELT_HOLDERS,
        "partition_by": None,
    },
    "title_fight_history": {
        "folder": "dashboard_title_fight_history",
        "sql": SQL_TITLE_FIGHT_HISTORY,
        "partition_by": ["weight_class"],
    },
    "manual_title_vacates": {
        "folder": "dashboard_manual_title_vacates",
        "sql": SQL_MANUAL_TITLE_VACATES,
        "partition_by": None,
    },
    "feature_importance_catboost": {
        "folder": "dashboard_feature_importance_catboost",
        "sql": SQL_FEATURE_IMPORTANCE_CATBOOST,
        "partition_by": None,
    },
    "hparam_importance_catboost": {
        "folder": "dashboard_hparam_importance_catboost",
        "sql": SQL_HPARAM_IMPORTANCE_CATBOOST,
        "partition_by": None,
    },
    "tune_trials_catboost": {
        "folder": "dashboard_tune_trials_catboost",
        "sql": SQL_TUNE_TRIALS_CATBOOST,
        "partition_by": None,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_duckdb_path(args: argparse.Namespace) -> str:
    """Resolve DuckDB file path from CLI > env vars."""
    if args.duckdb_path:
        return str(Path(args.duckdb_path).expanduser())
    # Env based on target
    target_env = "DUCK_WH_DB" if args.target == "prod" else "DUCK_DEV_DB"
    env_path = os.environ.get(target_env) or os.environ.get("DUCKDB_PATH")
    if not env_path:
        raise RuntimeError(
            f"No DuckDB path found. Set {target_env}, DUCKDB_PATH, or pass --duckdb-path."
        )
    return str(Path(env_path).expanduser())


def resolve_base_uri(args: argparse.Namespace) -> str:
    """Resolve the base output URI (local path or s3:// / az://)."""
    if args.parquet_base:
        return args.parquet_base.rstrip("/")
    env = os.environ.get("PARQUET_BASE_URI")
    if env:
        return env.rstrip("/")
    # Sensible local default next to the DuckDB database
    db_dir = Path(resolve_duckdb_path(args)).parent
    return str(db_dir / "lake")


def resolve_prefix(args: argparse.Namespace) -> str:
    """Resolve optional serving prefix from CLI/env.

    Example: prefix='mma/diamond' -> outputs under <base>/mma/diamond/...
    """
    raw = (getattr(args, "prefix", None) or os.environ.get("PARQUET_PREFIX", "")).strip()
    return raw.strip("/")


def _apply_prefix(prefix: str, folder: str) -> str:
    if not prefix:
        return folder
    return f"{prefix}/{folder}"


def _homepage_reference_prefix(prefix: str) -> str:
    parts = [part for part in (prefix or "").split("/") if part]
    root = parts[0] if parts else ""
    return f"{root}/reference/homepage" if root else "reference/homepage"


def _is_remote(uri: str) -> bool:
    return uri.startswith("s3://") or uri.startswith("az://") or uri.startswith("gs://")


def _is_azure(uri: str) -> bool:
    return uri.startswith("az://") or uri.startswith("azure://")


# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _resolve_azure_config(
    args: argparse.Namespace,
) -> dict | None:
    """Build Azure config dict from CLI flags / env vars.  Returns None if
    credentials are not available."""
    account = getattr(args, "azure_account", None) or os.environ.get("AZURE_STORAGE_ACCOUNT")
    key = getattr(args, "azure_key", None) or os.environ.get("AZURE_STORAGE_KEY")
    container = (
        getattr(args, "azure_container", None)
        or os.environ.get("AZURE_STORAGE_CONTAINER", "fightprophet-dashboard")
    )
    if not account or not key:
        return None
    return {
        "account": account,
        "key": key,
        "container": container,
        "base_uri": f"az://{container}",
        "connection_string": (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={account};"
            f"AccountKey={key};"
            f"EndpointSuffix=core.windows.net"
        ),
    }


def _ensure_azure_container(azure_cfg: dict) -> None:
    """Create the Azure Blob container if it does not exist."""
    try:
        from azure.storage.blob import BlobServiceClient  # lazy import
    except ImportError as exc:
        raise RuntimeError(
            "azure-storage-blob is required for Azure uploads.  "
            "pip install azure-storage-blob"
        ) from exc

    client = BlobServiceClient(
        account_url=f"https://{azure_cfg['account']}.blob.core.windows.net",
        credential=azure_cfg["key"],
    )
    container_client = client.get_container_client(azure_cfg["container"])
    if not container_client.exists():
        container_client.create_container()
        logger.info(
            "✓  Created Azure container '%s' (account=%s)",
            azure_cfg["container"],
            azure_cfg["account"],
        )
    else:
        logger.info(
            "✓  Azure container '%s' already exists",
            azure_cfg["container"],
        )


def _setup_azure(azure_cfg: dict) -> None:
    """Ensure the target Azure container exists (creates it if missing)."""
    _ensure_azure_container(azure_cfg)
    logger.info(
        "✓  Azure ready  (account=%s, container=%s)",
        azure_cfg["account"],
        azure_cfg["container"],
    )


def _get_blob_service_client(azure_cfg: dict):
    """Lazily import and return a BlobServiceClient."""
    try:
        from azure.storage.blob import BlobServiceClient  # lazy import
    except ImportError as exc:
        raise RuntimeError(
            "azure-storage-blob is required for Azure uploads.  "
            "pip install azure-storage-blob"
        ) from exc
    return BlobServiceClient(
        account_url=f"https://{azure_cfg['account']}.blob.core.windows.net",
        credential=azure_cfg["key"],
    )


def _upload_blob(
    azure_cfg: dict,
    blob_name: str,
    data: bytes,
) -> None:
    """Upload raw bytes to Azure Blob Storage via the SDK."""
    client = _get_blob_service_client(azure_cfg)
    blob = client.get_blob_client(
        container=azure_cfg["container"],
        blob=blob_name,
    )
    blob.upload_blob(data, overwrite=True)
    logger.info("  ✓  Uploaded blob %s/%s", azure_cfg["container"], blob_name)


def _upload_directory_to_azure(
    azure_cfg: dict,
    local_dir: str,
    remote_prefix: str,
) -> int:
    """Upload all files from *local_dir* to Azure Blob Storage.

    Walks the directory tree and uploads each file with a blob name
    of ``<remote_prefix>/<relative_path>``.

    Returns the number of blobs uploaded.
    """
    client = _get_blob_service_client(azure_cfg)
    container_client = client.get_container_client(azure_cfg["container"])
    local_root = Path(local_dir)
    files = [fpath for fpath in sorted(local_root.rglob("*")) if fpath.is_file()]

    if not files:
        logger.info(
            "  ✓  Uploaded %d file(s) → %s/%s/",
            0, azure_cfg["container"], remote_prefix,
        )
        return 0

    max_workers = int(os.environ.get("AZURE_UPLOAD_MAX_WORKERS", "8") or "8")
    max_workers = max(1, min(max_workers, 32))

    def _upload_one(fpath: Path) -> None:
        rel = fpath.relative_to(local_root).as_posix()
        blob_name = f"{remote_prefix}/{rel}"
        blob_client = container_client.get_blob_client(blob_name)
        with fpath.open("rb") as fh:
            blob_client.upload_blob(fh, overwrite=True)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_upload_one, fpath) for fpath in files]
        for fut in as_completed(futures):
            fut.result()

    count = len(files)
    logger.info(
        "  ✓  Uploaded %d file(s) → %s/%s/",
        count, azure_cfg["container"], remote_prefix,
    )
    return count


def _read_latest_json(
    base_uri: str,
    folder: str,
    azure_cfg: dict | None = None,
) -> dict | None:
    """Read LATEST.json for a dataset folder.

    Returns parsed JSON dict or None when missing/unreadable.
    """
    try:
        if _is_azure(base_uri) and azure_cfg:
            client = _get_blob_service_client(azure_cfg)
            container_client = client.get_container_client(azure_cfg["container"])
            blob_name = f"{folder}/LATEST.json"
            if not container_client.get_blob_client(blob_name).exists():
                return None
            data = container_client.get_blob_client(blob_name).download_blob().readall()
            return json.loads(data)

        latest_file = Path(f"{base_uri}/{folder}/LATEST.json")
        if not latest_file.exists():
            return None
        return json.loads(latest_file.read_text())
    except Exception:
        return None


def _version_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _build_copy_sql(
    sql: str,
    dest: str,
    partition_by: list[str] | None,
) -> str:
    """Build a DuckDB COPY … TO … PARQUET statement."""
    row_group_size = 100000
    if partition_by:
        part_clause = ", ".join(partition_by)
        return (
            f"COPY ({sql}) TO '{dest}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {row_group_size}, "
            f"PARTITION_BY ({part_clause}), OVERWRITE_OR_IGNORE 1)"
        )
    out_file = f"{dest}/data.parquet"
    return (
        f"COPY ({sql}) TO '{out_file}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {row_group_size})"
    )


def _json_int(value: object, default: int = 0) -> int:
    if value is None or pd.isna(value):
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _json_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _json_pct(value: object) -> float | None:
    num = _json_float(value)
    if num is None:
        return None
    if num <= 1.0:
        num *= 100.0
    return round(num, 1)


def _division_sort_index(value: str) -> int:
    try:
        return DIVISION_ORDER.index(value)
    except ValueError:
        return len(DIVISION_ORDER)


def _write_reference_json(
    base_uri: str,
    prefix: str,
    filename: str,
    payload: dict,
    *,
    dry_run: bool = False,
    azure_cfg: dict | None = None,
    local_cache: str | None = None,
) -> None:
    reference_prefix = _homepage_reference_prefix(prefix)
    blob_name = f"{reference_prefix}/{filename}"
    payload_bytes = (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    if dry_run:
        logger.info("[DRY-RUN] Would write homepage feed %s/%s", base_uri, blob_name)
        return

    if _is_azure(base_uri) and azure_cfg:
        _upload_blob(azure_cfg, blob_name, payload_bytes)
    elif _is_remote(base_uri):
        logger.warning("Remote homepage JSON write not implemented for path=%s/%s", base_uri, blob_name)
    else:
        out_file = Path(f"{base_uri}/{blob_name}")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(payload_bytes)
        logger.info("  ✓  Wrote homepage feed %s", out_file)

    if local_cache and _is_remote(base_uri):
        local_file = Path(local_cache) / blob_name
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(payload_bytes)
        logger.info("  ✓  Cached homepage feed locally → %s", local_file)


def _build_profile_maps(df_profiles: pd.DataFrame) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in df_profiles.to_dict("records"):
        fighter_id = str(row.get("fighter_id") or "").strip()
        fighter_name = str(row.get("fighter_name") or "").strip()
        fighter_name_display = str(row.get("fighter_name_display") or "").strip()
        if fighter_id:
            by_id[fighter_id] = row
        if fighter_name:
            by_name[fighter_name] = row
            norm_name = _normalize_person_key(fighter_name)
            if norm_name:
                by_name[norm_name] = row
        if fighter_name_display:
            by_name[fighter_name_display] = row
            norm_display = _normalize_person_key(fighter_name_display)
            if norm_display:
                by_name[norm_display] = row
    return by_id, by_name


def _normalize_person_key(value: object) -> str:
    txt = str(value or "").strip().lower()
    if not txt:
        return ""
    txt = re.sub(r"\s*\([^)]*\)", "", txt)
    txt = re.sub(r"[^a-z0-9]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _build_current_champion_maps(
    df_belt: pd.DataFrame,
    profiles_by_id: dict[str, dict],
    profiles_by_name: dict[str, dict],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    by_id: dict[str, set[str]] = {}
    by_name: dict[str, set[str]] = {}

    if df_belt.empty:
        return by_id, by_name

    for row in df_belt.to_dict("records"):
        if _json_int(row.get("is_vacant"), default=0) == 1:
            continue

        weight_class = str(row.get("weight_class") or "").strip()
        if not weight_class:
            continue

        fighter_id = str(row.get("champion_fighter_id") or "").strip()
        champion_name = str(row.get("champion_fighter_name") or "").strip()

        profile = (
            (profiles_by_id.get(fighter_id) if fighter_id else None)
            or profiles_by_name.get(champion_name)
            or profiles_by_name.get(_normalize_person_key(champion_name))
            or {}
        )

        resolved_id = str(profile.get("fighter_id") or fighter_id).strip()
        name_candidates = {
            champion_name,
            str(profile.get("fighter_name") or "").strip(),
            str(profile.get("fighter_name_display") or "").strip(),
        }

        if resolved_id:
            by_id.setdefault(resolved_id, set()).add(weight_class)

        for raw_name in name_candidates:
            norm = _normalize_person_key(raw_name)
            if norm:
                by_name.setdefault(norm, set()).add(weight_class)

    return by_id, by_name


def _export_homepage_rankings_wall(
    conn: duckdb.DuckDBPyConnection,
    base_uri: str,
    prefix: str,
    version: str,
    *,
    dry_run: bool = False,
    azure_cfg: dict | None = None,
    local_cache: str | None = None,
) -> dict:
    df_rank = conn.execute(f"SELECT * FROM ({DATASETS['rankings']['sql']}) _q").fetchdf()
    df_profiles = conn.execute(f"SELECT * FROM ({DATASETS['fighter_profiles']['sql']}) _q").fetchdf()
    df_belt = conn.execute(f"SELECT * FROM ({DATASETS['belt_holders']['sql']}) _q").fetchdf()

    if df_rank.empty:
        payload = {
            "export_version": version,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "fighters": [],
        }
        _write_reference_json(
            base_uri,
            prefix,
            HOMEPAGE_RANKINGS_WALL_FILENAME,
            payload,
            dry_run=dry_run,
            azure_cfg=azure_cfg,
            local_cache=local_cache,
        )
        return {"dataset": "homepage_rankings_wall", "row_count": 0, "skipped": True}

    profiles_by_id, profiles_by_name = _build_profile_maps(df_profiles)
    champion_classes_by_id, champion_classes_by_name = _build_current_champion_maps(
        df_belt,
        profiles_by_id,
        profiles_by_name,
    )
    cards_by_key: dict[tuple[str, str], dict] = {}
    goats_by_key: dict[str, dict] = {}

    for row in df_rank.to_dict("records"):
        rank = _json_int(row.get("rank"), default=-1)
        if rank < 0:
            continue

        # Ship both active and inactive fighters; the Astro homepage filters
        # client-side and re-ranks the visible rows, so the JSON must include
        # everyone with a real `fighter_status` so the UI toggle has data.
        status_raw = str(row.get("fighter_status") or "").strip().lower()
        fighter_status = status_raw if status_raw in {"active", "inactive"} else "active"

        fighter_id = str(row.get("fighter_id") or "").strip()
        fighter_name = str(row.get("fighter_name") or "").strip()
        weight_class = str(row.get("weight_class") or "").strip()
        profile = (profiles_by_id.get(fighter_id) if fighter_id else None) or profiles_by_name.get(fighter_name) or {}
        fighter_display = str(profile.get("fighter_name_display") or profile.get("fighter_name") or fighter_name).strip()
        finish_rate = profile.get("finish_rate_win_shrunk")
        if finish_rate is None or pd.isna(finish_rate):
            finish_rate = profile.get("finish_rate")

        is_current_champion = False
        if fighter_id and weight_class in champion_classes_by_id.get(fighter_id, set()):
            is_current_champion = True
        else:
            for candidate_name in (fighter_display, fighter_name):
                norm_name = _normalize_person_key(candidate_name)
                if norm_name and weight_class in champion_classes_by_name.get(norm_name, set()):
                    is_current_champion = True
                    break

        card = {
            "fighter_id": fighter_id or str(profile.get("fighter_id") or "").strip(),
            "name": fighter_display,
            "country": str(profile.get("country") or row.get("country") or "").strip(),
            "weight_class": weight_class,
            "is_champion": is_current_champion,
            "fighter_status": fighter_status,
            "wins": _json_int(profile.get("wins")),
            "losses": _json_int(profile.get("losses")),
            "draws": _json_int(profile.get("draws")),
            "win_streak": _json_int(profile.get("win_streak")),
            "loss_streak": _json_int(profile.get("loss_streak")),
            "longest_win_streak": _json_int(profile.get("longest_win_streak")),
            "longest_loss_streak": _json_int(profile.get("longest_loss_streak")),
            "finish_rate": _json_pct(finish_rate),
            "sub_rate": _json_pct(profile.get("sub_rate_win_shrunk")),
            "ko_rate": _json_pct(profile.get("ko_rate_win_shrunk")),
            "rank": rank,
            "points": _json_float(row.get("points")),
            "global_rank": _json_int(row.get("global_rank")),
            "global_points": _json_float(row.get("global_points")),
            "normalized_global_score": _json_float(row.get("normalized_global_score")),
        }

        dedupe_id = str(card.get("fighter_id") or "").strip()
        dedupe_name = _normalize_person_key(card.get("name") or fighter_name)
        dedupe_key = (weight_class, dedupe_id or dedupe_name)
        if not dedupe_key[1]:
            continue

        existing = cards_by_key.get(dedupe_key)
        if existing is None:
            cards_by_key[dedupe_key] = card
        else:
            existing_rank = _json_int(existing.get("rank"), default=999999)
            new_rank = _json_int(card.get("rank"), default=999999)
            existing_points = _json_float(existing.get("points"))
            new_points = _json_float(card.get("points"))
            existing_points = existing_points if existing_points is not None else -1.0
            new_points = new_points if new_points is not None else -1.0

            keep_new = (
                new_rank < existing_rank
                or (
                    new_rank == existing_rank
                    and new_points > existing_points
                )
            )

            if keep_new:
                if existing.get("is_champion") and not card.get("is_champion"):
                    card["is_champion"] = True
                cards_by_key[dedupe_key] = card
            elif card.get("is_champion") and not existing.get("is_champion"):
                existing["is_champion"] = True

        goat_key = dedupe_id or dedupe_name
        if goat_key:
            existing_goat = goats_by_key.get(goat_key)
            if existing_goat is None:
                goats_by_key[goat_key] = dict(card)
            else:
                existing_global_rank = _json_int(existing_goat.get("global_rank"), default=999999)
                new_global_rank = _json_int(card.get("global_rank"), default=999999)
                existing_global_points = _json_float(existing_goat.get("global_points"))
                new_global_points = _json_float(card.get("global_points"))
                existing_global_points = existing_global_points if existing_global_points is not None else -1.0
                new_global_points = new_global_points if new_global_points is not None else -1.0
                keep_new_goat = (
                    new_global_rank < existing_global_rank
                    or (
                        new_global_rank == existing_global_rank
                        and new_global_points > existing_global_points
                    )
                )
                if keep_new_goat:
                    if existing_goat.get("is_champion") and not card.get("is_champion"):
                        card["is_champion"] = True
                    goats_by_key[goat_key] = dict(card)
                elif card.get("is_champion") and not existing_goat.get("is_champion"):
                    existing_goat["is_champion"] = True

    cards = list(cards_by_key.values())
    cards.sort(key=lambda row: (_division_sort_index(str(row.get("weight_class") or "")), _json_int(row.get("rank"), 999)))
    goats = list(goats_by_key.values())
    goats.sort(
        key=lambda row: (
            _json_int(row.get("global_rank"), 999999),
            -(_json_float(row.get("global_points")) if _json_float(row.get("global_points")) is not None else -1.0),
            _json_int(row.get("rank"), 999999),
        )
    )

    payload = {
        "export_version": version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "fighters": cards,
        "goats": goats,
    }
    _write_reference_json(
        base_uri,
        prefix,
        HOMEPAGE_RANKINGS_WALL_FILENAME,
        payload,
        dry_run=dry_run,
        azure_cfg=azure_cfg,
        local_cache=local_cache,
    )
    return {"dataset": "homepage_rankings_wall", "row_count": len(cards)}


def _export_dataset(
    conn: duckdb.DuckDBPyConnection,
    base_uri: str,
    prefix: str,
    dataset_key: str,
    version: str,
    dry_run: bool = False,
    local_cache: str | None = None,
    azure_cfg: dict | None = None,
    incremental_fighter_history: bool = True,
) -> dict:
    """Run a single dataset's SQL and write the result as Parquet.

    For Azure targets the data is first written to a local temp directory
    via DuckDB ``COPY TO``, then uploaded to Azure Blob Storage via the
    SDK (DuckDB's Azure extension does not support writes to Blob Storage).

    When *local_cache* is given **and** *base_uri* is remote (az://),
    a secondary copy goes to the local cache path so the Streamlit
    container can read from disk without Azure SDK.

    Returns metadata dict for LATEST.json.
    """
    ds = DATASETS[dataset_key]
    folder = _apply_prefix(prefix, ds["folder"])
    sql = ds["sql"]
    partition_by = ds["partition_by"]

    version_dir = f"{base_uri}/{folder}/export_version={version}"

    # Preview row count
    count_sql = f"SELECT COUNT(*) AS cnt FROM ({sql}) _q"
    row_count = conn.execute(count_sql).fetchone()[0]

    snapshot: dict | None = None
    if dataset_key == "fighter_history":
        snap_sql = f"""
        SELECT
            COUNT(*) AS row_count,
            MAX(event_date::DATE) AS max_event_date,
            COUNT(DISTINCT fighter_id) AS fighter_count
        FROM ({sql}) _q
        """
        snap_row = conn.execute(snap_sql).fetchone()
        snapshot = {
            "row_count": int(snap_row[0] or 0),
            "max_event_date": str(snap_row[1]) if snap_row[1] is not None else None,
            "fighter_count": int(snap_row[2] or 0),
        }

        if incremental_fighter_history and not dry_run:
            prev = _read_latest_json(base_uri, folder, azure_cfg=azure_cfg)
            prev_snapshot = prev.get("snapshot") if isinstance(prev, dict) else None
            if isinstance(prev_snapshot, dict) and prev_snapshot == snapshot:
                logger.info(
                    "Skipping %s (incremental): snapshot unchanged (rows=%d, max_event_date=%s, fighters=%d)",
                    dataset_key,
                    snapshot["row_count"],
                    snapshot["max_event_date"],
                    snapshot["fighter_count"],
                )
                return {
                    "dataset": dataset_key,
                    "folder": folder,
                    "export_version": prev.get("export_version", version),
                    "row_count": row_count,
                    "path": prev.get("path", f"{base_uri}/{folder}"),
                    "skipped": True,
                    "incremental_skipped": True,
                    "snapshot": snapshot,
                }

    cache_dir = (
        f"{local_cache}/{folder}/export_version={version}"
        if local_cache and _is_remote(base_uri)
        else None
    )

    if dry_run:
        logger.info(
            "[DRY-RUN] %s  →  %s  (%d rows, partition_by=%s)",
            dataset_key, version_dir, row_count, partition_by,
        )
        if cache_dir:
            logger.info("[DRY-RUN]      + local cache → %s", cache_dir)
        return {
            "dataset": dataset_key,
            "folder": folder,
            "export_version": version,
            "row_count": row_count,
            "path": version_dir,
            "dry_run": True,
            "snapshot": snapshot,
        }

    if row_count == 0:
        logger.warning("Dataset %s has 0 rows – skipping write.", dataset_key)
        return {
            "dataset": dataset_key,
            "folder": folder,
            "export_version": version,
            "row_count": 0,
            "path": version_dir,
            "skipped": True,
            "snapshot": snapshot,
        }

    logger.info(
        "Exporting %s  →  %s  (%d rows)",
        dataset_key, version_dir, row_count,
    )

    is_azure_target = _is_azure(base_uri) and azure_cfg

    if is_azure_target:
        # ── Azure path: write to temp dir, then upload via SDK ──
        remote_prefix = f"{folder}/export_version={version}"
        with tempfile.TemporaryDirectory(prefix="parquet_export_") as tmp_dir:
            # DuckDB writes to the local temp dir
            conn.execute(_build_copy_sql(sql, tmp_dir, partition_by))
            logger.info("  ✓  Wrote %d rows to temp dir", row_count)

            # Upload to Azure
            _upload_directory_to_azure(azure_cfg, tmp_dir, remote_prefix)

            # Copy to local cache if requested
            if cache_dir:
                import shutil
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                for src_file in Path(tmp_dir).rglob("*"):
                    if src_file.is_file():
                        dest_file = Path(cache_dir) / src_file.relative_to(tmp_dir)
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dest_file)
                logger.info("  ✓  Cached locally → %s", cache_dir)
    else:
        # ── Local or other remote path: write directly ──
        if not _is_remote(base_uri):
            Path(version_dir).mkdir(parents=True, exist_ok=True)

        conn.execute(_build_copy_sql(sql, version_dir, partition_by))
        logger.info("  ✓  Wrote %d rows to %s", row_count, version_dir)

    return {
        "dataset": dataset_key,
        "folder": folder,
        "export_version": version,
        "row_count": row_count,
        "path": version_dir,
        "glob": f"{version_dir}/**/*.parquet" if partition_by else f"{version_dir}/data.parquet",
        "cached_locally": bool(cache_dir),
        "snapshot": snapshot,
    }


def _write_latest_json(
    base_uri: str,
    folder: str,
    meta: dict,
    dry_run: bool = False,
    azure_cfg: dict | None = None,
    local_cache: str | None = None,
) -> None:
    """Write (or overwrite) LATEST.json for a dataset.

    For Azure targets the JSON is uploaded via the Blob SDK.
    When *local_cache* is set, a copy is also written to the
    local cache directory so the Streamlit reader works offline.
    """
    latest_path = f"{base_uri}/{folder}/LATEST.json"

    payload = {
        "export_version": meta["export_version"],
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "row_count": meta["row_count"],
        "path": meta.get("glob") or meta.get("path", ""),
        "snapshot": meta.get("snapshot"),
    }
    payload_bytes = (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    if dry_run:
        logger.info("[DRY-RUN] Would write %s:\n%s", latest_path, json.dumps(payload, indent=2))
        if local_cache and _is_remote(base_uri):
            logger.info("[DRY-RUN]      + local cache LATEST.json → %s/%s/LATEST.json", local_cache, folder)
        return

    # ---- Azure upload via SDK ----
    if _is_azure(base_uri) and azure_cfg:
        blob_name = f"{folder}/LATEST.json"
        _upload_blob(azure_cfg, blob_name, payload_bytes)
    elif _is_remote(base_uri):
        logger.warning(
            "Remote LATEST.json write not implemented for this scheme (path=%s).",
            latest_path,
        )
    else:
        # Pure local write
        latest_file = Path(latest_path)
        latest_file.parent.mkdir(parents=True, exist_ok=True)
        latest_file.write_bytes(payload_bytes)
        logger.info("  ✓  Updated %s", latest_path)

    # ---- Local cache copy (when primary target is remote) ----
    if local_cache and _is_remote(base_uri):
        # Rewrite payload.path to point at the local cache
        local_meta = dict(payload)
        cache_version_dir = f"{local_cache}/{folder}/export_version={meta['export_version']}"
        if meta.get("glob"):
            local_meta["path"] = f"{cache_version_dir}/**/*.parquet"
        else:
            local_meta["path"] = f"{cache_version_dir}/data.parquet"

        local_latest = Path(f"{local_cache}/{folder}/LATEST.json")
        local_latest.parent.mkdir(parents=True, exist_ok=True)
        local_latest.write_text(json.dumps(local_meta, indent=2) + "\n")
        logger.info("  ✓  Updated local cache %s", local_latest)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Materialize dashboard-serving Parquet datasets from DuckDB gold layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # all datasets, dev DB, local lake
  python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard --target dev --dataset all

  # only upcoming + events, prod DB, custom output path
  python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \\
      --target prod --dataset upcoming events \\
      --parquet-base /mnt/shared/lake

  # dry-run
  python -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard --dataset all --dry-run
""",
    )
    p.add_argument("--duckdb-path", help="Explicit DuckDB file path (overrides env vars)")
    p.add_argument("--target", choices=["dev", "prod"], default="dev",
                   help="Which env var to resolve: DUCK_DEV_DB (dev) or DUCK_WH_DB (prod)")
    p.add_argument(
        "--dataset",
        nargs="+",
        choices=[*DATASET_NAMES, ALL_ALIAS],
        default=[ALL_ALIAS],
        help="Which datasets to export (default: all)",
    )
    p.add_argument("--parquet-base", help="Base URI for Parquet output (overrides PARQUET_BASE_URI)")
    p.add_argument(
        "--prefix",
        default=None,
        help=(
            "Optional serving prefix under base output URI, e.g. 'mma/diamond' "
            "(or set PARQUET_PREFIX env var)."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print what would be written, don't write")
    p.add_argument(
        "--no-incremental-fighter-history",
        action="store_true",
        help="Disable incremental skip logic for fighter_history and force full re-export.",
    )
    p.add_argument(
        "--status-overrides-json",
        default=fighter_status_mod.DEFAULT_STATUS_OVERRIDES_JSON_PATH,
        help=(
            "Path to fighter-status override JSON. Loaded at startup so editing "
            "this file and re-running ONLY the dashboard ETL still propagates "
            "active/inactive changes to athletes/fight cards/rankings parquets."
        ),
    )
    p.add_argument(
        "--skip-status-refresh",
        action="store_true",
        help=(
            "Skip rebuilding gold.fighter_effective_status at startup. Use only "
            "if a separate ETL step already refreshed it in this run."
        ),
    )

    # Azure Blob Storage
    az = p.add_argument_group("Azure Blob Storage")
    az.add_argument(
        "--azure-account",
        help="Storage account name (or set AZURE_STORAGE_ACCOUNT env var)",
    )
    az.add_argument(
        "--azure-key",
        help="Storage account access key (or set AZURE_STORAGE_KEY env var)",
    )
    az.add_argument(
        "--azure-container",
        default=None,
        help="Blob container name (default: fightprophet-dashboard, or AZURE_STORAGE_CONTAINER env var)",
    )

    # Local cache (useful when writing to Azure but wanting a local copy)
    p.add_argument(
        "--local-cache",
        default=None,
        help=(
            "Local directory for caching Parquets when writing to Azure. "
            "Also set PARQUET_LOCAL_CACHE=1 to enable via env var."
        ),
    )
    p.add_argument(
        "--no-local-cache",
        action="store_true",
        help="Disable local caching even if PARQUET_LOCAL_CACHE=1",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    db_path = resolve_duckdb_path(args)
    version = _version_stamp()
    prefix = resolve_prefix(args)

    # ── Azure config ──
    azure_cfg = _resolve_azure_config(args)

    # ── Base URI: Azure takes priority → CLI/env → local default ──
    if azure_cfg:
        base_uri = azure_cfg["base_uri"]
    else:
        base_uri = resolve_base_uri(args)

    # ── Local cache (only meaningful when writing to a remote target) ──
    local_cache: str | None = None
    if not args.no_local_cache:
        if args.local_cache:
            local_cache = args.local_cache.rstrip("/")
        elif (
            os.environ.get("PARQUET_LOCAL_CACHE", "").lower() in ("1", "true", "yes")
            and _is_remote(base_uri)
        ):
            # Default cache directory next to the DuckDB file
            local_cache = str(Path(db_path).parent / "lake")

    # Resolve dataset list
    if ALL_ALIAS in args.dataset:
        datasets_to_run = list(DATASET_NAMES)
    else:
        datasets_to_run = list(dict.fromkeys(args.dataset))  # dedup, preserve order

    logger.info("=" * 70)
    logger.info("Dashboard Parquet Materializer")
    logger.info("  DuckDB      : %s", db_path)
    logger.info("  Output      : %s", base_uri)
    if prefix:
        logger.info("  Prefix      : %s", prefix)
    if azure_cfg:
        logger.info("  Azure acct  : %s", azure_cfg["account"])
        logger.info("  Container   : %s", azure_cfg["container"])
    if local_cache:
        logger.info("  Local cache : %s", local_cache)
    logger.info("  Version     : %s", version)
    logger.info("  Datasets    : %s", ", ".join(datasets_to_run))
    logger.info("  Dry-run     : %s", args.dry_run)
    logger.info("=" * 70)

    bootstrap_conn = duckdb.connect(db_path, read_only=False)
    bootstrap_conn.execute("CREATE SCHEMA IF NOT EXISTS gold")

    # Refresh canonical fighter status (single source of truth for active/inactive)
    # before running any export. This guarantees that editing the override JSON
    # and re-running ONLY the parquet dashboard ETL is enough to propagate
    # changes to rankings, athletes, and fight cards parquets.
    if not args.skip_status_refresh:
        try:
            status_json_path = fighter_status_mod.resolve_json_path(args.status_overrides_json)
            fighter_status_mod.build_effective_status(
                bootstrap_conn,
                status_json_path,
                cascade_to_silver=True,
            )
        except Exception:
            logger.exception(
                "Failed to refresh gold.fighter_effective_status from %s; "
                "exports will use whatever value is currently in the table.",
                args.status_overrides_json,
            )

    bootstrap_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gold.manual_fighter_countries (
            country VARCHAR,
            fighter_id VARCHAR,
            fighter_name VARCHAR,
            nickname VARCHAR
        )
        """
    )
    bootstrap_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gold.feature_importance_catboost (
            created_at TIMESTAMP,
            target VARCHAR,
            model_name VARCHAR,
            feature VARCHAR,
            feature_kind VARCHAR,
            importance DOUBLE,
            loss_change DOUBLE,
            rank BIGINT,
            n_train BIGINT
        )
        """
    )
    bootstrap_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gold.hparam_importance_catboost (
            created_at TIMESTAMP,
            target VARCHAR,
            model_name VARCHAR,
            metric VARCHAR,
            param VARCHAR,
            importance DOUBLE,
            rank BIGINT,
            n_trials BIGINT,
            best_value DOUBLE
        )
        """
    )
    bootstrap_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gold.tune_trials_catboost (
            created_at TIMESTAMP,
            target VARCHAR,
            model_name VARCHAR,
            metric VARCHAR,
            trial_number BIGINT,
            state VARCHAR,
            value DOUBLE,
            best_value_so_far DOUBLE,
            is_best BOOLEAN,
            duration_seconds DOUBLE,
            params_json VARCHAR
        )
        """
    )
    bootstrap_conn.close()

    conn = duckdb.connect(db_path, read_only=True)

    # Ensure Azure container exists (SDK-based, not DuckDB extension)
    if azure_cfg and not args.dry_run:
        _setup_azure(azure_cfg)

    results: list[dict] = []
    failed: list[str] = []

    for ds_key in datasets_to_run:
        try:
            meta = _export_dataset(
                conn, base_uri, prefix, ds_key, version,
                dry_run=args.dry_run,
                local_cache=local_cache,
                azure_cfg=azure_cfg,
                incremental_fighter_history=not args.no_incremental_fighter_history,
            )
            results.append(meta)

            if not meta.get("skipped"):
                folder = _apply_prefix(prefix, DATASETS[ds_key]["folder"])
                _write_latest_json(
                    base_uri,
                    folder,
                    meta,
                    dry_run=args.dry_run,
                    azure_cfg=azure_cfg,
                    local_cache=local_cache,
                )

        except Exception:
            logger.exception("FAILED exporting dataset '%s'", ds_key)
            failed.append(ds_key)

    homepage_keys = {"rankings", "fighter_profiles"}
    if homepage_keys.issubset(set(datasets_to_run)) and not any(key in failed for key in homepage_keys):
        try:
            homepage_meta = _export_homepage_rankings_wall(
                conn,
                base_uri,
                prefix,
                version,
                dry_run=args.dry_run,
                azure_cfg=azure_cfg,
                local_cache=local_cache,
            )
            results.append(homepage_meta)
        except Exception:
            logger.exception("FAILED exporting homepage rankings wall feed")
            failed.append("homepage_rankings_wall")

    conn.close()

    # Summary
    logger.info("-" * 70)
    logger.info("Summary:")
    for r in results:
        status = "DRY-RUN" if r.get("dry_run") else ("SKIPPED" if r.get("skipped") else "OK")
        cached = "  +cached" if r.get("cached_locally") else ""
        logger.info("  %-14s  %6d rows  %s%s", r["dataset"], r["row_count"], status, cached)
    if failed:
        logger.error("  FAILED: %s", ", ".join(failed))
        sys.exit(1)

    logger.info("Done (version=%s).", version)


if __name__ == "__main__":
    main()
