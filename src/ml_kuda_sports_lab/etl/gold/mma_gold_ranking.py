#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Gold Layer: MMA ranking points (UFC only).

Creates:
- gold.mma_rankings          (latest snapshot as-of date)
- gold.mma_ranking_fight_log (per-bout points deltas used to build snapshot)

Design goals (based on provided Fight Minds description, adapted to this repo):
- Separate by organization, but use a single unified gold table.
- Time-decay (inactivity erosion) is applied to points.
- Method multipliers reward finishes more than decisions.
- UFC bonuses are derived from silver.fights.bonus_tags (UFC only).

This module intentionally avoids pandas/numpy so it can run in the current
Docker ETL image (requirements.etl.txt does not include pandas).
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import duckdb


@dataclass
class FighterState:
    points: float
    last_fight_date: Optional[date]
    win_streak: int
    loss_streak: int
    fights: int
    wins: int
    losses: int
    draws: int
    ncs: int
    has_won_title: bool
    title_defenses: int
    name: str


@dataclass
class OrgTotals:
    fights: int
    wins: int
    losses: int
    draws: int
    ncs: int


def resolve_duckdb_path(args: argparse.Namespace) -> str:
    if args.duckdb_path:
        return str(Path(args.duckdb_path).expanduser())
    target_env = "DUCK_WH_DB" if args.target == "prod" else "DUCK_DEV_DB"
    env_path = os.environ.get(target_env)
    if not env_path:
        raise RuntimeError(f"{target_env} not set; please export it or pass --duckdb-path")
    return str(Path(env_path).expanduser())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gold MMA ranking builder (UFC only)")
    p.add_argument("--duckdb-path", help="Path to the DuckDB file")
    p.add_argument("--target", choices=["dev", "prod"], default="dev")
    p.add_argument("--rebuild", action="store_true", help="Drop and recreate gold ranking tables")
    p.add_argument(
        "--as-of-date",
        default=None,
        help="Date for final ranking snapshot (YYYY-MM-DD). Default: today.",
    )
    return p.parse_args()


def _parse_as_of_date(s: Optional[str]) -> date:
    if not s:
        return date.today()
    return datetime.strptime(s, "%Y-%m-%d").date()


def _coerce_date(d: object | None) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        s = d.strip()
        if not s:
            return None
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def inactivity_erosion(last_date: object | None, current_date: object | None) -> float:
    """Inactivity erosion coefficient.

    Based on the provided reference code's sigmoid-style decay after ~24 months.
    Returns 1.0 for <= 730 days since last fight.
    """

    last_d = _coerce_date(last_date)
    cur_d = _coerce_date(current_date) or date.today()

    if last_d is None:
        return 1.0

    t_days = (cur_d - last_d).days
    if t_days <= 730:
        return 1.0

    decay_rate = 4200.0
    midpoint = 400.0

    # Same shape as the reference implementation.
    return (1.0 + math.exp(-midpoint / decay_rate)) / (1.0 + math.exp((t_days - 730.0 - midpoint) / decay_rate))


def method_multiplier(method_class: Optional[str], method_norm: Optional[str]) -> float:
    """Reward finishes more than decisions.

    The user explicitly called out the original weights as unintuitive; we bias
    KO/TKO and SUB above decisions.
    """

    mc = (method_class or "").upper().strip()
    mn = (method_norm or "").upper().strip()

    if mc == "KO_TKO":
        return 1.65
    if mc == "SUB":
        return 1.60

    # decisions
    if mn == "UD":
        return 1.10
    if mn in ("SD", "MD"):
        return 1.05
    if mc == "DECISION":
        return 1.08

    if mc == "DQ":
        return 1.00

    # NC / unknown
    return 1.00


def ufc_bonus_multiplier(organization: str, bonus_tags: Optional[str]) -> float:
    if organization != "UFC":
        return 1.0
    tags = (bonus_tags or "").upper()
    mult = 1.0

    # Conservative parsing; tags are free-form.
    if "PERF" in tags:
        mult *= 1.25
    if "FOTN" in tags or "FIGHT" in tags:
        mult *= 1.20

    return mult


def title_multiplier(is_title_fight: bool) -> float:
    return 1.40 if is_title_fight else 1.0


def streak_multiplier(streak: int) -> float:
    # 1 + (0.005 * n) as described (n = consecutive).
    return 1.0 + (0.005 * float(max(0, streak)))


def median(values: Iterable[float], default: float) -> float:
    xs = [v for v in values if v is not None]
    if not xs:
        return default
    xs.sort()
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return float(xs[mid])
    return float((xs[mid - 1] + xs[mid]) / 2.0)


