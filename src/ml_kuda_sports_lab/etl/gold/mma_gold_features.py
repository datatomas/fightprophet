#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Gold Layer: Pre-fight features for win/loss modeling (no leakage).

Creates:
- gold.prefight_features (TABLE)
- gold.prefight_fight_features (VIEW alias for backwards compatibility)

Key points:
- Features for each fighter are computed ONLY from fights strictly before the current fight
  via window frames ending at "1 PRECEDING".
- Opponent features are joined from the opponent's corresponding fight-row (same bout).
- Includes delta_* features (fighter - opponent) which typically work well for linear models.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

import duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def resolve_duckdb_path(args: argparse.Namespace) -> str:
    if args.duckdb_path:
        return str(Path(args.duckdb_path).expanduser())
    target_env = "DUCK_WH_DB" if args.target == "prod" else "DUCK_DEV_DB"
    env_path = os.environ.get(target_env)
    if not env_path:
        raise RuntimeError(f"{target_env} not set; please export it or pass --duckdb-path")
    return str(Path(env_path).expanduser())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gold pre-fight feature builder (no leakage)")
    p.add_argument("--duckdb-path", help="Path to the DuckDB file")
    p.add_argument("--target", choices=["dev", "prod"], default="dev")
    p.add_argument("--rebuild", action="store_true", help="Drop and recreate the gold table")
    return p.parse_args()


