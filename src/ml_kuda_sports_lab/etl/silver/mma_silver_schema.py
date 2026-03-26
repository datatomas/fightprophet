#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Silver Layer ETL for MMA Data (UFC only).

Transforms bronze layer data into unified silver tables.

Tables produced:
  - silver.events
  - silver.fighters
  - silver.fights
  - silver.fighter_stats

Notes:
  - Each run DROPS and recreates these silver tables.
  - finish_rate metrics are "finish win rate" = wins by SUB or KO/TKO (from silver.fights.method).
"""

import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
from typing import List

import duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class SilverLayerETL:
    """ETL pipeline for creating silver layer tables from bronze source data"""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())
        self.conn = duckdb.connect(self.db_path)
        self.conn.execute("PRAGMA enable_progress_bar = FALSE")
        self.conn.execute("CREATE SCHEMA IF NOT EXISTS silver")

    # -----------------------
    # Small helpers
    # -----------------------
    def table_exists(self, schema: str, table: str) -> bool:
        q = """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = ? AND table_name = ?
        LIMIT 1
        """
        return self.conn.execute(q, [schema, table]).fetchone() is not None

    def table_columns(self, schema: str, table: str) -> List[str]:
        q = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """
        rows = self.conn.execute(q, [schema, table]).fetchall()
        return [str(r[0]) for r in rows if r and len(r) > 0]

    def ensure_bronze_ufc_fights_metadata_cols(self) -> None:
        """
        Ensures these columns exist so create_fights_table never breaks on older DBs:
          - weight_class (VARCHAR)
          - is_title_fight (BOOLEAN)
          - bonus_tags (VARCHAR)
        """
        if not self.table_exists("bronze", "ufc_fights"):
            raise RuntimeError("Required table bronze.ufc_fights does not exist")

        logger.info("Ensuring bronze.ufc_fights has metadata columns (weight/title/bonus)...")
        self.conn.execute("ALTER TABLE bronze.ufc_fights ADD COLUMN IF NOT EXISTS weight_class VARCHAR;")
        self.conn.execute("ALTER TABLE bronze.ufc_fights ADD COLUMN IF NOT EXISTS is_title_fight BOOLEAN;")
        self.conn.execute("ALTER TABLE bronze.ufc_fights ADD COLUMN IF NOT EXISTS bonus_tags VARCHAR;")

    # -----------------------
    # Steps
    # -----------------------
    def create_events_table(self) -> None:
        logger.info("Creating silver.events table...")

        query = """
        DROP TABLE IF EXISTS silver.events;
        CREATE TABLE silver.events AS
        WITH raw AS (
            -- Primary source: events scraper
            SELECT
                CONCAT('ufc_', event_id) AS event_id,
                'UFC' AS organization,
                NULLIF(TRIM(CAST(event_name AS VARCHAR)), '') AS event_name,
                COALESCE(
                    TRY_CAST(event_date AS TIMESTAMP),
                    STRPTIME(CAST(event_date AS VARCHAR), '%B %d, %Y'),
                    STRPTIME(CAST(event_date AS VARCHAR), '%Y-%m-%d'),
                    STRPTIME(CAST(event_date AS VARCHAR), '%d/%m/%Y'),
                    STRPTIME(CAST(event_date AS VARCHAR), '%m/%d/%Y')
                ) AS event_date,
                NULLIF(TRIM(CAST(location AS VARCHAR)), '') AS location,
                NULLIF(TRIM(CAST(event_url AS VARCHAR)), '') AS event_url,
                NULLIF(TRIM(CAST(event_status AS VARCHAR)), '') AS status,
                scraped_at,
                REGEXP_REPLACE(LOWER(TRIM(CAST(event_url AS VARCHAR))), '/+$', '') AS event_url_key
            FROM bronze.ufc_events

            UNION ALL

            -- Backfill source: fights scraper can discover upcoming events before events ETL is rebuilt.
            -- It carries event_url + sometimes a more specific event name in bronze.ufc_fights.event.
            SELECT
                CONCAT('ufc_', REGEXP_EXTRACT(REGEXP_REPLACE(LOWER(TRIM(CAST(event_url AS VARCHAR))), '/+$', ''), '.*/([^/]+)$', 1)) AS event_id,
                'UFC' AS organization,
                NULLIF(TRIM(CAST(event AS VARCHAR)), '') AS event_name,
                NULL::TIMESTAMP AS event_date,
                NULL::VARCHAR AS location,
                NULLIF(TRIM(CAST(event_url AS VARCHAR)), '') AS event_url,
                'upcoming' AS status,
                CURRENT_TIMESTAMP AS scraped_at,
                REGEXP_REPLACE(LOWER(TRIM(CAST(event_url AS VARCHAR))), '/+$', '') AS event_url_key
            FROM bronze.ufc_fights
            WHERE COALESCE(TRIM(CAST(event_url AS VARCHAR)), '') <> ''
        )
        SELECT
            MAX(event_id) AS event_id,
            organization,
            -- Prefer the most specific (longest) non-empty name.
            ARG_MAX(event_name, LENGTH(COALESCE(event_name, ''))) AS event_name,
            MAX(event_date) AS event_date,
            ARG_MAX(location, LENGTH(COALESCE(location, ''))) AS location,
            ARG_MAX(event_url, LENGTH(COALESCE(event_url, ''))) AS event_url,
            CASE
                WHEN BOOL_OR(LOWER(COALESCE(status, '')) = 'completed') THEN 'completed'
                WHEN BOOL_OR(LOWER(COALESCE(status, '')) = 'upcoming') THEN 'upcoming'
                ELSE ARG_MAX(status, LENGTH(COALESCE(status, '')))
            END AS status,
            MAX(scraped_at) AS scraped_at
        FROM raw
        WHERE COALESCE(event_url_key, '') <> ''
        GROUP BY organization, event_url_key
        """
        self.conn.execute(query)
        count = self.conn.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0]
        logger.info(f"Created silver.events with {count} records")

    def create_fighters_table(self) -> None:
        logger.info("Creating silver.fighters table...")

        # Needed to identify title fights for belt holders.
        self.ensure_bronze_ufc_fights_metadata_cols()

        if not self.table_exists("silver", "events"):
            logger.warning("silver.events missing; creating it now (required for belt holders join).")
            self.create_events_table()

        cols = {c.lower(): c for c in self.table_columns("bronze", "ufc_fighters")}
        dob_col = None
        for candidate in ("dob", "date_of_birth", "birth_date", "birthday"):
            if candidate in cols:
                dob_col = cols[candidate]
                break

        if dob_col:
            dob_expr = f"NULLIF(TRIM(CAST({dob_col} AS VARCHAR)), '')"
            dob_date_expr = (
                f"COALESCE(TRY_CAST({dob_expr} AS DATE), "
                f"TRY_STRPTIME({dob_expr}, '%b %d, %Y')::DATE, "
                f"TRY_STRPTIME({dob_expr}, '%B %d, %Y')::DATE)"
            )
            dob_sql_expr = f"{dob_date_expr} AS dob"
            age_expr = f"""
            CASE
                WHEN {dob_date_expr} IS NULL THEN NULL
                ELSE
                    (EXTRACT(year FROM CURRENT_DATE) - EXTRACT(year FROM {dob_date_expr}))
                    - CASE
                        WHEN EXTRACT(month FROM CURRENT_DATE) < EXTRACT(month FROM {dob_date_expr}) THEN 1
                        WHEN EXTRACT(month FROM CURRENT_DATE) = EXTRACT(month FROM {dob_date_expr})
                         AND EXTRACT(day FROM CURRENT_DATE) < EXTRACT(day FROM {dob_date_expr}) THEN 1
                        ELSE 0
                      END
            END AS age
            """
        else:
            logger.warning("bronze.ufc_fighters has no DOB column; setting silver.fighters.age = NULL")
            dob_sql_expr = "NULL::DATE AS dob"
            age_expr = "NULL::INTEGER AS age"

        # Belt holders: latest title-fight winner per weight class.
        # Stored as a comma-separated list of weight classes for multi-division champs.
        belt_expr = "COALESCE(ch.belt_weight_classes, NULLIF(TRIM(CAST(u.belt AS VARCHAR)), '')) AS belt"

        query = """
        DROP TABLE IF EXISTS silver.fighters;
        CREATE TABLE silver.fighters AS
        WITH
        title_wins AS (
            SELECT
                f.fighter_id,
                NULLIF(TRIM(f.weight_class), '') AS weight_class,
                CAST(e.event_date AS DATE) AS event_date,
                lower(COALESCE(f.result, '')) AS result
                        FROM bronze.ufc_fights f
                        JOIN silver.events e
                            ON e.organization = 'UFC'
                         AND REGEXP_REPLACE(LOWER(TRIM(CAST(e.event_url AS VARCHAR))), '/+$', '')
                                 = REGEXP_REPLACE(LOWER(TRIM(CAST(f.event_url AS VARCHAR))), '/+$', '')
            WHERE COALESCE(f.is_title_fight, FALSE) = TRUE
              AND NULLIF(TRIM(f.weight_class), '') IS NOT NULL
              AND e.event_date IS NOT NULL
              AND lower(COALESCE(f.result, '')) = 'win'
        ),
        latest_title_wins AS (
            SELECT fighter_id, weight_class
            FROM (
                SELECT
                    fighter_id,
                    weight_class,
                    event_date,
                    row_number() OVER (PARTITION BY weight_class ORDER BY event_date DESC, fighter_id) AS rn
                FROM title_wins
            ) x
            WHERE rn = 1
        ),
        champions AS (
            SELECT
                fighter_id,
                string_agg(weight_class, ', ' ORDER BY weight_class) AS belt_weight_classes
            FROM latest_title_wins
            GROUP BY fighter_id
        ),
        last_fights AS (
            SELECT
                f.fighter_id,
                MAX(CAST(e.event_date AS DATE)) AS last_fight_date
                        FROM bronze.ufc_fights f
                        JOIN silver.events e
                            ON e.organization = 'UFC'
                         AND REGEXP_REPLACE(LOWER(TRIM(CAST(e.event_url AS VARCHAR))), '/+$', '')
                                 = REGEXP_REPLACE(LOWER(TRIM(CAST(f.event_url AS VARCHAR))), '/+$', '')
            WHERE e.event_date IS NOT NULL
            GROUP BY f.fighter_id
        )
        ,base AS (
            SELECT
                CONCAT('ufc_', u.fighter_id) AS fighter_id,
                'UFC' AS organization,
                first_name,
                last_name,
                nickname,

                NULLIF(
                    TRIM(
                        COALESCE(first_name, '') ||
                        CASE
                            WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                            ELSE ''
                        END
                    ),
                    ''
                ) AS fighter_name_plain_raw,
                NULLIF(
                    CASE
                        WHEN NULLIF(TRIM(nickname), '') IS NOT NULL
                        THEN
                            NULLIF(
                                TRIM(
                                    COALESCE(first_name, '') ||
                                    CASE
                                        WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            ) || ' (' || TRIM(nickname) || ')'
                        ELSE
                            NULLIF(
                                TRIM(
                                    COALESCE(first_name, '') ||
                                    CASE
                                        WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            )
                    END,
                    ''
                ) AS fighter_name_display_raw,
                NULLIF(
                    CASE
                        WHEN NULLIF(TRIM(nickname), '') IS NOT NULL
                        THEN
                            NULLIF(
                                TRIM(
                                    COALESCE(first_name, '') ||
                                    CASE
                                        WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            ) || ' (' || TRIM(nickname) || ')'
                        ELSE
                            NULLIF(
                                TRIM(
                                    COALESCE(first_name, '') ||
                                    CASE
                                        WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            )
                    END,
                    ''
                ) AS fighter_name_raw,
                lower(
                    regexp_replace(
                        TRIM(
                            COALESCE(first_name, '') || ' ' ||
                            COALESCE(last_name, '') || ' ' ||
                            COALESCE(NULLIF(TRIM(nickname), ''), '')
                        ),
                        '[^a-z0-9]+',
                        '_'
                    )
                ) AS fighter_name_key,
                height,
                weight,
                reach,
                stance,
                wins,
                losses,
                draws,
                {belt_expr},
                CASE
                    WHEN lf.last_fight_date IS NOT NULL
                     AND date_diff('day', lf.last_fight_date, CURRENT_DATE) > 730
                    THEN 'inactive'
                    ELSE 'active'
                END AS fighter_status,
                detail_url,
                NULL::TEXT AS country,
                {dob_sql_expr},
                {age_expr},
                NULL::TEXT AS team,
                NULL::TIMESTAMP AS scraped_at
            FROM bronze.ufc_fighters u
            LEFT JOIN champions ch
              ON ch.fighter_id = u.fighter_id
            LEFT JOIN last_fights lf
              ON lf.fighter_id = u.fighter_id
        ),
        disambig AS (
            SELECT
                b.*,
                COUNT(*) OVER (
                    PARTITION BY organization,
                    COALESCE(b.fighter_name_plain_raw, b.fighter_name_display_raw, b.fighter_name_raw, b.fighter_id)
                ) AS name_cnt,
                NULLIF(TRIM(b.weight), '') AS weight_t,
                NULLIF(TRIM(b.height), '') AS height_t,
                CASE
                    WHEN b.wins IS NULL AND b.losses IS NULL AND b.draws IS NULL THEN NULL
                    ELSE
                        COALESCE(CAST(b.wins AS VARCHAR), '?') || '-' || COALESCE(CAST(b.losses AS VARCHAR), '?')
                        || CASE
                            WHEN b.draws IS NOT NULL AND b.draws <> 0 THEN '-' || CAST(b.draws AS VARCHAR)
                            ELSE ''
                           END
                END AS record_t
            FROM base b
        )
        SELECT
            fighter_id,
            organization,
            first_name,
            last_name,
            nickname,
            COALESCE(fighter_name_plain_raw, fighter_id) AS fighter_name_plain,
            CASE
                WHEN NULLIF(TRIM(nickname), '') IS NULL AND name_cnt > 1
                THEN
                    COALESCE(fighter_name_display_raw, fighter_name_plain_raw, fighter_id)
                    || ' [' ||
                    COALESCE(
                        NULLIF(
                            TRIM(
                                COALESCE(weight_t, '') ||
                                CASE WHEN weight_t IS NOT NULL AND height_t IS NOT NULL THEN ', ' ELSE '' END ||
                                COALESCE(height_t, '') ||
                                CASE
                                    WHEN (weight_t IS NOT NULL OR height_t IS NOT NULL) AND record_t IS NOT NULL THEN ', '
                                    WHEN (weight_t IS NULL AND height_t IS NULL) AND record_t IS NOT NULL THEN ''
                                    ELSE ''
                                END ||
                                COALESCE(record_t, '')
                            ),
                            ''
                        ),
                        right(fighter_id, 8)
                    )
                    || ']'
                ELSE COALESCE(fighter_name_display_raw, fighter_name_plain_raw, fighter_id)
            END AS fighter_name_display,
            -- Canonical name used downstream: always prefer display, and disambiguate duplicates.
            CASE
                WHEN NULLIF(TRIM(nickname), '') IS NULL AND name_cnt > 1
                THEN
                    COALESCE(fighter_name_display_raw, fighter_name_plain_raw, fighter_id)
                    || ' [' ||
                    COALESCE(
                        NULLIF(
                            TRIM(
                                COALESCE(weight_t, '') ||
                                CASE WHEN weight_t IS NOT NULL AND height_t IS NOT NULL THEN ', ' ELSE '' END ||
                                COALESCE(height_t, '') ||
                                CASE
                                    WHEN (weight_t IS NOT NULL OR height_t IS NOT NULL) AND record_t IS NOT NULL THEN ', '
                                    WHEN (weight_t IS NULL AND height_t IS NULL) AND record_t IS NOT NULL THEN ''
                                    ELSE ''
                                END ||
                                COALESCE(record_t, '')
                            ),
                            ''
                        ),
                        right(fighter_id, 8)
                    )
                    || ']'
                ELSE COALESCE(fighter_name_raw, fighter_name_display_raw, fighter_name_plain_raw, fighter_id)
            END AS fighter_name,
            fighter_name_key,
            height,
            weight,
            reach,
            stance,
            wins,
            losses,
            draws,
            belt,
            fighter_status,
            detail_url,
            country,
            dob,
            age,
            team,
            scraped_at
        FROM disambig
        """
        self.conn.execute(query.format(age_expr=age_expr, dob_sql_expr=dob_sql_expr, belt_expr=belt_expr))
        count = self.conn.execute("SELECT COUNT(*) FROM silver.fighters").fetchone()[0]
        logger.info(f"Created silver.fighters with {count} records")

    def create_fights_table(self) -> None:
        """
        UFC-only unified fights table.

        UFC:
          - keeps weight_class / is_title_fight / bonus_tags from bronze.ufc_fights
          - pulls event_date + event_status from silver.events
          - adds fighter_name/fighter_url and opponent_name/opponent_url when possible

        Note: ONE Championship is no longer included.
        """
        logger.info("Creating silver.fights table...")

        self.ensure_bronze_ufc_fights_metadata_cols()

        if not self.table_exists("silver", "events"):
            logger.warning("silver.events missing; creating it now (required for fights join).")
            self.create_events_table()

        # normalize finish methods so downstream stats can rely on consistent categories
        self.conn.execute(
            r"""
            CREATE OR REPLACE TEMP MACRO clean_method(method_text) AS (
                CASE
                    WHEN method_text IS NULL THEN NULL
                    WHEN TRIM(method_text) = '' THEN NULL
                    WHEN UPPER(TRIM(method_text)) IN ('HLAING','SITJANIM','THE','VS') THEN NULL
                    ELSE REGEXP_REPLACE(UPPER(TRIM(method_text)), '\\s+', ' ')
                END
            );
            """
        )

        self.conn.execute(
            r"""
            CREATE OR REPLACE TEMP MACRO method_category(method_text) AS (
                CASE
                    WHEN clean_method(method_text) IS NULL THEN 'NC_DQ'
                    WHEN REGEXP_MATCHES(
                        clean_method(method_text),
                        '.*(NO CONTEST|\\bNC\\b|\\bCNC\\b|OVER.?TURN|INJURY|FOUL|DQ|DISQUAL).*'
                    ) THEN 'NC_DQ'
                    WHEN REGEXP_MATCHES(
                        clean_method(method_text),
                        '.*(SUB|CHOKE|ARMBAR|TRIANGLE|KIMURA|ARM TRIANGLE|NECK CRANK|KEYLOCK|GUILLOTINE|GOGO|D''ARCE|ANACONDA|OMOPLATA|KNEEBAR|HEEL HOOK|LEGLOCK|LOCK|TWISTER|SUL?OEV|VON FLUE|NORTH-SOUTH|SCARF|CALF SLICER|PERUVIAN|STRETCH).*'
                    ) THEN 'SUB'
                    WHEN REGEXP_MATCHES(
                        clean_method(method_text),
                        '.*(U-DEC|S-DEC|M-DEC|DECISION|\\bDEC\\b|MAJORITY).*'
                    ) THEN 'DEC'
                    WHEN REGEXP_MATCHES(
                        clean_method(method_text),
                        '.*(\\bKO\\b|KO/TKO|\\bTKO\\b|PUNCH|KICK|ELBOW|KNEE|SLAM|HEADBUTT|STOMP|DR STOP|DOCTOR|CUT|SPINNING|KNEES).*'
                    ) THEN 'KO_TKO'
                    ELSE 'KO_TKO'
                END
            );
            """
        )

        # NEW: method normalization (abbr) + high-level class
        self.conn.execute(
            r"""
            CREATE OR REPLACE TEMP MACRO method_norm(method_text) AS (
                CASE
                    WHEN clean_method(method_text) IS NULL THEN NULL

                    -- DQ
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bDQ\\b.*|.*DISQUAL.*') THEN 'DQ'

                    -- KO/TKO variants
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*DOCTOR.?S? STOPPAGE.*|.*DOCTOR.*STOPPAGE.*|.*DR STOP.*') THEN 'TKO'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bKO/TKO\\b.*') THEN 'KO_TKO'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bTKO\\b.*') THEN 'TKO'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bKO\\b.*') THEN 'KO'

                    -- SUB
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bSUBMISSION\\b.*|.*\\bSUB\\b.*') THEN 'SUB'

                    -- Decisions
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*DECISION.*UNANIMOUS.*|.*\\bU-DEC\\b.*|.*\\bUD\\b.*') THEN 'UD'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*DECISION.*SPLIT.*|.*\\bS-DEC\\b.*|.*\\bSD\\b.*') THEN 'SD'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*DECISION.*MAJORITY.*|.*\\bM-DEC\\b.*|.*\\bMD\\b.*') THEN 'MD'
                    WHEN REGEXP_MATCHES(clean_method(method_text), '.*\\bDECISION\\b.*|.*\\bDEC\\b.*') THEN 'DECISION'

                    ELSE clean_method(method_text)
                END
            );

            CREATE OR REPLACE TEMP MACRO method_class(method_text) AS (
                CASE
                    WHEN method_norm(method_text) IN ('KO','TKO','KO_TKO') THEN 'KO_TKO'
                    WHEN method_norm(method_text) = 'SUB' THEN 'SUB'
                    WHEN method_norm(method_text) IN ('UD','SD','MD','DECISION') THEN 'DECISION'
                    WHEN method_norm(method_text) = 'DQ' THEN 'DQ'
                    ELSE
                        CASE
                            WHEN method_category(method_text) = 'DEC' THEN 'DECISION'
                            ELSE method_category(method_text)
                        END
                END
            );
            """
        )

        query = """
        DROP TABLE IF EXISTS silver.fights;
        CREATE TABLE silver.fights AS
        WITH
        ufc_events AS (
            SELECT
                event_id,
                event_url,
                event_name,
                location,
                event_date,
                status,
                REGEXP_REPLACE(LOWER(TRIM(CAST(event_url AS VARCHAR))), '/+$', '') AS event_url_key
            FROM silver.events
            WHERE organization = 'UFC'
        ),
        base_fights AS (

        -- UFC fights
        SELECT
            CONCAT(
                'ufc_',
                f.fighter_id,
                '_',
                COALESCE(
                    NULLIF(regexp_replace(lower(f.opponent_url), '.*/fighter-details/', ''), ''),
                    NULLIF(regexp_replace(lower(f.opponent), '[^a-z0-9]+', '_'), '')
                ),
                '_',
                COALESCE(ev.event_id, CONCAT('ufc_event_', f.event_url))
            ) AS fight_id,
            'UFC' AS organization,
            COALESCE(ev.event_id, CONCAT('ufc_', f.event_url)) AS event_id,
            COALESCE(
                ev.event_date,
                TRY_CAST(bev.event_date AS TIMESTAMP),
                TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%B %d, %Y'),
                TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%Y-%m-%d'),
                TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%d/%m/%Y'),
                TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%m/%d/%Y')
            ) AS event_date,
            ev.status AS event_status,
            f.event_url AS event_url,
            CASE
                WHEN NULLIF(TRIM(CAST(f.event AS VARCHAR)), '') IS NOT NULL
                 AND (
                      ev.event_name IS NULL
                      OR TRIM(CAST(ev.event_name AS VARCHAR)) = ''
                      OR LOWER(TRIM(CAST(ev.event_name AS VARCHAR))) = 'ufc fight night'
                 )
                THEN TRIM(CAST(f.event AS VARCHAR))
                ELSE ev.event_name
            END AS event_name,
            ev.location AS location,

            CONCAT('ufc_', f.fighter_id) AS fighter_id,
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(ff.first_name, '') ||
                        CASE
                            WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                            ELSE ''
                        END
                    ),
                    ''
                ),
                CONCAT('ufc_', f.fighter_id)
            ) AS fighter_name_plain,
            NULLIF(TRIM(ff.nickname), '') AS fighter_nickname,
            COALESCE(
                NULLIF(
                    CASE
                        WHEN NULLIF(TRIM(ff.nickname), '') IS NOT NULL
                        THEN
                            NULLIF(
                                TRIM(
                                    COALESCE(ff.first_name, '') ||
                                    CASE
                                        WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            ) || ' (' || TRIM(ff.nickname) || ')'
                        ELSE
                            NULLIF(
                                TRIM(
                                    COALESCE(ff.first_name, '') ||
                                    CASE
                                        WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            )
                    END,
                    ''
                ),
                COALESCE(
                    NULLIF(
                        TRIM(
                            COALESCE(ff.first_name, '') ||
                            CASE
                                WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                                ELSE ''
                            END
                        ),
                        ''
                    ),
                    CONCAT('ufc_', f.fighter_id)
                )
            ) AS fighter_name_display,
            -- Make fighter_name nickname-aware by default to avoid same-name collisions.
            COALESCE(
                NULLIF(
                    CASE
                        WHEN NULLIF(TRIM(ff.nickname), '') IS NOT NULL
                        THEN
                            NULLIF(
                                TRIM(
                                    COALESCE(ff.first_name, '') ||
                                    CASE
                                        WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            ) || ' (' || TRIM(ff.nickname) || ')'
                        ELSE
                            NULLIF(
                                TRIM(
                                    COALESCE(ff.first_name, '') ||
                                    CASE
                                        WHEN ff.last_name IS NOT NULL AND TRIM(ff.last_name) <> '' THEN ' ' || ff.last_name
                                        ELSE ''
                                    END
                                ),
                                ''
                            )
                    END,
                    ''
                ),
                CONCAT('ufc_', f.fighter_id)
            ) AS fighter_name,
            lower(
                regexp_replace(
                    TRIM(
                        COALESCE(ff.first_name, '') || ' ' ||
                        COALESCE(ff.last_name, '') || ' ' ||
                        COALESCE(NULLIF(TRIM(ff.nickname), ''), '')
                    ),
                    '[^a-z0-9]+',
                    '_'
                )
            ) AS fighter_name_key,
            ff.detail_url AS fighter_url,

            CASE
                WHEN regexp_extract(f.opponent_url, 'fighter-details/([0-9a-fA-F-]+)', 1) IS NOT NULL
                THEN CONCAT('ufc_', regexp_extract(f.opponent_url, 'fighter-details/([0-9a-fA-F-]+)', 1))
                ELSE NULL
            END AS opponent_id,
            COALESCE(
                TRIM(
                    COALESCE(ofi.first_name, '') ||
                    CASE
                        WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                        ELSE ''
                    END
                ),
                NULLIF(f.opponent, '')
            ) AS opponent_name_plain,
            NULLIF(TRIM(ofi.nickname), '') AS opponent_nickname,
            COALESCE(NULLIF(CASE
                WHEN NULLIF(TRIM(ofi.nickname), '') IS NOT NULL
                 AND NULLIF(
                    TRIM(
                        COALESCE(ofi.first_name, '') ||
                        CASE
                            WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                            ELSE ''
                        END
                    ),
                    ''
                 ) IS NOT NULL
                THEN
                    TRIM(
                        COALESCE(ofi.first_name, '') ||
                        CASE
                            WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                            ELSE ''
                        END
                    ) || ' (' || TRIM(ofi.nickname) || ')'
                ELSE
                    COALESCE(
                        TRIM(
                            COALESCE(ofi.first_name, '') ||
                            CASE
                                WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                                ELSE ''
                            END
                        ),
                        NULLIF(f.opponent, '')
                    )
            END, ''), COALESCE(NULLIF(f.opponent, ''), 'unknown_opponent')) AS opponent_name_display,
            -- Make opponent_name nickname-aware by default to avoid same-name collisions.
            COALESCE(NULLIF(CASE
                WHEN NULLIF(TRIM(ofi.nickname), '') IS NOT NULL
                 AND NULLIF(
                    TRIM(
                        COALESCE(ofi.first_name, '') ||
                        CASE
                            WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                            ELSE ''
                        END
                    ),
                    ''
                 ) IS NOT NULL
                THEN
                    TRIM(
                        COALESCE(ofi.first_name, '') ||
                        CASE
                            WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                            ELSE ''
                        END
                    ) || ' (' || TRIM(ofi.nickname) || ')'
                ELSE
                    COALESCE(
                        TRIM(
                            COALESCE(ofi.first_name, '') ||
                            CASE
                                WHEN ofi.last_name IS NOT NULL AND TRIM(ofi.last_name) <> '' THEN ' ' || ofi.last_name
                                ELSE ''
                            END
                        ),
                        NULLIF(f.opponent, '')
                    )
            END, ''), COALESCE(NULLIF(f.opponent, ''), 'unknown_opponent')) AS opponent_name,
            lower(
                regexp_replace(
                    TRIM(
                        COALESCE(ofi.first_name, '') || ' ' ||
                        COALESCE(ofi.last_name, '') || ' ' ||
                        COALESCE(NULLIF(TRIM(ofi.nickname), ''), '')
                    ),
                    '[^a-z0-9]+',
                    '_'
                )
            ) AS opponent_name_key,
            COALESCE(ofi.detail_url, NULLIF(f.opponent_url, '')) AS opponent_url,

            CASE
                WHEN lower(coalesce(ev.status, '')) = 'upcoming'
                  OR CAST(
                        COALESCE(
                            ev.event_date,
                            TRY_CAST(bev.event_date AS TIMESTAMP),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%B %d, %Y'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%Y-%m-%d'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%d/%m/%Y'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%m/%d/%Y')
                        ) AS DATE
                    ) > CURRENT_DATE
                THEN NULL
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'win' THEN 'win'
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'loss' THEN 'loss'
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'draw' THEN 'draw'
                ELSE 'nc'
            END AS result,

            NULLIF(TRIM(f.method), '') AS method_raw,
            f.round,
            f.time,
            NULLIF(f.weight_class, '') AS weight_class,
            COALESCE(f.is_title_fight, FALSE) AS is_title_fight,
            CASE
                WHEN lower(coalesce(ev.status, '')) = 'upcoming'
                  OR CAST(
                        COALESCE(
                            ev.event_date,
                            TRY_CAST(bev.event_date AS TIMESTAMP),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%B %d, %Y'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%Y-%m-%d'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%d/%m/%Y'),
                            TRY_STRPTIME(CAST(bev.event_date AS VARCHAR), '%m/%d/%Y')
                        ) AS DATE
                    ) > CURRENT_DATE
                THEN NULL
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'win' THEN 'win'
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'loss' THEN 'loss'
                WHEN LOWER(COALESCE(f.result, 'nc')) = 'draw' THEN 'draw'
                ELSE 'nc'
            END AS winner,

            f.kd_for,
            f.kd_against,
            f.str_for,
            f.str_against,
            f.td_for,
            f.td_against,
            f.sub_for,
            f.sub_against,

            COALESCE(NULLIF(f.bonus_tags, ''), '') AS bonus_tags,
            NULL::TIMESTAMP AS scraped_at,

            NULL::TEXT AS corner,
            NULL::TEXT AS title_name,
            NULL::TEXT AS weight_label,
            NULL::DOUBLE AS weight_lbs

        FROM bronze.ufc_fights f
        LEFT JOIN ufc_events ev
            ON ev.event_url_key = REGEXP_REPLACE(LOWER(TRIM(CAST(f.event_url AS VARCHAR))), '/+$', '')
        LEFT JOIN bronze.ufc_events bev
            ON REGEXP_REPLACE(LOWER(TRIM(CAST(bev.event_url AS VARCHAR))), '/+$', '')
               = REGEXP_REPLACE(LOWER(TRIM(CAST(f.event_url AS VARCHAR))), '/+$', '')
        LEFT JOIN bronze.ufc_fighters ff
            ON ff.fighter_id = f.fighter_id
        LEFT JOIN bronze.ufc_fighters ofi
            ON ofi.detail_url = f.opponent_url
        )

        SELECT
            fight_id,
            organization,
            event_id,
            event_date,
            event_status,
            event_url,
            event_name,
            location,
            fighter_id,
            fighter_name,
            fighter_name_plain,
            fighter_nickname,
            fighter_name_display,
            fighter_name_key,
            fighter_url,
            opponent_id,
            opponent_name,
            opponent_name_plain,
            opponent_nickname,
            opponent_name_display,
            opponent_name_key,
            opponent_url,
            result,
            method_raw,
            clean_method(method_raw) AS method,
            method_norm(method_raw) AS method_norm,
            method_class(method_raw) AS method_class,
            round,
            time,
            weight_class,
            is_title_fight,
            winner,
            kd_for,
            kd_against,
            str_for,
            str_against,
            td_for,
            td_against,
            sub_for,
            sub_against,
            bonus_tags,
            scraped_at,
            method_category(method_raw) AS method_category,
            CASE WHEN clean_method(method_raw) IS NULL THEN TRUE ELSE FALSE END AS method_is_missing,
            corner,
            title_name,
            weight_label,
            weight_lbs
        FROM base_fights;
        """
        self.conn.execute(query)
        count = self.conn.execute("SELECT COUNT(*) FROM silver.fights").fetchone()[0]
        logger.info(f"Created silver.fights with {count} records")

    def create_fighter_stats_table(self) -> None:
        """Unified fighter-level stats (UFC only)."""
        logger.info("Creating silver.fighter_stats table...")

        has_ufc = self.table_exists("bronze", "ufc_fighters_stats")

        if not has_ufc:
            query = """
            DROP TABLE IF EXISTS silver.fighter_stats;
            CREATE TABLE silver.fighter_stats AS
            SELECT
                NULL::TEXT AS fighter_id,
                NULL::TEXT AS organization,
                NULL::TEXT AS record,
                NULL::DOUBLE AS slpm,
                NULL::DOUBLE AS str_acc,
                NULL::DOUBLE AS sapm,
                NULL::DOUBLE AS str_def,
                NULL::DOUBLE AS td_avg,
                NULL::DOUBLE AS td_acc,
                NULL::DOUBLE AS td_def,
                NULL::DOUBLE AS sub_avg,
                NULL::INTEGER AS wins,
                NULL::INTEGER AS losses,
                NULL::INTEGER AS draws,
                NULL::DOUBLE AS finish_rate,
                NULL::INTEGER AS total_bouts,
                NULL::TEXT AS fighter_status,
                NULL::TIMESTAMP AS scraped_at
            WHERE FALSE;
            """
            self.conn.execute(query)
            logger.info("Created silver.fighter_stats (empty; missing bronze inputs)")
            return

        ufc_select = """
            SELECT
                CONCAT('ufc_', s.fighter_id) AS fighter_id,
                'UFC' AS organization,
                NULLIF(TRIM(s.record), '') AS record,
                s.slpm,
                s.str_acc,
                s.sapm,
                s.str_def,
                s.td_avg,
                s.td_acc,
                s.td_def,
                s.sub_avg,
                TRY_CAST(f.wins AS INTEGER) AS wins,
                TRY_CAST(f.losses AS INTEGER) AS losses,
                TRY_CAST(f.draws AS INTEGER) AS draws,
                NULL::DOUBLE AS finish_rate,
                NULL::INTEGER AS total_bouts,
                s.scraped_at AS scraped_at
            FROM bronze.ufc_fighters_stats s
            LEFT JOIN bronze.ufc_fighters f
                ON f.fighter_id = s.fighter_id
        """

        union_parts = [ufc_select]

        if not self.table_exists("silver", "fights"):
            logger.warning("silver.fights missing; creating it now (required for fighter-level aggregates).")
            self.create_fights_table()

        # Enrich the base stats with win-method aggregates from silver.fights.
        # Bayesian shrinkage uses a global prior mean (across all orgs) and a fixed prior strength.
        query = """
        DROP TABLE IF EXISTS silver.fighter_stats;
        CREATE TABLE silver.fighter_stats AS
        WITH
        base AS (
        """ + "\nUNION ALL BY NAME\n".join(union_parts) + """
        ),
        fighter_dim AS (
            SELECT
                fighter_id,
                TRIM(
                    COALESCE(first_name, '') ||
                    CASE
                        WHEN last_name IS NOT NULL AND TRIM(last_name) <> '' THEN ' ' || last_name
                        ELSE ''
                    END
                ) AS fighter_name,
                fighter_status
            FROM silver.fighters
        ),
        fight_names AS (
            SELECT
                fighter_id,
                MAX(NULLIF(TRIM(fighter_name), '')) AS fighter_name
            FROM silver.fights
            GROUP BY fighter_id
        ),
        name_pick AS (
            SELECT
                b.fighter_id,
                COALESCE(fd.fighter_name, fn.fighter_name) AS fighter_name,
                fd.fighter_status AS fighter_status
            FROM base b
            LEFT JOIN fighter_dim fd
                ON fd.fighter_id = b.fighter_id
            LEFT JOIN fight_names fn
                ON fn.fighter_id = b.fighter_id
        ),
        fight_aggs AS (
            SELECT
                fighter_id,

                COUNT(*) FILTER (WHERE result = 'win') AS wins_count,
                COUNT(*) FILTER (
                    WHERE result = 'win'
                      AND COALESCE(method_is_missing, FALSE) = FALSE
                      AND method_class IS NOT NULL
                ) AS wins_method_known_count,

                COUNT(*) FILTER (WHERE result = 'win' AND method_class = 'KO_TKO') AS ko_wins_count,
                COUNT(*) FILTER (WHERE result = 'win' AND method_class = 'SUB') AS sub_wins_count,
                COUNT(*) FILTER (WHERE result = 'win' AND method_class = 'DECISION') AS decision_wins_count,
                COUNT(*) FILTER (WHERE result = 'win' AND method_class = 'DQ') AS dq_wins_count,

                COUNT(*) FILTER (WHERE result = 'win' AND method_category = 'KO_TKO') AS wins_by_method_category_ko_tko,
                COUNT(*) FILTER (WHERE result = 'win' AND method_category = 'SUB') AS wins_by_method_category_sub,
                COUNT(*) FILTER (WHERE result = 'win' AND method_category = 'DEC') AS wins_by_method_category_dec,
                COUNT(*) FILTER (WHERE result = 'win' AND method_category = 'NC_DQ') AS wins_by_method_category_nc_dq

            FROM silver.fights
            GROUP BY fighter_id
        ),
        params AS (
            SELECT 10.0::DOUBLE AS prior_strength
        ),
        global_rates AS (
            SELECT
                CASE
                    WHEN SUM(CASE
                        WHEN result = 'win'
                         AND COALESCE(method_is_missing, FALSE) = FALSE
                         AND method_class IS NOT NULL
                        THEN 1 ELSE 0 END) = 0
                    THEN 0.0
                    ELSE
                        SUM(CASE WHEN result = 'win' AND method_class = 'KO_TKO' THEN 1 ELSE 0 END)::DOUBLE /
                        SUM(CASE
                            WHEN result = 'win'
                             AND COALESCE(method_is_missing, FALSE) = FALSE
                             AND method_class IS NOT NULL
                            THEN 1 ELSE 0 END)
                END AS global_ko_rate,

                CASE
                    WHEN SUM(CASE
                        WHEN result = 'win'
                         AND COALESCE(method_is_missing, FALSE) = FALSE
                         AND method_class IS NOT NULL
                        THEN 1 ELSE 0 END) = 0
                    THEN 0.0
                    ELSE
                        SUM(CASE WHEN result = 'win' AND method_class = 'SUB' THEN 1 ELSE 0 END)::DOUBLE /
                        SUM(CASE
                            WHEN result = 'win'
                             AND COALESCE(method_is_missing, FALSE) = FALSE
                             AND method_class IS NOT NULL
                            THEN 1 ELSE 0 END)
                END AS global_sub_rate,

                CASE
                    WHEN SUM(CASE
                        WHEN result = 'win'
                         AND COALESCE(method_is_missing, FALSE) = FALSE
                         AND method_class IS NOT NULL
                        THEN 1 ELSE 0 END) = 0
                    THEN 0.0
                    ELSE
                        SUM(CASE WHEN result = 'win' AND method_class IN ('KO_TKO', 'SUB') THEN 1 ELSE 0 END)::DOUBLE /
                        SUM(CASE
                            WHEN result = 'win'
                             AND COALESCE(method_is_missing, FALSE) = FALSE
                             AND method_class IS NOT NULL
                            THEN 1 ELSE 0 END)
                END AS global_finish_rate
            FROM silver.fights
        )

        SELECT
            b.*,

            np.fighter_name,
            np.fighter_status,

            a.wins_count,
            a.wins_method_known_count,

            a.ko_wins_count,
            a.sub_wins_count,
            (COALESCE(a.ko_wins_count, 0) + COALESCE(a.sub_wins_count, 0))::INTEGER AS finish_wins_count,
            a.decision_wins_count,
            a.dq_wins_count,

            a.wins_by_method_category_ko_tko,
            a.wins_by_method_category_sub,
            a.wins_by_method_category_dec,
            a.wins_by_method_category_nc_dq,

            CASE
                WHEN COALESCE(a.wins_method_known_count, 0) = 0 THEN NULL
                ELSE COALESCE(a.ko_wins_count, 0)::DOUBLE / a.wins_method_known_count
            END AS ko_rate_win_raw,
            CASE
                WHEN COALESCE(a.wins_method_known_count, 0) = 0 THEN NULL
                ELSE COALESCE(a.sub_wins_count, 0)::DOUBLE / a.wins_method_known_count
            END AS sub_rate_win_raw,
            CASE
                WHEN COALESCE(a.wins_method_known_count, 0) = 0 THEN NULL
                ELSE (COALESCE(a.ko_wins_count, 0) + COALESCE(a.sub_wins_count, 0))::DOUBLE / a.wins_method_known_count
            END AS finish_rate_win_raw,

            -- Bayesian shrinkage (Beta-Binomial posterior mean)
            (
                (1.0 + p.prior_strength * g.global_ko_rate + COALESCE(a.ko_wins_count, 0)) /
                (2.0 + p.prior_strength + COALESCE(a.wins_method_known_count, 0))
            ) AS ko_rate_win_shrunk,
            (
                (1.0 + p.prior_strength * g.global_sub_rate + COALESCE(a.sub_wins_count, 0)) /
                (2.0 + p.prior_strength + COALESCE(a.wins_method_known_count, 0))
            ) AS sub_rate_win_shrunk,
            (
                (1.0 + p.prior_strength * g.global_finish_rate + (COALESCE(a.ko_wins_count, 0) + COALESCE(a.sub_wins_count, 0))) /
                (2.0 + p.prior_strength + COALESCE(a.wins_method_known_count, 0))
            ) AS finish_rate_win_shrunk

        FROM base b
        LEFT JOIN name_pick np
            ON np.fighter_id = b.fighter_id
        LEFT JOIN fight_aggs a
            ON a.fighter_id = b.fighter_id
        CROSS JOIN params p
        CROSS JOIN global_rates g
        ;
        """

        self.conn.execute(query)
        count = self.conn.execute("SELECT COUNT(*) FROM silver.fighter_stats").fetchone()[0]
        logger.info(f"Created silver.fighter_stats with {count} records")

    # -----------------------
    # Runner
    # -----------------------
    def run_pipeline(self, steps: List[str]) -> None:
        """
        steps: list like ["fights"] or ["events","fights"] or ["all"]
        """
        step_order = ["events", "fighters", "fights", "fighter_stats"]

        cleaned: List[str] = []
        for s in steps:
            s = (s or "").strip().lower()
            if s:
                cleaned.append(s)
        if not cleaned:
            cleaned = ["all"]

        if "all" in cleaned:
            run_steps = step_order
        else:
            unknown = sorted(set(cleaned) - set(step_order))
            if unknown:
                raise ValueError(f"Unknown steps: {unknown}. Valid: {step_order} or 'all'")
            run_steps = [s for s in step_order if s in cleaned]

        logger.info(f"Running Silver ETL steps: {run_steps}")
        start_time = datetime.now()

        for s in run_steps:
            if s == "events":
                self.create_events_table()
            elif s == "fighters":
                self.create_fighters_table()
            elif s == "fights":
                self.create_fights_table()
            elif s == "fighter_stats":
                self.create_fighter_stats_table()

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Silver ETL completed in {elapsed:.2f} seconds")

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")