def opponent_term(opponent_points: float, class_median_points: float, *, epsilon: float = 1e-9) -> float:
    """Opponent-strength contribution with dampening.

    Linear opponent points can lead to runaway compounding where active fighters
    explode upward in score. Use a geometric-mean style term so the contribution
    grows sub-linearly with opponent points while staying in the same units.
    """

    op = max(0.0, float(opponent_points))
    cm = max(epsilon, float(class_median_points))
    return math.sqrt(op * cm)


def load_bouts(conn: duckdb.DuckDBPyConnection) -> List[Tuple]:
    """Load one row per bout by pairing the two fighter perspectives."""

    sql = r"""
    WITH fights_norm AS (
        SELECT
            sf.organization,
            CAST(sf.event_date AS DATE) AS event_date,
            sf.event_url,
            sf.event_id,
            sf.weight_class,
            sf.is_title_fight,
            sf.bonus_tags,
            sf.method_norm,
            sf.method_class,
            sf.method_category,
            sf.fighter_id,
            COALESCE(
                fd.fighter_name_display,
                fd.fighter_name,
                sf.fighter_name_display,
                sf.fighter_name,
                sf.fighter_id
            ) AS fighter_name,
            sf.opponent_id,
            COALESCE(
                od.fighter_name_display,
                od.fighter_name,
                sf.opponent_name_display,
                sf.opponent_name,
                sf.opponent_id
            ) AS opponent_name,
            sf.result,
            sf.round,
            sf.time,
            sf.fight_id,
            md5(
                sf.organization || '|' ||
                coalesce(sf.event_url, '') || '|' ||
                least(sf.fighter_id, sf.opponent_id) || '|' ||
                greatest(sf.fighter_id, sf.opponent_id) || '|' ||
                coalesce(CAST(sf.round AS VARCHAR), '') || '|' ||
                coalesce(CAST(sf.time AS VARCHAR), '')
            ) AS bout_key
                FROM silver.fights sf
                LEFT JOIN silver.fighters fd
                  ON fd.organization = sf.organization
                 AND fd.fighter_id = sf.fighter_id
                LEFT JOIN silver.fighters od
                  ON od.organization = sf.organization
                 AND od.fighter_id = sf.opponent_id
                WHERE sf.event_date IS NOT NULL
                    AND sf.organization = 'UFC'
          AND sf.weight_class IS NOT NULL
          AND sf.fighter_id IS NOT NULL
          AND sf.opponent_id IS NOT NULL
    )
    SELECT
        a.organization,
        a.weight_class,
        a.event_date,
        a.event_url,
        a.event_id,
        a.is_title_fight,
        a.bonus_tags,
        a.method_norm,
        a.method_class,
        a.method_category,
        a.bout_key,

        a.fighter_id AS f1_id,
        a.fighter_name AS f1_name,
        a.result AS f1_result,

        b.fighter_id AS f2_id,
        b.fighter_name AS f2_name,
        b.result AS f2_result

    FROM fights_norm a
    JOIN fights_norm b
      ON a.bout_key = b.bout_key
     AND a.fighter_id < b.fighter_id

    ORDER BY a.organization, a.event_date, a.event_url, a.bout_key;
    """

    return conn.execute(sql).fetchall()