def _table_exists(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    q = """
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = ? AND table_name = ?
    LIMIT 1
    """
    return conn.execute(q, [schema, table]).fetchone() is not None


# ...existing code...

def main() -> None:
    args = parse_args()
    db_path = resolve_duckdb_path(args)
    logger.info(f"Using DuckDB at {db_path}")

    conn = duckdb.connect(db_path)
    conn.execute("PRAGMA enable_progress_bar = FALSE")
    conn.execute("CREATE SCHEMA IF NOT EXISTS gold")

    if not _table_exists(conn, "silver", "fights"):
        raise RuntimeError(
            "Missing required table silver.fights. "
            "Run the silver ETL first (mma_silver_schema.py) to build silver.fights."
        )

    sql = r"""
    CREATE TABLE IF NOT EXISTS gold.prefight_features AS
    WITH fights_norm AS (
        SELECT
            -- Keep all fight columns, but override event_name with a resolved value
            sf.* EXCLUDE (
                event_name,
                fighter_name,
                fighter_name_plain,
                fighter_name_display,
                opponent_name,
                opponent_name_plain,
                opponent_name_display
            ),

            -- Prefer unified name if present; otherwise pull from UFC bronze events
            COALESCE(sf.event_name, ue.event_name) AS event_name,

            -- Authoritative naming from silver.fighters (includes duplicate-name disambiguation)
            COALESCE(fd.fighter_name_plain, sf.fighter_name_plain, sf.fighter_name, sf.fighter_id) AS fighter_name_plain,
            COALESCE(
                fd.fighter_name_display,
                fd.fighter_name,
                sf.fighter_name_display,
                sf.fighter_name_plain,
                sf.fighter_name,
                sf.fighter_id
            ) AS fighter_name_display,
            COALESCE(
                fd.fighter_name,
                fd.fighter_name_display,
                sf.fighter_name,
                sf.fighter_name_display,
                sf.fighter_id
            ) AS fighter_name,

            COALESCE(od.fighter_name_plain, sf.opponent_name_plain, sf.opponent_name, sf.opponent_id) AS opponent_name_plain,
            COALESCE(
                od.fighter_name_display,
                od.fighter_name,
                sf.opponent_name_display,
                sf.opponent_name_plain,
                sf.opponent_name,
                sf.opponent_id
            ) AS opponent_name_display,
            COALESCE(
                od.fighter_name,
                od.fighter_name_display,
                sf.opponent_name,
                sf.opponent_name_display,
                sf.opponent_id
            ) AS opponent_name,

            -- A stable pairing key for self-join (two perspectives of same bout)
            md5(
                sf.organization || '|' ||
                coalesce(sf.event_url, '') || '|' ||
                least(sf.fighter_id, sf.opponent_id) || '|' ||
                greatest(sf.fighter_id, sf.opponent_id) || '|' ||
                coalesce(CAST(sf.round AS VARCHAR), '') || '|' ||
                coalesce(CAST(sf.time AS VARCHAR), '')
            ) AS bout_key,

            TRUE AS is_ufc,
            CASE WHEN sf.result = 'win' THEN 1 WHEN sf.result = 'loss' THEN 0 ELSE NULL END AS y_win,

            -- per-fight diffs (in-fight; only used as aggregates over PAST fights)
            (sf.kd_for  - sf.kd_against)  AS kd_diff,
            (sf.str_for - sf.str_against) AS str_diff,
            (sf.td_for  - sf.td_against)  AS td_diff,
            (sf.sub_for - sf.sub_against) AS sub_diff,

            -- bonuses (UFC only)
            CASE WHEN sf.bonus_tags ILIKE '%PERF%' THEN 1 ELSE 0 END AS has_perf_bonus,
            CASE WHEN sf.bonus_tags ILIKE '%FOTN%' OR sf.bonus_tags ILIKE '%FIGHT%' THEN 1 ELSE 0 END AS has_fotn_bonus,
            CASE WHEN sf.bonus_tags IS NULL OR trim(sf.bonus_tags) = '' THEN 0 ELSE 1 END AS has_any_bonus,

            -- === Fighter profile from silver.fighters (static per fighter) ===
            fd.stance                       AS fighter_stance,
            fd.reach                        AS fighter_reach,
            fd.height                       AS fighter_height,
            fd.age                          AS fighter_age,
            od.stance                       AS opponent_stance,
            od.reach                        AS opponent_reach,
            od.height                       AS opponent_height,
            od.age                          AS opponent_age

            -- (Removed leaky fighter_last_fight_date and fighter_status; see prefight_base for new features)

        FROM silver.fights sf
        LEFT JOIN silver.fighters fd
                ON fd.organization = sf.organization
               AND fd.fighter_id = sf.fighter_id
        LEFT JOIN silver.fighters od
                ON od.organization = sf.organization
               AND od.fighter_id = sf.opponent_id
        LEFT JOIN bronze.ufc_events ue
                    ON sf.event_url = ue.event_url

        WHERE sf.event_date IS NOT NULL
                    AND sf.organization = 'UFC'
          AND sf.fighter_id IS NOT NULL
          AND sf.opponent_id IS NOT NULL
    ),

    params AS (
        SELECT 10.0::DOUBLE AS prior_strength
    ),

    global_rates AS (
        SELECT
            CASE
                WHEN SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) = 0 THEN 0.0
                ELSE
                    SUM(CASE WHEN result = 'win' AND method_category = 'KO_TKO' THEN 1 ELSE 0 END)::DOUBLE /
                    SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END)
            END AS global_ko_rate,
            CASE
                WHEN SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) = 0 THEN 0.0
                ELSE
                    SUM(CASE WHEN result = 'win' AND method_category = 'SUB' THEN 1 ELSE 0 END)::DOUBLE /
                    SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END)
            END AS global_sub_rate,
            CASE
                WHEN SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) = 0 THEN 0.0
                ELSE
                    SUM(CASE WHEN result = 'win' AND method_category IN ('KO_TKO','SUB') THEN 1 ELSE 0 END)::DOUBLE /
                    SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END)
            END AS global_finish_rate
        FROM fights_norm
    ),

    prefight_base AS (
        SELECT
            f.*,

            -- === No-leakage inactivity and streak features ===
            MAX(CAST(f.event_date AS DATE)) OVER w_all AS fighter_last_fight_date,
            CASE
                WHEN MAX(CAST(f.event_date AS DATE)) OVER w_all IS NULL THEN NULL
                ELSE date_diff('day', MAX(CAST(f.event_date AS DATE)) OVER w_all, CAST(f.event_date AS DATE))
            END AS days_since_last_fight,
            CASE
                WHEN MAX(CAST(f.event_date AS DATE)) OVER w_all IS NOT NULL
                 AND date_diff('day', MAX(CAST(f.event_date AS DATE)) OVER w_all, CAST(f.event_date AS DATE)) > 730
                THEN 'inactive'
                ELSE 'active'
            END AS fighter_status,
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) OVER w_streak AS win_streak_entering,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) OVER w_streak AS loss_streak_entering,

            -- === Record / form (career-to-date, excluding current fight) ===
            COUNT(*) FILTER (WHERE result IN ('win','loss')) OVER w_all AS fights_count,
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) OVER w_all AS wins_count,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) OVER w_all AS losses_count,

            -- Raw win outcome counts (for shrinkage; denominator = wins_count)
            SUM(CASE WHEN result='win' AND method_category IN ('KO_TKO','SUB') THEN 1 ELSE 0 END) OVER w_all
                AS finish_wins_count,
            SUM(CASE WHEN result='win' AND method_category='KO_TKO' THEN 1 ELSE 0 END) OVER w_all
                AS ko_wins_count,
            SUM(CASE WHEN result='win' AND method_category='SUB' THEN 1 ELSE 0 END) OVER w_all
                AS sub_wins_count,
            SUM(CASE WHEN result='win' AND method_category='DEC' THEN 1 ELSE 0 END) OVER w_all
                AS dec_wins_count,

            CAST(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(COUNT(*) FILTER (WHERE result IN ('win','loss')) OVER w_all, 0) AS win_rate_all,

            CAST(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) OVER w_last5 AS DOUBLE)
                / NULLIF(COUNT(*) FILTER (WHERE result IN ('win','loss')) OVER w_last5, 0) AS win_rate_last_5,

            -- net wins in last 5 (win=+1, loss=-1, else 0)
            SUM(
                CASE
                    WHEN result = 'win' THEN 1
                    WHEN result = 'loss' THEN -1
                    ELSE 0
                END
            ) OVER w_last5 AS net_wins_last_5,

            -- === Finish profile (conditional on wins; computed from prior fights) ===
            CAST(SUM(CASE WHEN result='win' AND method_category='KO_TKO' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) OVER w_all, 0) AS ko_win_rate,
            CAST(SUM(CASE WHEN result='win' AND method_category='SUB' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) OVER w_all, 0) AS sub_win_rate,
            CAST(SUM(CASE WHEN result='win' AND method_category='DEC' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) OVER w_all, 0) AS dec_win_rate,
            CAST(SUM(CASE WHEN result='win' AND method_category IN ('KO_TKO','SUB') THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) OVER w_all, 0) AS finish_win_rate,

            CAST(SUM(CASE WHEN result='loss' AND method_category='KO_TKO' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) OVER w_all, 0) AS ko_loss_rate,
            CAST(SUM(CASE WHEN result='loss' AND method_category='SUB' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) OVER w_all, 0) AS sub_loss_rate,
            CAST(SUM(CASE WHEN result='loss' AND method_category='DEC' THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) OVER w_all, 0) AS dec_loss_rate,

            -- === Last-5 sample sizes for “diff” stats (so deltas don’t become fake zeros) ===
            COUNT(str_diff) OVER w_last5 AS last5_str_diff_n,
            COUNT(td_diff)  OVER w_last5 AS last5_td_diff_n,
            COUNT(kd_diff)  OVER w_last5 AS last5_kd_diff_n,

            -- === Style/effectiveness from past fights (rolling medians of diffs) ===
            COALESCE(median(kd_diff)  FILTER (WHERE kd_diff  IS NOT NULL) OVER w_last5, 0.0) AS median_kd_diff_last_5,
            COALESCE(median(str_diff) FILTER (WHERE str_diff IS NOT NULL) OVER w_last5, 0.0) AS median_str_diff_last_5,
            COALESCE(median(td_diff)  FILTER (WHERE td_diff  IS NOT NULL) OVER w_last5, 0.0) AS median_td_diff_last_5,
            median(sub_diff) FILTER (WHERE sub_diff IS NOT NULL) OVER w_last5 AS median_sub_diff_last_5,

            -- Optional pace-ish (medians of raw counts) over last 5 prior fights
            median(str_for)     FILTER (WHERE str_for     IS NOT NULL) OVER w_last5 AS median_str_for_last_5,
            median(str_against) FILTER (WHERE str_against IS NOT NULL) OVER w_last5 AS median_str_against_last_5,
            median(td_for)      FILTER (WHERE td_for      IS NOT NULL) OVER w_last5 AS median_td_for_last_5,
            median(td_against)  FILTER (WHERE td_against  IS NOT NULL) OVER w_last5 AS median_td_against_last_5,

            -- === UFC bonus rates (pre-fight) ===
            AVG(CASE WHEN is_ufc THEN has_any_bonus ELSE 0 END) OVER w_all AS ufc_bonus_rate,
            AVG(CASE WHEN is_ufc THEN has_perf_bonus ELSE 0 END) OVER w_all AS ufc_perf_rate,

            -- === Career-level averages (pace/volume per fight, pre-fight only) ===
            AVG(str_for)  FILTER (WHERE str_for  IS NOT NULL) OVER w_all AS avg_str_for_career,
            AVG(str_against) FILTER (WHERE str_against IS NOT NULL) OVER w_all AS avg_str_against_career,
            AVG(td_for)   FILTER (WHERE td_for   IS NOT NULL) OVER w_all AS avg_td_for_career,
            AVG(td_against)  FILTER (WHERE td_against IS NOT NULL) OVER w_all AS avg_td_against_career,
            AVG(kd_diff)  FILTER (WHERE kd_diff  IS NOT NULL) OVER w_all AS avg_kd_diff_career,
            AVG(str_diff) FILTER (WHERE str_diff IS NOT NULL) OVER w_all AS avg_str_diff_career,
            AVG(td_diff)  FILTER (WHERE td_diff  IS NOT NULL) OVER w_all AS avg_td_diff_career,

            -- === Variance in performance (consistency indicator) ===
            STDDEV_SAMP(str_diff) FILTER (WHERE str_diff IS NOT NULL) OVER w_all AS std_str_diff_career,
            STDDEV_SAMP(td_diff)  FILTER (WHERE td_diff  IS NOT NULL) OVER w_all AS std_td_diff_career,

            -- === Win rate last 3 (more recent signal) ===
            CAST(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) OVER w_last3 AS DOUBLE)
                / NULLIF(COUNT(*) FILTER (WHERE result IN ('win','loss')) OVER w_last3, 0) AS win_rate_last_3,

            -- === Loss method vulnerability (career, from losses only) ===
            CAST(SUM(CASE WHEN result='loss' AND method_category IN ('KO_TKO','SUB') THEN 1 ELSE 0 END) OVER w_all AS DOUBLE)
                / NULLIF(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) OVER w_all, 0) AS finish_loss_rate

        FROM fights_norm f

        WINDOW
            w_all AS (
                PARTITION BY organization, fighter_id
                ORDER BY event_date, event_url, round, time, fight_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            w_last3 AS (
                PARTITION BY organization, fighter_id
                ORDER BY event_date, event_url, round, time, fight_id
                ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING
            ),
            w_last5 AS (
                PARTITION BY organization, fighter_id
                ORDER BY event_date, event_url, round, time, fight_id
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ),
            w_streak AS (
                PARTITION BY organization, fighter_id
                ORDER BY event_date, event_url, round, time, fight_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            )
    ),

    prefight AS (
        SELECT
            pb.*,

            -- Bayesian shrinkage for win-method rates (conditional on wins)
            (
                (1.0 + p.prior_strength * g.global_ko_rate + COALESCE(pb.ko_wins_count, 0)) /
                (2.0 + p.prior_strength + COALESCE(pb.wins_count, 0))
            ) AS ko_win_rate_shrunk,
            (
                (1.0 + p.prior_strength * g.global_sub_rate + COALESCE(pb.sub_wins_count, 0)) /
                (2.0 + p.prior_strength + COALESCE(pb.wins_count, 0))
            ) AS sub_win_rate_shrunk,
            (
                (1.0 + p.prior_strength * g.global_finish_rate + COALESCE(pb.finish_wins_count, 0)) /
                (2.0 + p.prior_strength + COALESCE(pb.wins_count, 0))
            ) AS finish_win_rate_shrunk

        FROM prefight_base pb
        CROSS JOIN params p
        CROSS JOIN global_rates g
    ),

    paired AS (
        SELECT
            -- identifiers / display columns
            a.fight_id,
            a.bout_key,
            a.organization,
            a.event_id,
            a.event_date,
            a.event_url,
            a.event_name,

            a.fighter_id,
            a.fighter_name,
            a.fighter_name_plain,
            a.fighter_name_display,
            a.opponent_id,
            a.opponent_name,
            a.opponent_name_plain,
            a.opponent_name_display,

            a.weight_class,
            a.is_title_fight,
            a.is_ufc,
            a.y_win,

            -- === Fighter profile (static) ===
            a.fighter_stance,
            a.fighter_reach,
            a.fighter_height,
            a.fighter_age,
            b.fighter_stance      AS opp_fighter_stance,
            b.fighter_reach       AS opp_fighter_reach,
            b.fighter_height      AS opp_fighter_height,
            b.fighter_age         AS opp_fighter_age,

            -- === Stance matchup categorical ===
            CASE
                WHEN a.fighter_stance IS NULL OR b.fighter_stance IS NULL THEN 'unknown'
                WHEN a.fighter_stance = b.fighter_stance THEN 'mirror'
                WHEN a.fighter_stance = 'Southpaw' AND b.fighter_stance = 'Orthodox' THEN 'southpaw_vs_orthodox'
                WHEN a.fighter_stance = 'Orthodox' AND b.fighter_stance = 'Southpaw' THEN 'orthodox_vs_southpaw'
                ELSE 'other'
            END AS stance_matchup,

            a.fighter_status,
            a.days_since_last_fight,
            a.win_streak_entering,
            a.loss_streak_entering,

            -- fighter pre-fight features
            a.fights_count,
            a.win_rate_all,
            a.win_rate_last_5,
            a.net_wins_last_5,
            a.finish_win_rate,
            a.finish_win_rate_shrunk,
            a.ko_win_rate,
            a.ko_win_rate_shrunk,
            a.sub_win_rate,
            a.sub_win_rate_shrunk,

            a.last5_str_diff_n,
            a.last5_td_diff_n,
            a.last5_kd_diff_n,

            a.median_str_diff_last_5,
            a.median_td_diff_last_5,
            a.median_kd_diff_last_5,
            a.ufc_perf_rate,

            -- fighter profile features (numeric, career-level)
            a.avg_str_for_career        AS prof_avg_str_for,
            a.avg_str_against_career     AS prof_avg_str_against,
            a.avg_td_for_career          AS prof_avg_td_for,
            a.avg_td_against_career       AS prof_avg_td_against,
            a.avg_kd_diff_career          AS prof_avg_kd_diff,
            a.avg_str_diff_career         AS prof_avg_str_diff,
            a.avg_td_diff_career          AS prof_avg_td_diff,
            a.std_str_diff_career         AS prof_std_str_diff,
            a.std_td_diff_career          AS prof_std_td_diff,
            a.win_rate_last_3             AS prof_win_rate_last_3,
            a.finish_loss_rate            AS prof_finish_loss_rate,
            a.ko_loss_rate                AS prof_ko_loss_rate,
            a.sub_loss_rate               AS prof_sub_loss_rate,
            a.dec_loss_rate               AS prof_dec_loss_rate,
            -- missing-indicator flags (1 if fighter has no prior fights)
            CASE WHEN a.fights_count IS NULL OR a.fights_count = 0 THEN 1 ELSE 0 END AS prof_missing,

            -- opponent pre-fight features
            b.fights_count           AS opp_fights_count,
            b.win_rate_all           AS opp_win_rate_all,
            b.win_rate_last_5        AS opp_win_rate_last_5,
            b.net_wins_last_5        AS opp_net_wins_last_5,
            b.finish_win_rate        AS opp_finish_win_rate,
            b.finish_win_rate_shrunk AS opp_finish_win_rate_shrunk,
            b.ko_win_rate            AS opp_ko_win_rate,
            b.ko_win_rate_shrunk      AS opp_ko_win_rate_shrunk,
            b.sub_win_rate           AS opp_sub_win_rate,
            b.sub_win_rate_shrunk     AS opp_sub_win_rate_shrunk,

            b.last5_str_diff_n       AS opp_last5_str_diff_n,
            b.last5_td_diff_n        AS opp_last5_td_diff_n,
            b.last5_kd_diff_n        AS opp_last5_kd_diff_n,

            b.median_str_diff_last_5 AS opp_median_str_diff_last_5,
            b.median_td_diff_last_5  AS opp_median_td_diff_last_5,
            b.median_kd_diff_last_5  AS opp_median_kd_diff_last_5,
            b.ufc_perf_rate          AS opp_ufc_perf_rate,

            -- opponent profile features (numeric, career-level)
            b.avg_str_for_career        AS opp_prof_avg_str_for,
            b.avg_str_against_career     AS opp_prof_avg_str_against,
            b.avg_td_for_career          AS opp_prof_avg_td_for,
            b.avg_td_against_career       AS opp_prof_avg_td_against,
            b.avg_kd_diff_career          AS opp_prof_avg_kd_diff,
            b.avg_str_diff_career         AS opp_prof_avg_str_diff,
            b.avg_td_diff_career          AS opp_prof_avg_td_diff,
            b.std_str_diff_career         AS opp_prof_std_str_diff,
            b.std_td_diff_career          AS opp_prof_std_td_diff,
            b.win_rate_last_3             AS opp_prof_win_rate_last_3,
            b.finish_loss_rate            AS opp_prof_finish_loss_rate,
            b.ko_loss_rate                AS opp_prof_ko_loss_rate,
            b.sub_loss_rate               AS opp_prof_sub_loss_rate,
            b.dec_loss_rate               AS opp_prof_dec_loss_rate,
            CASE WHEN b.fights_count IS NULL OR b.fights_count = 0 THEN 1 ELSE 0 END AS opp_prof_missing,

            b.fighter_status         AS opp_fighter_status,
            b.days_since_last_fight  AS opp_days_since_last_fight,
            b.win_streak_entering    AS opp_win_streak_entering,
            b.loss_streak_entering   AS opp_loss_streak_entering

        FROM prefight a
        LEFT JOIN prefight b
          ON b.bout_key = a.bout_key
         AND b.organization = a.organization
         AND b.fighter_id = a.opponent_id
         AND b.opponent_id = a.fighter_id
    )

    SELECT
        *,
        -- deltas (fighter - opponent)
        (win_rate_all - opp_win_rate_all) AS delta_win_rate_all,
        (win_rate_last_5 - opp_win_rate_last_5) AS delta_win_rate_last_5,
        (finish_win_rate - opp_finish_win_rate) AS delta_finish_win_rate,
        (finish_win_rate_shrunk - opp_finish_win_rate_shrunk) AS delta_finish_win_rate_shrunk,
        (ko_win_rate - opp_ko_win_rate) AS delta_ko_win_rate,
        (ko_win_rate_shrunk - opp_ko_win_rate_shrunk) AS delta_ko_win_rate_shrunk,
        (sub_win_rate - opp_sub_win_rate) AS delta_sub_win_rate,
        (sub_win_rate_shrunk - opp_sub_win_rate_shrunk) AS delta_sub_win_rate_shrunk,

        -- Only compute last-5 median deltas when both sides have any last-5 observations
        CASE
            WHEN last5_str_diff_n > 0 AND opp_last5_str_diff_n > 0
            THEN (median_str_diff_last_5 - opp_median_str_diff_last_5)
            ELSE NULL
        END AS delta_median_str_diff_last_5,

        CASE
            WHEN last5_td_diff_n > 0 AND opp_last5_td_diff_n > 0
            THEN (median_td_diff_last_5 - opp_median_td_diff_last_5)
            ELSE NULL
        END AS delta_median_td_diff_last_5,

        CASE
            WHEN last5_kd_diff_n > 0 AND opp_last5_kd_diff_n > 0
            THEN (median_kd_diff_last_5 - opp_median_kd_diff_last_5)
            ELSE NULL
        END AS delta_median_kd_diff_last_5,

        (ufc_perf_rate - opp_ufc_perf_rate) AS delta_ufc_perf_rate,

        -- New deltas for inactivity and streaks
        (days_since_last_fight - opp_days_since_last_fight) AS delta_days_since_last_fight,
        (win_streak_entering - opp_win_streak_entering) AS delta_win_streak_entering,
        (loss_streak_entering - opp_loss_streak_entering) AS delta_loss_streak_entering,

        -- === NEW: Profile deltas (career averages) ===
        (prof_avg_str_diff - opp_prof_avg_str_diff) AS delta_avg_str_diff_career,
        (prof_avg_td_diff - opp_prof_avg_td_diff)   AS delta_avg_td_diff_career,
        (prof_avg_kd_diff - opp_prof_avg_kd_diff)   AS delta_avg_kd_diff_career,

        -- === NEW: Physical deltas (strip trailing " and ' from reach/height) ===
        (TRY_CAST(REGEXP_REPLACE(fighter_reach, '[^0-9.]', '', 'g') AS DOUBLE)
            - TRY_CAST(REGEXP_REPLACE(opp_fighter_reach, '[^0-9.]', '', 'g') AS DOUBLE)) AS delta_reach,
        (TRY_CAST(REGEXP_REPLACE(fighter_height, '[^0-9.]', '', 'g') AS DOUBLE)
            - TRY_CAST(REGEXP_REPLACE(opp_fighter_height, '[^0-9.]', '', 'g') AS DOUBLE)) AS delta_height,
        (TRY_CAST(fighter_age AS DOUBLE) - TRY_CAST(opp_fighter_age AS DOUBLE)) AS delta_age,

        -- === NEW: Win rate last 3 delta ===
        (prof_win_rate_last_3 - opp_prof_win_rate_last_3) AS delta_win_rate_last_3,

        -- === NEW: Vulnerability deltas (finish-loss rate) ===
        (prof_finish_loss_rate - opp_prof_finish_loss_rate) AS delta_finish_loss_rate,

        -- === NEW: Interaction features (top-feature expansions) ===
        -- Momentum composite: streak quality
        (win_streak_entering - opp_win_streak_entering)
            * COALESCE(win_rate_last_5 - opp_win_rate_last_5, 0.0) AS delta_hot_streak_quality,

        -- Log-layoff (diminishing returns on rest)
        LN(GREATEST(COALESCE(days_since_last_fight, 365), 1)) AS prof_layoff_log,
        LN(GREATEST(COALESCE(opp_days_since_last_fight, 365), 1)) AS opp_prof_layoff_log,
        LN(GREATEST(COALESCE(days_since_last_fight, 365), 1))
            - LN(GREATEST(COALESCE(opp_days_since_last_fight, 365), 1)) AS delta_layoff_log,

        -- Consistency delta (fighter with lower std is more consistent)
        (COALESCE(prof_std_str_diff, 0.0) - COALESCE(opp_prof_std_str_diff, 0.0)) AS delta_str_consistency,

        -- Experience delta
        (fights_count - opp_fights_count) AS delta_fights_count,

        -- Striker vs grappler mismatch
        CASE WHEN last5_str_diff_n > 0 AND opp_last5_str_diff_n > 0
             THEN ABS(COALESCE(median_str_diff_last_5, 0) - COALESCE(median_td_diff_last_5, 0))
                - ABS(COALESCE(opp_median_str_diff_last_5, 0) - COALESCE(opp_median_td_diff_last_5, 0))
             ELSE NULL
        END AS delta_style_breadth

    FROM paired
    ;
    """

    if args.rebuild:
        # prefight_fight_features may exist as VIEW (current) or TABLE (legacy).
        # (DuckDB's IF EXISTS only suppresses "not found", not "wrong type")
        conn.execute("DROP VIEW IF EXISTS gold.prefight_fight_features")
        conn.execute("DROP TABLE IF EXISTS gold.prefight_fight_features")
        conn.execute("DROP TABLE IF EXISTS gold.prefight_features")

    start = datetime.now()
    conn.execute(sql)
    # Backwards-compatible alias (older code and notebooks expect this name).
    # Drop both forms in case it was previously a table
    conn.execute("DROP VIEW IF EXISTS gold.prefight_fight_features")
    conn.execute("DROP TABLE IF EXISTS gold.prefight_fight_features")
    conn.execute("CREATE OR REPLACE VIEW gold.prefight_fight_features AS SELECT * FROM gold.prefight_features")

    n = conn.execute("SELECT COUNT(*) FROM gold.prefight_features").fetchone()[0]
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Created gold.prefight_features with {n:,} rows in {elapsed:.2f}s")
    conn.close()



if __name__ == "__main__":
    main()