def resolve_duckdb_path(args: argparse.Namespace) -> str:
    """
    Resolve DuckDB path from (highest priority first):
      1) --duckdb-path
      2) env var based on --target:
         - prod -> DUCK_WH_DB
         - dev  -> DUCK_DEV_DB
    """
    # ...existing code...

    if getattr(args, "duckdb_path", None):
        return args.duckdb_path

    target = (getattr(args, "target", "dev") or "dev").lower().strip()
    if target in ("prod", "production"):
        env_key = "DUCK_WH_DB"
    elif target in ("dev", "development"):
        env_key = "DUCK_DEV_DB"
    else:
        raise RuntimeError(f"Unknown --target '{args.target}'. Use 'prod' or 'dev'.")

    db_path = os.environ.get(env_key)
    if not db_path:
        raise RuntimeError(
            f"{env_key} not set for --target {target}; "
            f"export {env_key} or pass --duckdb-path"
        )

    return db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Silver Layer ETL for MMA DuckDB")
    parser.add_argument("--duckdb-path", help="Path to the DuckDB file containing the bronze schema")
    parser.add_argument(
        "--target",
        choices=["dev", "prod"],
        default="dev",
        help="Convenience target to pick DUCK_DEV_DB or DUCK_WH_DB",
    )
    parser.add_argument(
        "--steps",
        default="all",
        help="Comma-separated: events,fighters,fights,fighter_stats or 'all'. Example: --steps fights",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = resolve_duckdb_path(args)
    logger.info(f"Using DuckDB at {db_path}")

    steps = [s.strip() for s in (args.steps or "all").split(",")]

    etl = SilverLayerETL(db_path)
    try:
        etl.run_pipeline(steps)
        print("\\n=== Silver Layer Summary ===")
        for table in ("events", "fighters", "fights", "fighter_stats"):
            if etl.table_exists("silver", table):
                count = etl.conn.execute(f"SELECT COUNT(*) FROM silver.{table}").fetchone()[0]
                print(f"silver.{table}: {count:,} records")
            else:
                print(f"silver.{table}: (not created)")
    finally:
        etl.close()


if __name__ == "__main__":
    main()