def ensure_gold_tables(conn: duckdb.DuckDBPyConnection, rebuild: bool) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
    if rebuild:
        conn.execute("DROP TABLE IF EXISTS gold.mma_rankings")
        conn.execute("DROP TABLE IF EXISTS gold.mma_overall_rankings")
        conn.execute("DROP TABLE IF EXISTS gold.mma_ranking_fight_log")

    conn.execute(
        r"""
        CREATE TABLE IF NOT EXISTS gold.mma_ranking_fight_log (
            computed_at TIMESTAMP,
            organization VARCHAR,
            weight_class VARCHAR,
            event_date DATE,
            event_url VARCHAR,
            event_id VARCHAR,
            bout_key VARCHAR,

            fighter1_id VARCHAR,
            fighter1_name VARCHAR,
            fighter2_id VARCHAR,
            fighter2_name VARCHAR,

            outcome VARCHAR,
            method_class VARCHAR,
            method_norm VARCHAR,
            is_title_fight BOOLEAN,
            bonus_tags VARCHAR,

            class_median_points DOUBLE,

            fighter1_points_before DOUBLE,
            fighter2_points_before DOUBLE,
            fighter1_points_after DOUBLE,
            fighter2_points_after DOUBLE,

            fighter1_delta DOUBLE,
            fighter2_delta DOUBLE
        );
        """
    )

    conn.execute(
        r"""
        CREATE TABLE IF NOT EXISTS gold.mma_rankings (
            computed_at TIMESTAMP,
            as_of_date DATE,
            organization VARCHAR,
            weight_class VARCHAR,
            fighter_id VARCHAR,
            fighter_name VARCHAR,
            points DOUBLE,
            last_fight_date DATE,
            fighter_status VARCHAR,

            -- Weight-class totals (within organization + weight_class)
            fights_count INTEGER,
            wins_count INTEGER,
            losses_count INTEGER,
            draws_count INTEGER,
            nc_count INTEGER,

            -- Org-wide totals (all weight classes within the same org)
            org_fights_count INTEGER,
            org_wins_count INTEGER,
            org_losses_count INTEGER,
            org_draws_count INTEGER,
            org_nc_count INTEGER,

            -- Deprecated alias columns kept for backwards compatibility
            class_fights_count INTEGER,
            class_wins_count INTEGER,
            class_losses_count INTEGER,
            class_draws_count INTEGER,
            class_nc_count INTEGER,

            title_defenses_count INTEGER,
            win_streak INTEGER,
            loss_streak INTEGER,
            has_won_title BOOLEAN,
            rank INTEGER
        );
        """
    )

    conn.execute(
        r"""
        CREATE TABLE IF NOT EXISTS gold.mma_overall_rankings (
            computed_at TIMESTAMP,
            as_of_date DATE,
            organization VARCHAR,
            fighter_id VARCHAR,
            fighter_name VARCHAR,
            points DOUBLE,
            last_fight_date DATE,
            fighter_status VARCHAR,

            fights_count INTEGER,
            wins_count INTEGER,
            losses_count INTEGER,
            draws_count INTEGER,
            nc_count INTEGER,

            title_defenses_count INTEGER,
            win_streak INTEGER,
            loss_streak INTEGER,
            has_won_title BOOLEAN,
            rank INTEGER
        );
        """
    )

    # Backwards/forwards compatibility: if table existed before, add new columns.
    try:
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS fighter_status VARCHAR")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS org_fights_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS org_wins_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS org_losses_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS org_draws_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS org_nc_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS class_fights_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS class_wins_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS class_losses_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS class_draws_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS class_nc_count INTEGER")
        conn.execute("ALTER TABLE gold.mma_rankings ADD COLUMN IF NOT EXISTS title_defenses_count INTEGER")
    except Exception:
        pass

    try:
        conn.execute("ALTER TABLE gold.mma_overall_rankings ADD COLUMN IF NOT EXISTS fighter_status VARCHAR")
        conn.execute("ALTER TABLE gold.mma_overall_rankings ADD COLUMN IF NOT EXISTS title_defenses_count INTEGER")
    except Exception:
        pass


def main() -> None:
    args = parse_args()
    as_of = _parse_as_of_date(args.as_of_date)
    db_path = resolve_duckdb_path(args)

    conn = duckdb.connect(db_path)
    conn.execute("PRAGMA enable_progress_bar = FALSE")

    ensure_gold_tables(conn, rebuild=args.rebuild)


    bouts = load_bouts(conn)
    # Store naive UTC to match DuckDB TIMESTAMP columns without using deprecated utcnow().
    computed_at = datetime.now(timezone.utc).replace(tzinfo=None)


    base_points = 0.01
    epsilon = 0.01

    # Shares / coefficients (kept small + interpretable)
    opponent_share = 0.50
    activity_win_share = 0.50
    activity_loss_share = 0.22
    opponent_loss_share = 0.18
    draw_class_share = 0.20
    draw_self_share = 0.05
    nc_class_share = 0.05

    # Snapshot-only boosts (do not affect per-bout deltas)
    fights_bonus_coeff = 0.60  # multiplies log1p(org_fights) (career matters a lot)
    defense_bonus_coeff = 0.35  # per title defense (defenses matter a lot)
    loss_penalty_coeff = 0.75  # strong penalty per org-wide loss (applied as losses^2)

    # Per-bout: a non-DQ loss should send a fighter "back in line".
    # Implemented as a cap on the loser's post-fight points.
    non_dq_loss_reset_share = 0.18

    # state key: (org, weight_class, fighter_id)
    states: Dict[Tuple[str, str, str], FighterState] = {}
    class_members: Dict[Tuple[str, str], List[str]] = {}

    # overall (org-wide) state key: (org, fighter_id)
    overall_states: Dict[Tuple[str, str], FighterState] = {}
    org_members: Dict[str, List[str]] = {}

    # org totals key: (org, fighter_id)
    org_totals: Dict[Tuple[str, str], OrgTotals] = {}

    def get_state(org: str, wc: str, fighter_id: str, fighter_name: str) -> FighterState:
        k = (org, wc, fighter_id)
        st = states.get(k)
        if st is None:
            st = FighterState(
                points=base_points,
                last_fight_date=None,
                win_streak=0,
                loss_streak=0,
                fights=0,
                wins=0,
                losses=0,
                draws=0,
                ncs=0,
                has_won_title=False,
                title_defenses=0,
                name=fighter_name or "",
            )
            states[k] = st
            class_members.setdefault((org, wc), []).append(fighter_id)
        elif fighter_name and (not st.name or len(fighter_name) > len(st.name)):
            # keep "best" name
            st.name = fighter_name
        return st
    

    def expiration_coeff(last_date: object | None, current_date: object | None) -> float:
        """Inactivity erosion coefficient."""
        last_d = _coerce_date(last_date)
        cur_d = _coerce_date(current_date) or date.today()

        if last_d is None:
            return 1.0

        t_days = (cur_d - last_d).days
        if t_days <= 730:
            return 1.0

        decay_rate = 4200.0
        midpoint = 400.0

        return (1.0 + math.exp(-midpoint / decay_rate)) / (
            1.0 + math.exp((t_days - 730.0 - midpoint) / decay_rate)
        )


    def get_overall_state(org: str, fighter_id: str, fighter_name: str) -> FighterState:
        k = (org, fighter_id)
        st = overall_states.get(k)
        if st is None:
            st = FighterState(
                points=base_points,
                last_fight_date=None,
                win_streak=0,
                loss_streak=0,
                fights=0,
                wins=0,
                losses=0,
                draws=0,
                ncs=0,
                has_won_title=False,
                title_defenses=0,
                name=fighter_name or "",
            )
            overall_states[k] = st
            org_members.setdefault(org, []).append(fighter_id)
        elif fighter_name and (not st.name or len(fighter_name) > len(st.name)):
            st.name = fighter_name
        return st

    def points_at(st: FighterState, on_date: date) -> float:
        last_d = _coerce_date(st.last_fight_date)
        on_d = _coerce_date(on_date) or date.today()
        if last_d is None:
            return st.points
        return st.points * expiration_coeff(last_d, on_d)

    def overall_points_at(st: FighterState, on_date: date) -> float:
        # Overall rankings are intended to be closer to an "all-time" view.
        # So we do not apply inactivity decay here.
        return st.points

    def class_median(org: str, wc: str, on_date: date) -> float:
        members = class_members.get((org, wc), [])
        vals: List[float] = []
        for fid in members:
            st = states.get((org, wc, fid))
            if st is not None:
                vals.append(points_at(st, on_date))
        return median(vals, default=0.10)

    def org_median(org: str, on_date: date) -> float:
        members = org_members.get(org, [])
        vals: List[float] = []
        for fid in members:
            st = overall_states.get((org, fid))
            if st is not None:
                vals.append(overall_points_at(st, on_date))
        return median(vals, default=0.10)

    def get_org_totals(org: str, fighter_id: str) -> OrgTotals:
        k = (org, fighter_id)
        t = org_totals.get(k)
        if t is None:
            t = OrgTotals(fights=0, wins=0, losses=0, draws=0, ncs=0)
            org_totals[k] = t
        return t

    fight_log_rows: List[Tuple] = []

    for (
        org,
        wc,
        ev_date,
        ev_url,
        ev_id,
        is_title_fight,
        bonus_tags,
        m_norm,
        m_class,
        _m_cat,
        bout_key,
        f1_id,
        f1_name,
        f1_result,
        f2_id,
        f2_name,
        f2_result,
    ) in bouts:
        if ev_date is None or wc is None:
            continue

        wc = str(wc)
        org = str(org)

        s1 = get_state(org, wc, str(f1_id), str(f1_name))
        s2 = get_state(org, wc, str(f2_id), str(f2_name))

        os1 = get_overall_state(org, str(f1_id), str(f1_name))
        os2 = get_overall_state(org, str(f2_id), str(f2_name))

        # Apply time-decay up to fight date (and materialize into state)
        p1_before = points_at(s1, ev_date)
        p2_before = points_at(s2, ev_date)
        s1.points = p1_before
        s2.points = p2_before
        s1.last_fight_date = ev_date
        s2.last_fight_date = ev_date

        c_med = class_median(org, wc, ev_date)

        # Overall (org-wide) time-decay + median (ignoring weight class)
        op1_before = overall_points_at(os1, ev_date)
        op2_before = overall_points_at(os2, ev_date)
        os1.last_fight_date = ev_date
        os2.last_fight_date = ev_date

        o_med = org_median(org, ev_date)

        method_mult = method_multiplier(m_class, m_norm)
        is_dq = (str(m_class).upper().strip() == "DQ") if m_class is not None else False
        bonus_mult = ufc_bonus_multiplier(org, bonus_tags)
        t_mult = title_multiplier(bool(is_title_fight))

        outcome = "nc"

        # Normalize result fields
        r1 = (str(f1_result) if f1_result is not None else "").lower().strip()
        r2 = (str(f2_result) if f2_result is not None else "").lower().strip()

        if r1 == "draw" or r2 == "draw":
            outcome = "draw"
            # streak reset
            s1.win_streak = 0
            s1.loss_streak = 0
            s2.win_streak = 0
            s2.loss_streak = 0

            gain1 = (c_med * draw_class_share) + (p1_before * draw_self_share)
            gain2 = (c_med * draw_class_share) + (p2_before * draw_self_share)

            p1_after = p1_before + gain1
            p2_after = p2_before + gain2

            s1.draws += 1
            s2.draws += 1

            # overall counters + points
            os1.win_streak = 0
            os1.loss_streak = 0
            os2.win_streak = 0
            os2.loss_streak = 0
            og1 = (o_med * draw_class_share) + (op1_before * draw_self_share)
            og2 = (o_med * draw_class_share) + (op2_before * draw_self_share)
            os1.points = float(op1_before + og1)
            os2.points = float(op2_before + og2)
            os1.draws += 1
            os2.draws += 1

            t1 = get_org_totals(org, str(f1_id))
            t2 = get_org_totals(org, str(f2_id))
            t1.fights += 1
            t2.fights += 1
            t1.draws += 1
            t2.draws += 1

        elif r1 == "win" and r2 == "loss":
            outcome = "f1_win" if not is_dq else "f1_win_dq"

            if is_dq:
                # DQ loss should not punish the loser in ranking.
                s1.win_streak = 0
                s1.loss_streak = 0
                s2.win_streak = 0
                s2.loss_streak = 0

                p1_after = p1_before + (c_med * 0.20)
                # DQ-loss treated like a mild NC for the loser
                p2_after = max(base_points, p2_before - (c_med * 0.03))

                s1.wins += 1
                s2.ncs += 1

                os1.win_streak = 0
                os1.loss_streak = 0
                os2.win_streak = 0
                os2.loss_streak = 0
                os1.points = float(op1_before + (o_med * 0.20))
                os2.points = float(max(base_points, op2_before - (o_med * 0.03)))
                os1.wins += 1
                os2.ncs += 1

                t1 = get_org_totals(org, str(f1_id))
                t2 = get_org_totals(org, str(f2_id))
                t1.fights += 1
                t2.fights += 1
                t1.wins += 1
                t2.ncs += 1

                if is_title_fight:
                    if s1.has_won_title:
                        s1.title_defenses += 1
                    s1.has_won_title = True
                    if os1.has_won_title:
                        os1.title_defenses += 1
                    os1.has_won_title = True

                # skip the normal win/loss path
                s1.fights += 1
                s2.fights += 1
                os1.fights += 1
                os2.fights += 1

                s1.points = float(p1_after)
                s2.points = float(p2_after)
                fight_log_rows.append(
                    (
                        computed_at,
                        org,
                        wc,
                        ev_date,
                        str(ev_url) if ev_url is not None else None,
                        str(ev_id) if ev_id is not None else None,
                        str(bout_key),
                        str(f1_id),
                        str(s1.name),
                        str(f2_id),
                        str(s2.name),
                        outcome,
                        str(m_class) if m_class is not None else None,
                        str(m_norm) if m_norm is not None else None,
                        bool(is_title_fight),
                        str(bonus_tags) if bonus_tags is not None else None,
                        float(c_med),
                        float(p1_before),
                        float(p2_before),
                        float(p1_after),
                        float(p2_after),
                        float(p1_after - p1_before),
                        float(p2_after - p2_before),
                    )
                )
                continue

            s1.win_streak += 1
            s1.loss_streak = 0
            s2.loss_streak += 1
            s2.win_streak = 0

            streak_w = streak_multiplier(s1.win_streak)
            streak_l = streak_multiplier(s2.loss_streak)

            champ_mult = 1.15 if s1.has_won_title else 1.0

            gain = (
                (opponent_term(p2_before, c_med) * opponent_share * method_mult)
                + (c_med * activity_win_share)
            ) * t_mult * bonus_mult * streak_w * champ_mult
            loss = (
                (c_med * activity_loss_share)
                + (opponent_term(p1_before, c_med) * opponent_loss_share)
            ) * method_mult * streak_l
            if is_title_fight:
                loss /= 1.30

            p1_after = p1_before + max(0.0, gain)
            p2_after = max(base_points, p2_before - max(0.0, loss))

            # Non-DQ loss: hard-reset loser back down the ranking.
            p2_after = min(p2_after, max(base_points, c_med * non_dq_loss_reset_share))

            if p1_after < p2_after:
                p1_after = p2_after + epsilon

            s1.wins += 1
            s2.losses += 1

            # overall
            os1.win_streak += 1
            os1.loss_streak = 0
            os2.loss_streak += 1
            os2.win_streak = 0

            ostreak_w = streak_multiplier(os1.win_streak)
            ostreak_l = streak_multiplier(os2.loss_streak)
            ochamp_mult = 1.15 if os1.has_won_title else 1.0

            ogain = (
                (opponent_term(op2_before, o_med) * opponent_share * method_mult)
                + (o_med * activity_win_share)
            ) * t_mult * bonus_mult * ostreak_w * ochamp_mult
            oloss = (
                (o_med * activity_loss_share)
                + (opponent_term(op1_before, o_med) * opponent_loss_share)
            ) * method_mult * ostreak_l
            if is_title_fight:
                oloss /= 1.30

            op1_after = op1_before + max(0.0, ogain)
            op2_after = max(base_points, op2_before - max(0.0, oloss))

            # Non-DQ loss: same reset logic in overall (org-wide) points.
            op2_after = min(op2_after, max(base_points, o_med * non_dq_loss_reset_share))
            if op1_after < op2_after:
                op1_after = op2_after + epsilon

            os1.points = float(op1_after)
            os2.points = float(op2_after)
            os1.wins += 1
            os2.losses += 1
            if is_title_fight:
                if os1.has_won_title:
                    os1.title_defenses += 1
                os1.has_won_title = True

            t1 = get_org_totals(org, str(f1_id))
            t2 = get_org_totals(org, str(f2_id))
            t1.fights += 1
            t2.fights += 1
            t1.wins += 1
            t2.losses += 1

            if is_title_fight:
                if s1.has_won_title:
                    s1.title_defenses += 1
                s1.has_won_title = True

        elif r2 == "win" and r1 == "loss":
            outcome = "f2_win" if not is_dq else "f2_win_dq"

            if is_dq:
                # DQ loss should not punish the loser in ranking.
                s1.win_streak = 0
                s1.loss_streak = 0
                s2.win_streak = 0
                s2.loss_streak = 0

                p2_after = p2_before + (c_med * 0.20)
                # DQ-loss treated like a mild NC for the loser
                p1_after = max(base_points, p1_before - (c_med * 0.03))

                s2.wins += 1
                s1.ncs += 1

                os1.win_streak = 0
                os1.loss_streak = 0
                os2.win_streak = 0
                os2.loss_streak = 0
                os2.points = float(op2_before + (o_med * 0.20))
                os1.points = float(max(base_points, op1_before - (o_med * 0.03)))
                os2.wins += 1
                os1.ncs += 1

                t1 = get_org_totals(org, str(f1_id))
                t2 = get_org_totals(org, str(f2_id))
                t1.fights += 1
                t2.fights += 1
                t1.ncs += 1
                t2.wins += 1

                if is_title_fight:
                    if s2.has_won_title:
                        s2.title_defenses += 1
                    s2.has_won_title = True
                    if os2.has_won_title:
                        os2.title_defenses += 1
                    os2.has_won_title = True

                # skip the normal win/loss path
                s1.fights += 1
                s2.fights += 1
                os1.fights += 1
                os2.fights += 1

                s1.points = float(p1_after)
                s2.points = float(p2_after)
                fight_log_rows.append(
                    (
                        computed_at,
                        org,
                        wc,
                        ev_date,
                        str(ev_url) if ev_url is not None else None,
                        str(ev_id) if ev_id is not None else None,
                        str(bout_key),
                        str(f1_id),
                        str(s1.name),
                        str(f2_id),
                        str(s2.name),
                        outcome,
                        str(m_class) if m_class is not None else None,
                        str(m_norm) if m_norm is not None else None,
                        bool(is_title_fight),
                        str(bonus_tags) if bonus_tags is not None else None,
                        float(c_med),
                        float(p1_before),
                        float(p2_before),
                        float(p1_after),
                        float(p2_after),
                        float(p1_after - p1_before),
                        float(p2_after - p2_before),
                    )
                )
                continue

            s2.win_streak += 1
            s2.loss_streak = 0
            s1.loss_streak += 1
            s1.win_streak = 0

            streak_w = streak_multiplier(s2.win_streak)
            streak_l = streak_multiplier(s1.loss_streak)

            champ_mult = 1.15 if s2.has_won_title else 1.0

            gain = (
                (opponent_term(p1_before, c_med) * opponent_share * method_mult)
                + (c_med * activity_win_share)
            ) * t_mult * bonus_mult * streak_w * champ_mult
            loss = (
                (c_med * activity_loss_share)
                + (opponent_term(p2_before, c_med) * opponent_loss_share)
            ) * method_mult * streak_l
            if is_title_fight:
                loss /= 1.30

            p2_after = p2_before + max(0.0, gain)
            p1_after = max(base_points, p1_before - max(0.0, loss))

            # Non-DQ loss: hard-reset loser back down the ranking.
            p1_after = min(p1_after, max(base_points, c_med * non_dq_loss_reset_share))

            if p2_after < p1_after:
                p2_after = p1_after + epsilon

            s2.wins += 1
            s1.losses += 1

            # overall
            os2.win_streak += 1
            os2.loss_streak = 0
            os1.loss_streak += 1
            os1.win_streak = 0

            ostreak_w = streak_multiplier(os2.win_streak)
            ostreak_l = streak_multiplier(os1.loss_streak)
            ochamp_mult = 1.15 if os2.has_won_title else 1.0

            ogain = (
                (opponent_term(op1_before, o_med) * opponent_share * method_mult)
                + (o_med * activity_win_share)
            ) * t_mult * bonus_mult * ostreak_w * ochamp_mult
            oloss = (
                (o_med * activity_loss_share)
                + (opponent_term(op2_before, o_med) * opponent_loss_share)
            ) * method_mult * ostreak_l
            if is_title_fight:
                oloss /= 1.30

            op2_after = op2_before + max(0.0, ogain)
            op1_after = max(base_points, op1_before - max(0.0, oloss))

            # Non-DQ loss: same reset logic in overall (org-wide) points.
            op1_after = min(op1_after, max(base_points, o_med * non_dq_loss_reset_share))
            if op2_after < op1_after:
                op2_after = op1_after + epsilon

            os1.points = float(op1_after)
            os2.points = float(op2_after)
            os2.wins += 1
            os1.losses += 1
            if is_title_fight:
                if os2.has_won_title:
                    os2.title_defenses += 1
                os2.has_won_title = True

            t1 = get_org_totals(org, str(f1_id))
            t2 = get_org_totals(org, str(f2_id))
            t1.fights += 1
            t2.fights += 1
            t1.losses += 1
            t2.wins += 1

            if is_title_fight:
                if s2.has_won_title:
                    s2.title_defenses += 1
                s2.has_won_title = True

        else:
            # no contest / unknown
            outcome = "nc"
            s1.win_streak = 0
            s1.loss_streak = 0
            s2.win_streak = 0
            s2.loss_streak = 0

            # NC should slightly take a fighter down
            loss1 = (c_med * nc_class_share)
            loss2 = (c_med * nc_class_share)
            p1_after = max(base_points, p1_before - loss1)
            p2_after = max(base_points, p2_before - loss2)

            s1.ncs += 1
            s2.ncs += 1

            # overall
            os1.win_streak = 0
            os1.loss_streak = 0
            os2.win_streak = 0
            os2.loss_streak = 0
            oloss1 = (o_med * nc_class_share)
            oloss2 = (o_med * nc_class_share)
            os1.points = float(max(base_points, op1_before - oloss1))
            os2.points = float(max(base_points, op2_before - oloss2))
            os1.ncs += 1
            os2.ncs += 1

            t1 = get_org_totals(org, str(f1_id))
            t2 = get_org_totals(org, str(f2_id))
            t1.fights += 1
            t2.fights += 1
            t1.ncs += 1
            t2.ncs += 1

        s1.fights += 1
        s2.fights += 1

        os1.fights += 1
        os2.fights += 1

        s1.points = float(p1_after)
        s2.points = float(p2_after)

        fight_log_rows.append(
            (
                computed_at,
                org,
                wc,
                ev_date,
                str(ev_url) if ev_url is not None else None,
                str(ev_id) if ev_id is not None else None,
                str(bout_key),
                str(f1_id),
                str(s1.name),
                str(f2_id),
                str(s2.name),
                outcome,
                str(m_class) if m_class is not None else None,
                str(m_norm) if m_norm is not None else None,
                bool(is_title_fight),
                str(bonus_tags) if bonus_tags is not None else None,
                float(c_med),
                float(p1_before),
                float(p2_before),
                float(p1_after),
                float(p2_after),
                float(p1_after - p1_before),
                float(p2_after - p2_before),  # ← ADD THIS LINE
            )
        )

    # Persist fight log (append by default)
    conn.executemany(
    """
    INSERT INTO gold.mma_ranking_fight_log (
        computed_at,
        organization,
        weight_class,
        event_date,
        event_url,
        event_id,
        bout_key,
        fighter1_id,
        fighter1_name,
        fighter2_id,
        fighter2_name,
        outcome,
        method_class,
        method_norm,
        is_title_fight,
        bonus_tags,
        class_median_points,
        fighter1_points_before,
        fighter2_points_before,
        fighter1_points_after,
        fighter2_points_after,
        fighter1_delta,
        fighter2_delta
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
    fight_log_rows,
)


    # Build final snapshot as-of date
    snapshot_rows: List[Tuple] = []
    overall_snapshot_rows: List[Tuple] = []

    def fighter_status_from_last_fight(last_fight_date: object | None) -> str:
        """Compute active/inactive from a yyyy-mm-dd date.

        Rule: inactive if last fight is more than 2 years ago.
        """
        last_d = _coerce_date(last_fight_date)
        if last_d is None:
            return "active"
        return "inactive" if (as_of - last_d).days > 730 else "active"

    # Compute ranks within (org, weight_class)
    grouped: Dict[Tuple[str, str], List[Tuple[str, FighterState, float]]] = {}
    for (org, wc, fid), st in states.items():
        pts = points_at(st, as_of)
        tot = get_org_totals(org, fid)
        boosted = float(pts)
        boosted *= 1.0 + (fights_bonus_coeff * math.log1p(max(0, tot.fights)))
        boosted *= 1.0 + (defense_bonus_coeff * float(max(0, st.title_defenses)))
        boosted *= 1.0 / (1.0 + (loss_penalty_coeff * (float(max(0, tot.losses)) ** 2)))
        grouped.setdefault((org, wc), []).append((fid, st, boosted))

    for (org, wc), entries in grouped.items():
        entries.sort(key=lambda t: t[2], reverse=True)
        for idx, (fid, st, pts) in enumerate(entries, 1):
            tot = get_org_totals(org, fid)
            snapshot_rows.append(
                (
                    computed_at,
                    as_of,
                    org,
                    wc,
                    fid,
                    st.name,
                    float(pts),
                    st.last_fight_date,
                    fighter_status_from_last_fight(st.last_fight_date),
                    int(st.fights),
                    int(st.wins),
                    int(st.losses),
                    int(st.draws),
                    int(st.ncs),
                    int(tot.fights),
                    int(tot.wins),
                    int(tot.losses),
                    int(tot.draws),
                    int(tot.ncs),
                    int(st.title_defenses),
                    int(st.fights),
                    int(st.wins),
                    int(st.losses),
                    int(st.draws),
                    int(st.ncs),
                    int(st.win_streak),
                    int(st.loss_streak),
                    bool(st.has_won_title),
                    int(idx),
                )
            )

    # Compute overall ranks within each organization (ignore weight class)
    overall_grouped: Dict[str, List[Tuple[str, FighterState, float]]] = {}
    for (org, fid), st in overall_states.items():
        pts = overall_points_at(st, as_of)
        tot = get_org_totals(org, fid)
        boosted = float(pts)
        boosted *= 1.0 + (fights_bonus_coeff * math.log1p(max(0, tot.fights)))
        boosted *= 1.0 + (defense_bonus_coeff * float(max(0, st.title_defenses)))
        boosted *= 1.0 / (1.0 + (loss_penalty_coeff * (float(max(0, tot.losses)) ** 2)))
        overall_grouped.setdefault(org, []).append((fid, st, boosted))

    for org, entries in overall_grouped.items():
        entries.sort(key=lambda t: t[2], reverse=True)
        for idx, (fid, st, pts) in enumerate(entries, 1):
            tot = get_org_totals(org, fid)
            overall_snapshot_rows.append(
                (
                    computed_at,
                    as_of,
                    org,
                    fid,
                    st.name,
                    float(pts),
                    st.last_fight_date,
                    fighter_status_from_last_fight(st.last_fight_date),
                    int(tot.fights),
                    int(tot.wins),
                    int(tot.losses),
                    int(tot.draws),
                    int(tot.ncs),
                    int(st.title_defenses),
                    int(st.win_streak),
                    int(st.loss_streak),
                    bool(st.has_won_title),
                    int(idx),
                )
            )

    if args.rebuild:
        conn.execute("DELETE FROM gold.mma_rankings")
        conn.execute("DELETE FROM gold.mma_overall_rankings")
    else:
        # keep only the latest snapshot for each run by clearing prior rows with same computed_at
        conn.execute("DELETE FROM gold.mma_rankings WHERE computed_at = ?", [computed_at])
        conn.execute("DELETE FROM gold.mma_overall_rankings WHERE computed_at = ?", [computed_at])

    if snapshot_rows:
        conn.executemany(
            """
            INSERT INTO gold.mma_rankings (
                computed_at,
                as_of_date,
                organization,
                weight_class,
                fighter_id,
                fighter_name,
                points,
                last_fight_date,
                fighter_status,
                fights_count,
                wins_count,
                losses_count,
                draws_count,
                nc_count,
                org_fights_count,
                org_wins_count,
                org_losses_count,
                org_draws_count,
                org_nc_count,
                title_defenses_count,
                class_fights_count,
                class_wins_count,
                class_losses_count,
                class_draws_count,
                class_nc_count,
                win_streak,
                loss_streak,
                has_won_title,
                rank
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            snapshot_rows,
        )

    if overall_snapshot_rows:
        conn.executemany(
            """
            INSERT INTO gold.mma_overall_rankings (
                computed_at,
                as_of_date,
                organization,
                fighter_id,
                fighter_name,
                points,
                last_fight_date,
                fighter_status,
                fights_count,
                wins_count,
                losses_count,
                draws_count,
                nc_count,
                title_defenses_count,
                win_streak,
                loss_streak,
                has_won_title,
                rank
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            overall_snapshot_rows,
        )


    conn.close()


if __name__ == "__main__":
    main()
