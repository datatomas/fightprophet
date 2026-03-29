#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gold Layer: Train + score UFC win/loss models from gold.prefight_features using CatBoost (+ optional LogReg).

This is intentionally lightweight and runnable inside the ETL Docker image.

Inputs
- gold.prefight_features (TABLE/VIEW) built by ml_kuda_sports_lab.etl.gold.mma_gold_features

Outputs
- Writes model artifacts into --out-dir
- Writes scored table/view back into DuckDB:
    --scored-table (TABLE)
    --scored-upcoming-view (VIEW)

Modeling notes
- Uses a time-based split (by event_date) to reduce leakage in evaluation.
- Keeps features numeric-only by default (no categorical encoding).
- CatBoost handles NaNs for numeric features (no imputer required).
- Optionally blends model probability with market implied probability when odds exist.

Tuned params
- Reads latest CatBoost hyperparameters from DuckDB table `gold.best_params` using:
    (target=args.target, model_name=args.catbest_model_name)
- Your tuner script should write `model_name='catboost'` by default.

Typical usage
  source bootstrap_scripts/envloader.sh && \
  python3 -m ml_kuda_sports_lab.etl.gold.mma_gold_train_catboost --target dev --model all --rebuild

Odds usage
- --odds-source duckdb: uses a column in the scoring view/table (auto-detected if omitted)
- --odds-source fightodds: fetches odds from https://api.fightodds.io/gql for upcoming fights only
"""

from __future__ import annotations
import time as _time
from html.parser import HTMLParser as _HTMLParser
import argparse
import difflib
import hashlib
import inspect
import json
import logging
import os
import re
import urllib.request
from urllib.parse import urlencode
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# -----------------------------
# DuckDB path / tuned params
# -----------------------------
def resolve_duckdb_path(args: argparse.Namespace) -> str:
    if args.duckdb_path:
        return str(Path(args.duckdb_path).expanduser())
    target_env = "DUCK_WH_DB" if args.target == "prod" else "DUCK_DEV_DB"
    env_path = os.environ.get(target_env)
    if not env_path:
        raise RuntimeError(f"{target_env} not set; please export it or pass --duckdb-path")
    return str(Path(env_path).expanduser())


def _load_latest_best_params(
    conn: duckdb.DuckDBPyConnection,
    *,
    target: str,
    model_name: str,
    params_table: str = "gold.best_params",
) -> Optional[dict]:
    """Load latest tuned hyperparameters (JSON) from DuckDB.

    Returns None if the table doesn't exist, is empty, or contains invalid JSON.
    """
    try:
        row = conn.execute(
            f"""
            SELECT params_json
            FROM {params_table}
            WHERE target = ? AND model_name = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [target, model_name],
        ).fetchone()
    except Exception as e:
        logger.info(f"No tuned params loaded from {params_table} (missing table or query failed): {e}")
        return None

    if not row or not row[0]:
        return None

    try:
        params = json.loads(row[0])
    except Exception as e:
        logger.warning(f"Failed to parse params_json from {params_table}: {e}")
        return None

    if not isinstance(params, dict):
        logger.warning(f"Ignoring params_json from {params_table}: expected dict, got {type(params)}")
        return None

    return params


# -----------------------------
# CLI
# -----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train + score UFC win/loss models (CatBoost + optional LogReg)")
    p.add_argument("--duckdb-path", help="Path to the DuckDB file")
    p.add_argument("--target", choices=["dev", "prod"], default="dev")
    p.add_argument("--seed", type=int, default=42, help="Random seed for model training")

    p.add_argument("--view", default="gold.prefight_features", help="Source table/view name for scoring")
    p.add_argument("--label-col", default="y_win", help="Binary label column (0/1)")
    p.add_argument("--time-col", default="event_date", help="Time column for time-based split")
    p.add_argument("--test-frac", type=float, default=0.2, help="Fraction of most-recent rows for test")
    p.add_argument(
        "--min-event-date",
        default=None,
        help="Optional YYYY-MM-DD; drop training rows before this date (scoring still uses all rows)",
    )
    p.add_argument(
        "--min-feature-coverage",
        type=float,
        default=0.0,
        help=(
            "Drop training rows whose non-null feature coverage is below this fraction (0-1). "
            "Useful when early fights lack stats. Scoring still uses all rows."
        ),
    )

    p.add_argument("--rebuild", action="store_true", help="Force retraining and overwrite saved models")

    p.add_argument("--model", choices=["logreg", "cat", "all"], default="all", help="Which model(s) to train")
    p.add_argument(
        "--out-dir",
        default=str(Path.home() / "Documents" / "uppercutanalytics" / "models" / "ufc"),
        help="Output folder for artifacts (mount a volume here in Docker)",
    )

    p.add_argument(
        "--cat-device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="CatBoost device; auto defaults to CPU unless GPU is available",
    )

    # Tuned params
    p.add_argument("--best-params-table", default="gold.best_params_catboost", help="DuckDB table containing tuned params")
    p.add_argument(
        "--catbest-model-name",
        default="catboost",
        help="model_name key to read from gold.best_params_catboost (should match tuner script)",
    )

    # Incremental scoring options
    p.add_argument(
        "--only-upcoming",
        action="store_true",
        help=(
            "Refresh ONLY upcoming fights inside the scored table (non-destructive). "
            "Historical fights remain untouched. Also creates/updates the upcoming-only VIEW."
        ),
    )

    p.add_argument(
        "--scored-upcoming-view",
        "--scored-upcoming-table",
        dest="scored_upcoming_view",
        default="gold.prefight_features_scored_upcoming",
        help=(
            "Create/replace this VIEW with upcoming-only rows (filters --scored-table by is_upcoming). "
            "(--scored-upcoming-table is a deprecated alias; it now creates a VIEW.)"
        ),
    )

    # Odds / betting
    p.add_argument(
        "--odds-source",
        choices=["duckdb", "fightodds", "bestfightodds"],
        default="duckdb",
        help=(
            "Where to get odds used for implied probability / betting_edge. "
            "duckdb: read from a column in the modeling view/table. "
            "fightodds: fetch odds from https://api.fightodds.io/gql and match by event_date + fighter names."
        ),
    )
    p.add_argument(
        "--odds-col",
        default=None,
        help=(
            "Column containing the fighter's odds (American like -150/+120, decimal like 1.75, or implied prob). "
            "If omitted, the script tries to auto-detect a column containing 'moneyline' or 'odds'."
        ),
    )
    p.add_argument(
        "--odds-format",
        choices=["auto", "american", "decimal", "implied"],
        default="american",
        help="How to interpret --odds-col. 'auto' guesses per-value.",
    )

    p.add_argument(
        "--odds-fallback",
        choices=["none", "oddsapi"],
        default="oddsapi",
        help="Fallback provider when --odds-source fightodds fails.",
    )
    p.add_argument(
        "--the-odds-api-key",
        default=os.environ.get("THE_ODDS_API_KEY", ""),
        help="API key for The Odds API fallback (or set THE_ODDS_API_KEY env var).",
    )
    p.add_argument(
        "--the-odds-api-sport-key",
        default="mma_mixed_martial_arts",
        help="The Odds API sport key used for fallback odds fetch.",
    )
    p.add_argument(
        "--the-odds-api-regions",
        default="us",
        help="Comma-separated The Odds API regions (e.g., us,uk,eu).",
    )

    p.add_argument(
        "--market-blend-weight",
        type=float,
        default=0.80,
        help=(
            "When odds are available, blend model probability with market implied probability. "
            "FinalP = w*ModelP + (1-w)*MarketP. Set to 1.0 to ignore market; 0.0 to fully trust market."
        ),
    )

    # FightOdds.io fetching/matching
    p.add_argument(
        "--sportsbook",
        action="append",
        default=[],
        help=(
            "FightOdds sportsbook filter (repeatable). Example: --sportsbook Pinnacle --sportsbook Bovada. "
            "If omitted, uses bestOdds1/bestOdds2 from FightOdds (best line across books)."
        ),
    )
    p.add_argument("--fightodds-promotion-slug", default="ufc", help="FightOdds promotion slug (default: ufc)")
    p.add_argument(
        "--fightodds-start-date",
        default=None,
        help=(
            "Optional start date (YYYY-MM-DD) for FightOdds event discovery. "
            "Defaults to earliest upcoming event_date minus 7 days."
        ),
    )

    # Ensemble + signal controls (conservative defaults)
    p.add_argument("--ensemble-weight-cat", type=float, default=0.65)
    p.add_argument("--ensemble-weight-logreg", type=float, default=0.35)

    p.add_argument("--strong-min-edge", type=float, default=0.12, help="Minimum betting edge for STRONG signals")
    p.add_argument("--strong-min-agreement", type=float, default=0.85, help="Minimum model agreement for STRONG signals")
    p.add_argument("--medium-min-edge", type=float, default=0.05, help="Minimum betting edge for MEDIUM signals")
    p.add_argument("--medium-min-agreement", type=float, default=0.70, help="Minimum model agreement for MEDIUM signals")

    p.add_argument("--scored-table", default="gold.prefight_features_scored", help="Target scored table to create/replace")

    # Materialized training dataset (append-only)
    p.add_argument(
        "--trainall-table",
        default="gold.trainall_prefight_features",
        help=(
            "Append-only, one-row-per-fight training dataset table. "
            "Built from --view after collapsing to one row per fight (only labeled rows)."
        ),
    )
    p.add_argument(
        "--overwrite-trainall",
        action="store_true",
        default=False,
        help="Drop and rebuild --trainall-table from scratch instead of appending only new fights.",
    )

    wb = p.add_mutually_exclusive_group()
    wb.add_argument("--overwrite", action="store_true", default=False, help="Drop and recreate --scored-table from scratch")
    wb.add_argument("--append", action="store_true", default=False, help="Append only new fights not yet present")

    p.add_argument(
        "--feature-prefix",
        action="append",
        default=["delta_", "prof_", "opp_prof_", "prof_missing", "opp_prof_missing"],
        help="Keep columns whose name starts with this prefix (repeatable)",
    )
    p.add_argument("--exclude-col", action="append", default=["y_win"], help="Explicitly exclude a column (repeatable)")

    p.add_argument(
        "--bfo-promotion-filter",
        default="ufc",
        help="BestFightOdds promotion name filter (default: ufc). Matches event names case-insensitively.",
        )
    return p.parse_args()


# -----------------------------
# Metrics + modeling helpers
# -----------------------------
@dataclass
class Metrics:
    n_train: int
    n_test: int
    auc: float | None
    logloss: float | None
    accuracy: float | None
    brier: float | None
    pr_auc: float | None
    r2: float | None


def _ensure_deps(model: str) -> None:
    if model in ("logreg", "all"):
        import sklearn  # noqa: F401
    if model in ("cat", "all"):
        import catboost  # noqa: F401


def _pick_feature_columns(df: pd.DataFrame, prefixes: Iterable[str], exclude: Iterable[str]) -> List[str]:
    exclude_set = set(exclude)
    keep: List[str] = []

    for col in df.columns:
        if col in exclude_set:
            continue
        for pfx in prefixes:
            if col == pfx or col.startswith(pfx):
                keep.append(col)
                break

    numeric_keep = [c for c in keep if pd.api.types.is_numeric_dtype(df[c])]
    return numeric_keep


# Known categorical columns that CatBoost can handle natively.
# These are NOT prefix-matched; they must be listed explicitly.
_KNOWN_CAT_COLS: List[str] = [
    "weight_class",
    "fighter_stance",
    "opp_fighter_stance",
    "stance_matchup",
    "fighter_status",
    "opp_fighter_status",
]


def _pick_categorical_columns(
    df: pd.DataFrame,
    *,
    allowed: Optional[List[str]] = None,
    max_cardinality: int = 60,
) -> List[str]:
    """Return a list of categorical column names suitable for CatBoost native encoding."""
    candidates = allowed if allowed is not None else list(_KNOWN_CAT_COLS)
    result: List[str] = []
    for c in candidates:
        if c not in df.columns:
            continue
        nuniq = df[c].nunique(dropna=True)
        if nuniq < 1 or nuniq > max_cardinality:
            continue
        result.append(c)
    return result


def _time_split(df: pd.DataFrame, time_col: str, test_frac: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if time_col not in df.columns:
        raise ValueError(f"Time column {time_col!r} not found")

    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    d = d.sort_values(time_col, ascending=True)

    n = len(d)
    if n < 200:
        logger.warning(f"Only {n} rows available; evaluation will be noisy")

    cut = int(np.floor((1.0 - test_frac) * n))
    cut = max(1, min(cut, n - 1))
    return d.iloc[:cut].copy(), d.iloc[cut:].copy()


def _row_feature_coverage(
    df: pd.DataFrame,
    feature_cols: list[str],
    cat_cols: list[str],
) -> pd.Series:
    cols = [c for c in feature_cols if c in df.columns]
    if not cols:
        return pd.Series(1.0, index=df.index)

    missing = np.zeros((len(df), len(cols)), dtype=bool)
    cat_set = set(cat_cols)
    for i, c in enumerate(cols):
        s = df[c]
        if c in cat_set:
            s_txt = s.astype(str).str.strip().str.lower()
            ok = s.notna() & (s_txt != "") & (s_txt != "nan") & (s_txt != "missing")
            missing[:, i] = ~ok.to_numpy()
        else:
            missing[:, i] = s.isna().to_numpy()
    coverage = 1.0 - missing.mean(axis=1)
    return pd.Series(coverage, index=df.index)


def _apply_training_filters(
    df: pd.DataFrame,
    *,
    time_col: str,
    min_event_date: str | None,
    feature_cols: list[str],
    cat_cols: list[str],
    min_feature_coverage: float,
) -> pd.DataFrame:
    out = df.copy()

    if min_event_date:
        if time_col not in out.columns:
            raise ValueError(f"Time column {time_col!r} not found for --min-event-date")
        t = pd.to_datetime(out[time_col], errors="coerce")
        min_dt = pd.to_datetime(min_event_date, errors="coerce")
        if pd.isna(min_dt):
            raise ValueError(f"Invalid --min-event-date: {min_event_date!r}")
        out = out.loc[t >= min_dt].copy()

    if min_feature_coverage and float(min_feature_coverage) > 0.0:
        cov = _row_feature_coverage(out, feature_cols, cat_cols)
        keep = cov >= float(min_feature_coverage)
        logger.info(
            "Training coverage filter: kept %s/%s rows (min_feature_coverage=%.2f)",
            int(keep.sum()),
            int(len(keep)),
            float(min_feature_coverage),
        )
        out = out.loc[keep].copy()

    return out


def _compute_metrics(y_true: np.ndarray, y_proba: np.ndarray) -> Metrics:
    from sklearn.metrics import accuracy_score, average_precision_score, log_loss, roc_auc_score

    auc: float | None
    try:
        auc = float(roc_auc_score(y_true, y_proba))
    except Exception:
        auc = None

    logloss: float | None
    try:
        logloss = float(log_loss(y_true, np.clip(y_proba, 1e-6, 1 - 1e-6)))
    except Exception:
        logloss = None

    y_pred = (y_proba >= 0.5).astype(int)
    accuracy = float(accuracy_score(y_true, y_pred))

    brier: float | None
    try:
        brier = float(np.mean((y_true.astype(float) - y_proba.astype(float)) ** 2))
    except Exception:
        brier = None

    pr_auc: float | None
    try:
        pr_auc = float(average_precision_score(y_true, y_proba))
    except Exception:
        pr_auc = None

    r2: float | None
    try:
        y = y_true.astype(float)
        p = y_proba.astype(float)
        sse = float(np.sum((y - p) ** 2))
        sst = float(np.sum((y - float(np.mean(y))) ** 2))
        r2 = None if sst <= 0 else float(1.0 - (sse / sst))
    except Exception:
        r2 = None

    return Metrics(
        n_train=0,
        n_test=len(y_true),
        auc=auc,
        logloss=logloss,
        accuracy=accuracy,
        brier=brier,
        pr_auc=pr_auc,
        r2=r2,
    )

def best_threshold_for_accuracy(y_true: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """
    Pick threshold that maximizes accuracy on a validation set.
    Does NOT change AUC/logloss/brier because it does not change probabilities.
    """
    y_true = np.asarray(y_true).astype(int)
    p = np.asarray(p).astype(float)

    grid = np.linspace(0.05, 0.95, 901)
    best_t = 0.5
    best_acc = -1.0

    for t in grid:
        acc = float(np.mean((p >= t).astype(int) == y_true))
        if acc > best_acc:
            best_acc = acc
            best_t = float(t)

    return best_t, best_acc


def best_weight_and_threshold_for_accuracy(
    y_valid: np.ndarray, p_cat_valid: np.ndarray, p_lr_valid: np.ndarray
) -> tuple[float, float, float]:
    y_valid = np.asarray(y_valid).astype(int)
    p_cat_valid = np.asarray(p_cat_valid, dtype=float)
    p_lr_valid = np.asarray(p_lr_valid, dtype=float)

    best_acc = -1.0
    best_w = 0.0
    best_t = 0.5

    for w_cat in np.linspace(0.0, 1.0, 101):
        p_ens = w_cat * p_cat_valid + (1.0 - w_cat) * p_lr_valid
        for t in np.linspace(0.05, 0.95, 901):
            acc = float(np.mean((p_ens >= t).astype(int) == y_valid))
            if acc > best_acc:
                best_acc = acc
                best_w = float(w_cat)
                best_t = float(t)

    return best_w, best_t, best_acc

def train_logreg_df(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    test_df: pd.DataFrame,
    *,
    label_col: str,
    num_cols: list[str],
    cat_cols: list[str],
    seed: int = 42,
) -> tuple[np.ndarray, object, float]:
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    # Drop columns that are completely missing in TRAIN
    num_cols = [c for c in num_cols if train_df[c].notna().any()]
    cat_cols = [c for c in cat_cols if train_df[c].notna().any()]
    if not num_cols and not cat_cols:
        raise RuntimeError("No numeric or categorical columns available for LogReg training")

    transformers: list[tuple[str, object, list[str]]] = []
    if num_cols:
        transformers.append(
            (
                "num",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scaler", StandardScaler()),
                    ]
                ),
                num_cols,
            )
        )
    if cat_cols:
        transformers.append(
            (
                "cat",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=10)),
                    ]
                ),
                cat_cols,
            )
        )

    pre = ColumnTransformer(transformers=transformers, remainder="drop")

    best: dict | None = None
    for C in [0.1, 0.3, 1.0, 3.0, 10.0]:
        for cw in [None, "balanced"]:
            model = Pipeline(
                steps=[
                    ("pre", pre),
                    (
                        "clf",
                        LogisticRegression(
                            max_iter=5000,
                            solver="saga",
                            penalty="l2",
                            C=C,
                            class_weight=cw,
                            random_state=seed,
                            n_jobs=-1,
                        ),
                    ),
                ]
            )

            model.fit(train_df[num_cols + cat_cols], train_df[label_col].astype(int))
            p_valid = model.predict_proba(valid_df[num_cols + cat_cols])[:, 1]
            thr, acc = best_threshold_for_accuracy(valid_df[label_col], p_valid)

            if best is None or acc > float(best["acc"]):
                best = {"model": model, "thr": thr, "acc": acc, "C": C, "cw": cw}

    if best is None:
        raise RuntimeError("Failed to train LogReg with any hyperparameter setting")

    p_test = best["model"].predict_proba(test_df[num_cols + cat_cols])[:, 1]
    return np.asarray(p_test, dtype=float), best["model"], float(best["thr"])


def _detect_catboost_gpu_available() -> bool:
    try:
        from catboost.utils import get_gpu_device_count
        return int(get_gpu_device_count()) > 0
    except Exception:
        return False


def train_catboost(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_valid: np.ndarray,
    y_valid: np.ndarray,
    X_test: np.ndarray,
    cat_param_overrides: Optional[dict] = None,
    *,
    device: str = "cpu",
    seed: int = 42,
    cat_feature_indices: Optional[List[int]] = None,
) -> tuple[np.ndarray, object]:
    """Train CatBoostClassifier with early stopping + safe tuned-param overrides."""
    from catboost import CatBoostClassifier, Pool

    pos = float(np.sum(y_train == 1))
    neg = float(np.sum(y_train == 0))
    scale_pos_weight = (neg / pos) if pos > 0 and neg > 0 else 1.0

    task_type = "CPU"
    if device == "cuda":
        task_type = "GPU"

    base_params: dict = {
        "loss_function": "Logloss",
        "eval_metric": "Logloss",
        "iterations": 2000,
        "depth": 6,
        "learning_rate": 0.03,
        "l2_leaf_reg": 3.0,
        "random_strength": 1.0,
        "bagging_temperature": 1.0,
        "od_type": "Iter",
        "od_wait": 50,
        "use_best_model": True,
        "random_seed": int(seed),
        "task_type": task_type,
        "verbose": False,
        "allow_writing_files": False,
        "scale_pos_weight": float(scale_pos_weight),
    }

    tunable_keys = {
        "iterations",
        "depth",
        "learning_rate",
        "l2_leaf_reg",
        "random_strength",
        "bagging_temperature",
        "subsample",
        "rsm",
        "border_count",
        "min_data_in_leaf",
        "od_wait",
        "od_type",
        "bootstrap_type",
        "grow_policy",
        "max_leaves",
        "auto_class_weights",
    }

    if cat_param_overrides:
        applied: dict = {}
        for k, v in cat_param_overrides.items():
            if k in tunable_keys and v is not None:
                base_params[k] = v
                applied[k] = v
        # auto_class_weights and scale_pos_weight are mutually exclusive
        if "auto_class_weights" in applied:
            base_params.pop("scale_pos_weight", None)
            logger.info("Using auto_class_weights=%s; removed scale_pos_weight", applied["auto_class_weights"])
        if applied:
            logger.info(f"Loaded tuned CatBoost params (applied): {applied}")
        else:
            logger.info("Found tuned CatBoost params, but none matched supported tunable keys; using defaults")

    if device == "cuda" and "rsm" in base_params:
        base_params.pop("rsm", None)
        logger.info("Removed rsm for GPU; not supported for classification on GPU")

    clf = CatBoostClassifier(**base_params)

    if cat_feature_indices:
        train_pool = Pool(X_train, label=y_train, cat_features=cat_feature_indices)
        valid_pool = Pool(X_valid, label=y_valid, cat_features=cat_feature_indices)
        clf.fit(train_pool, eval_set=valid_pool)
        test_pool = Pool(X_test, cat_features=cat_feature_indices)
        proba = clf.predict_proba(test_pool)[:, 1]
    else:
        clf.fit(X_train, y_train, eval_set=(X_valid, y_valid))
        proba = clf.predict_proba(X_test)[:, 1]
    return np.asarray(proba, dtype=float), clf


# -----------------------------
# Odds + FightOdds
# -----------------------------
def _market_prob_from_implied(implied_f: np.ndarray, implied_o: np.ndarray) -> np.ndarray:
    f = implied_f.astype(float)
    o = implied_o.astype(float)
    out = f.copy()
    tot = f + o
    ok = np.isfinite(f) & np.isfinite(o) & np.isfinite(tot) & (tot > 0)
    out = np.where(ok, f / tot, out)
    return out


def _detect_odds_col(df: pd.DataFrame) -> Optional[str]:
    candidates: List[str] = []
    for c in df.columns:
        lc = c.lower()
        if "moneyline" in lc or "odds" in lc:
            if lc.startswith("opp_") or "opp" in lc:
                continue
            candidates.append(c)
    if not candidates:
        return None
    for pref in ("moneyline", "odds", "fighter_moneyline", "fighter_odds", "closing_odds", "open_odds"):
        for c in candidates:
            if pref in c.lower():
                return c
    return candidates[0]


def _implied_prob_from_odds_value(v: object, odds_format: str) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    if not np.isfinite(x):
        return None

    fmt = odds_format
    if fmt == "auto":
        if 0.0 <= x <= 1.0:
            fmt = "implied"
        elif x > 1.01:
            fmt = "decimal"
        else:
            fmt = "american"

    if fmt == "implied":
        return x if 0.0 <= x <= 1.0 else None

    if fmt == "decimal":
        if x <= 1.0:
            return None
        return 1.0 / x

    if fmt == "american":
        if x == 0:
            return None
        if x > 0:
            return 100.0 / (x + 100.0)
        return (-x) / ((-x) + 100.0)

    return None


def _fightodds_graphql(query: str, variables: dict) -> dict:
    url = "https://api.fightodds.io/gql"
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ml-kuda-sports-lab/etl",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"FightOdds GraphQL request failed: {e}")

    try:
        data = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"FightOdds returned non-JSON: {e} (first 200 chars={body[:200]!r})")

    if isinstance(data, dict) and data.get("errors"):
        msg = data["errors"][0].get("message") if isinstance(data["errors"], list) else str(data["errors"])
        raise RuntimeError(f"FightOdds GraphQL errors: {msg}")

    return data


def _norm_name(s: object) -> str:
    if s is None:
        return ""
    txt = str(s).strip().lower()
    txt = re.sub(r"\(.*?\)", " ", txt)
    txt = re.sub(r"[^a-z\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _best_offer_odds_value(odds_values: list[object], odds_format: str) -> Optional[float]:
    best_prob: float | None = None
    best_raw: float | None = None
    for v in odds_values:
        ip = _implied_prob_from_odds_value(v, odds_format=odds_format)
        if ip is None:
            continue
        if best_prob is None or ip < best_prob:
            best_prob = float(ip)
            try:
                best_raw = float(v)
            except Exception:
                best_raw = None
    return best_raw


def _fightodds_fetch_events(promotion_slug: str, start_date: str) -> list[dict]:
    query = """query EventSelectorQuery($promotionSlug: String, $startDate: Date) {
  promotion: promotionBySlug(slug: $promotionSlug) {
    events(date_Gte: $startDate) {
      edges {
        node {
          pk
          name
          slug
          startTime
          id
        }
      }
    }
    id
  }
}"""
    data = _fightodds_graphql(query, {"promotionSlug": promotion_slug, "startDate": start_date})
    edges = data.get("data", {}).get("promotion", {}).get("events", {}).get("edges", [])
    out: list[dict] = []
    for e in edges:
        node = (e or {}).get("node") or {}
        if node.get("pk") is None:
            continue
        out.append(node)
    return out


def _fightodds_fetch_event_offer_table(event_pk: int) -> dict:
    query = """query EventOfferTableQrQuery($eventPk: Int!, $isCancelled: Boolean) {
  eventOfferTable(pk: $eventPk, isCancelled: $isCancelled) {
    slug
    pk
    name
    fightOffers {
      edges {
        node {
          id
          fighter1 { firstName lastName slug id }
          fighter2 { firstName lastName slug id }
          bestOdds1
          bestOdds2
          slug
          isCancelled
          straightOffers {
            edges {
              node {
                sportsbook { shortName slug id }
                outcome1 { odds id }
                outcome2 { odds id }
                id
              }
            }
          }
        }
      }
    }
    id
  }
}"""
    data = _fightodds_graphql(query, {"eventPk": int(event_pk), "isCancelled": None})
    return data.get("data", {}).get("eventOfferTable") or {}


def _choose_event_pk_for_row(events: list[dict], event_date: pd.Timestamp, event_name: object) -> Optional[int]:
    if pd.isna(event_date):
        return None
    target = pd.to_datetime(event_date).date()
    target_name = _norm_name(event_name)

    best_pk: Optional[int] = None
    best_score: tuple[int, int] | None = None
    for ev in events:
        try:
            pk = int(ev.get("pk"))
        except Exception:
            continue
        start = ev.get("startTime") or ev.get("date")
        try:
            ev_dt = pd.to_datetime(start, errors="coerce", utc=True)
            if pd.isna(ev_dt):
                continue
            ev_date = ev_dt.tz_convert("America/New_York").tz_localize(None).date()
        except Exception:
            continue

        day_diff = abs((ev_date - target).days)
        if day_diff > 1:
            continue

        name_score = 0
        if target_name:
            ev_name = _norm_name(ev.get("name"))
            if ev_name == target_name:
                name_score = 3
            elif ev_name and target_name and (ev_name in target_name or target_name in ev_name):
                name_score = 2
            elif ev_name and target_name and any(tok in ev_name for tok in target_name.split()):
                name_score = 1

        score = (name_score, -day_diff)
        if best_score is None or score > best_score:
            best_score = score
            best_pk = pk

    return best_pk


def _fightodds_two_sided_odds(
    df: pd.DataFrame,
    promotion_slug: str,
    sportsbooks: list[str],
    odds_format: str,
    start_date: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (fighter_odds, opponent_odds) aligned to df rows."""

    required = ["event_date", "fighter_name", "opponent_name"]
    for c in required:
        if c not in df.columns:
            raise RuntimeError(f"FightOdds odds source requires column {c!r} in the dataset")

    fighter_col = "fighter_name_plain" if "fighter_name_plain" in df.columns else "fighter_name"
    opponent_col = "opponent_name_plain" if "opponent_name_plain" in df.columns else "opponent_name"

    logger.info(f"Fetching FightOdds events for promotion={promotion_slug} from {start_date}...")
    events = _fightodds_fetch_events(promotion_slug=promotion_slug, start_date=start_date)
    if not events:
        raise RuntimeError("FightOdds returned no events; check promotion slug / start date")

    tmp = df[["event_date", "event_name"]].copy() if "event_name" in df.columns else df[["event_date"]].copy()
    tmp["event_date"] = pd.to_datetime(tmp["event_date"], errors="coerce")
    tmp = tmp.dropna(subset=["event_date"]).drop_duplicates()

    event_pk_by_date: dict[pd.Timestamp, int] = {}
    for _, r in tmp.iterrows():
        pk = _choose_event_pk_for_row(events, r["event_date"], r.get("event_name"))
        if pk is not None:
            event_pk_by_date[pd.to_datetime(r["event_date"]).normalize()] = pk

    needed_pks = sorted(set(event_pk_by_date.values()))
    if not needed_pks:
        raise RuntimeError("Could not map any event_date values to FightOdds event PKs")

    sb_norm = {_norm_name(s): s for s in sportsbooks}
    use_books = set(sb_norm.keys())

    fight_maps: dict[int, dict[tuple[str, str], tuple[Optional[float], Optional[float]]]] = {}
    for pk in needed_pks:
        logger.info(f"Fetching FightOdds offer table for eventPk={pk}...")
        event_offer = _fightodds_fetch_event_offer_table(pk)
        fight_edges = ((event_offer.get("fightOffers") or {}).get("edges") or [])

        fmap: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}
        for edge in fight_edges:
            node = (edge or {}).get("node") or {}
            f1 = node.get("fighter1") or {}
            f2 = node.get("fighter2") or {}

            n1 = _norm_name(f"{f1.get('firstName','')} {f1.get('lastName','')}")
            n2 = _norm_name(f"{f2.get('firstName','')} {f2.get('lastName','')}")
            if not n1 or not n2:
                continue

            if not use_books:
                o1 = node.get("bestOdds1")
                o2 = node.get("bestOdds2")
                try:
                    o1v = float(o1) if o1 is not None else None
                except Exception:
                    o1v = None
                try:
                    o2v = float(o2) if o2 is not None else None
                except Exception:
                    o2v = None
            else:
                offers = ((node.get("straightOffers") or {}).get("edges") or [])
                o1_vals: list[object] = []
                o2_vals: list[object] = []
                for oe in offers:
                    on = (oe or {}).get("node") or {}
                    sb = (on.get("sportsbook") or {})
                    sb_name = _norm_name(sb.get("shortName") or sb.get("slug"))
                    if sb_name not in use_books:
                        continue
                    o1_vals.append(((on.get("outcome1") or {}).get("odds")))
                    o2_vals.append(((on.get("outcome2") or {}).get("odds")))
                o1v = _best_offer_odds_value(o1_vals, odds_format=odds_format)
                o2v = _best_offer_odds_value(o2_vals, odds_format=odds_format)

            fmap[(n1, n2)] = (o1v, o2v)

        fight_maps[int(pk)] = fmap

    fighter_odds = np.full(shape=len(df), fill_value=np.nan, dtype=float)
    opponent_odds = np.full(shape=len(df), fill_value=np.nan, dtype=float)

    df_event = pd.to_datetime(df["event_date"], errors="coerce").dt.normalize()
    for i in range(len(df)):
        d = df_event.iat[i]
        if pd.isna(d):
            continue
        pk = event_pk_by_date.get(pd.to_datetime(d).normalize())
        if pk is None:
            continue
        fmap = fight_maps.get(pk) or {}

        fn = _norm_name(df[fighter_col].iat[i])
        on = _norm_name(df[opponent_col].iat[i])
        if not fn or not on:
            continue

        pair = fmap.get((fn, on))
        if pair is not None:
            o_f, o_o = pair[0], pair[1]
        else:
            pair2 = fmap.get((on, fn))
            if pair2 is None:
                continue
            o_f, o_o = pair2[1], pair2[0]

        if o_f is not None:
            fighter_odds[i] = float(o_f)
        if o_o is not None:
            opponent_odds[i] = float(o_o)

    return fighter_odds, opponent_odds


def _oddsapi_two_sided_odds(
    df: pd.DataFrame,
    *,
    api_key: str,
    sport_key: str,
    regions: str,
    odds_format: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Fetch moneyline odds from The Odds API and map them to df rows by fighter/opponent names + date."""
    required = ["event_date", "fighter_name", "opponent_name"]
    for c in required:
        if c not in df.columns:
            raise RuntimeError(f"The Odds API fallback requires column {c!r}")

    if not api_key:
        raise RuntimeError("THE_ODDS_API_KEY is not set for odds fallback")

    qs = urlencode(
        {
            "apiKey": api_key,
            "regions": regions,
            "markets": "h2h",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
    )
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ml-kuda-sports-lab/etl",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"The Odds API request failed: {e}")

    try:
        events = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"The Odds API returned non-JSON: {e}")

    if not isinstance(events, list):
        raise RuntimeError("The Odds API payload is not an events list")

    event_rows: list[dict[str, object]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        commence = ev.get("commence_time")
        try:
            ev_dt = pd.to_datetime(commence, errors="coerce", utc=True)
        except Exception:
            ev_dt = pd.NaT
        if pd.isna(ev_dt):
            continue

        fighter_prices: dict[str, list[float]] = {}
        for bk in ev.get("bookmakers") or []:
            if not isinstance(bk, dict):
                continue
            for m in bk.get("markets") or []:
                if not isinstance(m, dict) or str(m.get("key", "")).strip().lower() != "h2h":
                    continue
                for out in m.get("outcomes") or []:
                    if not isinstance(out, dict):
                        continue
                    nm = _norm_name(out.get("name"))
                    if not nm:
                        continue
                    try:
                        px = float(out.get("price"))
                    except Exception:
                        continue
                    fighter_prices.setdefault(nm, []).append(px)

        if len(fighter_prices) < 2:
            continue

        names = sorted(fighter_prices.keys())
        if len(names) != 2:
            continue

        p1 = _best_offer_odds_value(fighter_prices[names[0]], odds_format="american")
        p2 = _best_offer_odds_value(fighter_prices[names[1]], odds_format="american")
        if p1 is None or p2 is None:
            continue

        event_rows.append(
            {
                "event_date": pd.to_datetime(ev_dt).tz_convert(None),
                "f1": names[0],
                "f2": names[1],
                "o1": float(p1),
                "o2": float(p2),
            }
        )

    if not event_rows:
        raise RuntimeError("The Odds API returned no usable MMA h2h odds rows")

    evt_df = pd.DataFrame(event_rows)
    fighter_odds = np.full(shape=len(df), fill_value=np.nan, dtype=float)
    opponent_odds = np.full(shape=len(df), fill_value=np.nan, dtype=float)

    src_event_dt = pd.to_datetime(df["event_date"], errors="coerce", utc=True).dt.tz_convert(None)
    for i in range(len(df)):
        fn = _norm_name(df["fighter_name"].iat[i])
        on = _norm_name(df["opponent_name"].iat[i])
        if not fn or not on:
            continue

        d = src_event_dt.iat[i]
        if pd.isna(d):
            continue

        lo, hi = sorted([fn, on])
        cand = evt_df[(evt_df["f1"] == lo) & (evt_df["f2"] == hi)].copy()
        if cand.empty:
            continue

        cand["ddays"] = (cand["event_date"].dt.normalize() - pd.to_datetime(d).normalize()).dt.days.abs()
        cand = cand[cand["ddays"] <= 3]
        if cand.empty:
            continue
        best = cand.sort_values("ddays", ascending=True).iloc[0]

        if fn == str(best["f1"]):
            fighter_odds[i] = float(best["o1"])
            opponent_odds[i] = float(best["o2"])
        else:
            fighter_odds[i] = float(best["o2"])
            opponent_odds[i] = float(best["o1"])

    return fighter_odds, opponent_odds


# ─────────────────────────────────────────────────────────────────────────────
# BFO scraper — rewritten to match actual HTML structure
# ─────────────────────────────────────────────────────────────────────────────
_BFO_BASE = "https://www.bestfightodds.com"

_RE_EVENT_URL   = re.compile(r'href="(/events/[^"]+)"', re.I)
_RE_FIGHTER_ROW = re.compile(
    r'<tr\s*>\s*<th[^>]*>\s*<a href="(/fighters/[^"]+)"[^>]*>\s*<span class="t-b-fcc">([^<]+)</span>',
    re.S,
)
_RE_FIGHTER_ODDS = re.compile(r'<td class="but-sg"[^>]*><span[^>]*>([\+\-]?\d+)</span>', re.S)
_RE_PROP_ROW     = re.compile(r'<tr class="pr"', re.I)


def _bfo_fetch_html(url: str, *, timeout: int = 30) -> str:
    """Fetch HTML from a BFO URL with a browser-like User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"BFO fetch failed for {url}: {e}")


def _bfo_extract_h2h_fights(html: str) -> list[dict]:
    """
    Extract moneyline (h2h) fights from a BFO event or homepage HTML.

    Fighter rows are <tr > (no class) with <span class="t-b-fcc">.
    Prop rows have class="pr" and are skipped.
    Odds come from <td class="but-sg"> (not but-sgp).
    """
    # Split on <tr> boundaries, keep prop rows separate
    # We need to process the HTML row by row.
    # Strategy: split the HTML at each <tr...> tag, classify each chunk.
    
    tr_split = re.split(r'(?=<tr[\s>])', html, flags=re.I)
    
    fights: list[dict] = []
    pending: dict | None = None   # first fighter in current bout

    for chunk in tr_split:
        # Skip prop rows
        if re.match(r'<tr\s+class="pr"', chunk, re.I):
            # A prop row between two fighter rows resets the pairing
            # (don't reset pending - props appear AFTER the fight rows pair)
            continue

        # Is this a fighter row?
        m = _RE_FIGHTER_ROW.search(chunk)
        if not m:
            continue

        fighter_url  = m.group(1)          # /fighters/Dustin-Poirier-2034
        fighter_name = m.group(2).strip()  # "Dustin Poirier"

        # Collect best-line American odds from but-sg cells in this row
        raw_odds = [int(v) for v in _RE_FIGHTER_ODDS.findall(chunk)]

        def _impl(o: int) -> float:
            return 100.0 / (o + 100.0) if o >= 0 else (-o) / (-o + 100.0)

        best_odds: int | None = min(raw_odds, key=_impl) if raw_odds else None

        if pending is None:
            pending = {
                "f1_display": fighter_name,
                "f1_url":     fighter_url,
                "odds1":      best_odds,
            }
        else:
            fights.append({
                "f1_display": pending["f1_display"],
                "f2_display": fighter_name,
                "odds1":      pending["odds1"],
                "odds2":      best_odds,
            })
            pending = None

    return fights


def _bfo_fetch_upcoming_events(*, promotion_filter: str = "ufc") -> list[dict]:
    """
    Fetch the BFO homepage. Returns list of {name, url, fights} dicts
    whose names contain `promotion_filter`.

    BFO homepage lists upcoming events directly — we parse h2h fights
    straight from the homepage HTML, and also collect event page URLs
    for callers who want per-event detail fetches.
    """
    html = _bfo_fetch_html(_BFO_BASE + "/")
    promo_lc = promotion_filter.lower()

    # Extract event URLs + names from odds-table-responsive-header tables
    # Pattern: <table class="odds-table odds-table-responsive-header">...<a href="/events/...">NAME</a>
    header_re = re.compile(
        r'<table class="odds-table odds-table-responsive-header"[^>]*>(.*?)</table>',
        re.S | re.I,
    )
    event_link_re = re.compile(
        r'href="(/events/([^"]+))"[^>]*>([^<]+)<',
        re.I,
    )

    events: list[dict] = []
    seen_urls: set[str] = set()

    for hdr_match in header_re.finditer(html):
        hdr_html = hdr_match.group(1)
        for lm in event_link_re.finditer(hdr_html):
            path  = lm.group(1)
            name  = lm.group(3).strip()
            url   = _BFO_BASE + path
            if url in seen_urls:
                continue
            seen_urls.add(url)
            events.append({"name": name, "url": url, "fights": []})

    # Filter by promotion
    filtered = [ev for ev in events if promo_lc in ev["name"].lower()]
    if not filtered:
        filtered = events   # fallback: return all

    # Extract all h2h fights from the homepage in one pass
    all_fights = _bfo_extract_h2h_fights(html)
    logger.info(
        f"BFO homepage: {len(events)} events found, "
        f"{len(filtered)} match '{promotion_filter}', "
        f"{len(all_fights)} h2h fights parsed"
    )

    # Attach fights to events by URL (events don't have fights embedded on homepage,
    # so we store the homepage fights globally and let _bfo_two_sided_odds use them directly)
    for ev in filtered:
        ev["fights"] = all_fights  # same pool for all — deduped by fighter name match

    return filtered, all_fights   # return both


def _bfo_two_sided_odds(
    df: pd.DataFrame,
    *,
    promotion_filter: str = "ufc",
    odds_format: str = "american",
) -> tuple[np.ndarray, np.ndarray]:
    """
    Scrape BestFightOdds.com and return (fighter_odds, opponent_odds) aligned to df rows.
    """
    required = ["event_date", "fighter_name", "opponent_name"]
    for c in required:
        if c not in df.columns:
            raise RuntimeError(f"BFO odds source requires column {c!r} in the dataset")

    fighter_col = "fighter_name_plain" if "fighter_name_plain" in df.columns else "fighter_name"
    opponent_col = "opponent_name_plain" if "opponent_name_plain" in df.columns else "opponent_name"

    logger.info(f"BFO: fetching upcoming events for promotion='{promotion_filter}'...")
    events_meta, all_fights = _bfo_fetch_upcoming_events(promotion_filter=promotion_filter)

    if not all_fights:
        raise RuntimeError(
            "BestFightOdds returned no h2h fights. "
            "Check --bfo-promotion-filter or verify the site is reachable."
        )

    # Build lookup: norm_pair → (odds1, odds2)
    pair_lookup: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for fight in all_fights:
        n1 = _norm_name(fight.get("f1_display") or "")
        n2 = _norm_name(fight.get("f2_display") or "")
        if not n1 or not n2:
            continue
        o1, o2 = fight.get("odds1"), fight.get("odds2")
        pair_lookup[(n1, n2)] = (o1, o2)
        pair_lookup[(n2, n1)] = (o2, o1)

    # Optionally also fetch individual event pages for events with no odds yet
    if not pair_lookup and events_meta:
        for ev_meta in events_meta[:5]:   # cap at 5 to avoid hammering the site
            _time.sleep(1.5)
            try:
                ev_html = _bfo_fetch_html(ev_meta["url"])
                ev_fights = _bfo_extract_h2h_fights(ev_html)
                for fight in ev_fights:
                    n1 = _norm_name(fight.get("f1_display") or "")
                    n2 = _norm_name(fight.get("f2_display") or "")
                    if not n1 or not n2:
                        continue
                    pair_lookup[(n1, n2)] = (fight.get("odds1"), fight.get("odds2"))
                    pair_lookup[(n2, n1)] = (fight.get("odds2"), fight.get("odds1"))
            except Exception as e:
                logger.warning(f"BFO: failed to fetch event page {ev_meta['url']}: {e}")

    if not pair_lookup:
        raise RuntimeError("BFO scraped homepage but found no fight odds. Check logs.")

    logger.info(f"BFO: loaded odds for {len(pair_lookup) // 2} unique bouts")

    fighter_odds  = np.full(len(df), np.nan, dtype=float)
    opponent_odds = np.full(len(df), np.nan, dtype=float)

    for i in range(len(df)):
        fn = _norm_name(df[fighter_col].iat[i])
        on = _norm_name(df[opponent_col].iat[i])
        if not fn or not on:
            continue

        pair = pair_lookup.get((fn, on))
        if pair is None:
            all_names = {k[0] for k in pair_lookup}
            close = difflib.get_close_matches(fn, all_names, n=1, cutoff=0.80)
            if close:
                matched = close[0]
                opp_names = {k[1] for k in pair_lookup if k[0] == matched}
                close2 = difflib.get_close_matches(on, opp_names, n=1, cutoff=0.80)
                if close2:
                    pair = pair_lookup.get((matched, close2[0]))

        if pair is None:
            continue

        o_f, o_o = pair
        if o_f is not None:
            fighter_odds[i]  = float(o_f)
        if o_o is not None:
            opponent_odds[i] = float(o_o)

    matched = int(np.sum(np.isfinite(fighter_odds)))
    logger.info(f"BFO: matched odds for {matched}/{len(df)} rows")
    return fighter_odds, opponent_odds
# -----------------------------
# Signals / recommendation
# -----------------------------
def _confidence_from_two_models(p1: np.ndarray, p2: np.ndarray) -> np.ndarray:
    return np.clip(1.0 - np.abs(p1 - p2), 0.0, 1.0)


def _signal_strength(
    confidence: np.ndarray,
    edge: np.ndarray,
    strong_min_agreement: float,
    strong_min_edge: float,
    med_min_agreement: float,
    med_min_edge: float,
) -> np.ndarray:
    out = np.full(shape=len(confidence), fill_value="WEAK", dtype=object)
    strong = (confidence >= strong_min_agreement) & (edge >= strong_min_edge)
    medium = (confidence >= med_min_agreement) & (edge >= med_min_edge)
    out[medium] = "MEDIUM"
    out[strong] = "STRONG"
    return out


def _signal_score(confidence: np.ndarray, edge: np.ndarray) -> np.ndarray:
    edge_component = np.clip(edge / 0.20, 0.0, 1.0)
    score01 = np.clip(0.8 * confidence + 0.2 * edge_component, 0.0, 1.0)
    return 100.0 * score01


# -----------------------------
# DuckDB I/O helpers
# -----------------------------
def _duckdb_object_type(conn: duckdb.DuckDBPyConnection, qualified_name: str) -> Optional[str]:
    if "." not in qualified_name:
        schema = "main"
        name = qualified_name
    else:
        schema, name = qualified_name.split(".", 1)

    row = conn.execute(
        """
        SELECT table_type
        FROM information_schema.tables
        WHERE table_schema = ? AND table_name = ?
        """,
        [schema, name],
    ).fetchone()
    if not row:
        return None
    return row[0]


def _ensure_columns_exist(conn: duckdb.DuckDBPyConnection, table_name: str, cols: List[Tuple[str, str]]) -> None:
    for col_name, col_type in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_type}")


def _write_trainall_dataset(
    conn: duckdb.DuckDBPyConnection,
    trainall_table: str,
    trainall_df: pd.DataFrame,
    *,
    mode: str,
) -> None:
    """Persist an append-only training dataset (ONE ROW PER FIGHT)."""
    if trainall_df.empty:
        logger.warning("trainall_df is empty; skipping trainall dataset write")
        return

    if "fight_key" in trainall_df.columns:
        key_col = "fight_key"
    elif "bout_id" in trainall_df.columns:
        key_col = "bout_id"
    else:
        key_col = "fight_id"

    for k in ["organization", key_col]:
        if k not in trainall_df.columns:
            raise RuntimeError(f"trainall_df missing key column {k!r}; cannot write trainall dataset")

    # DuckDB doesn't accept pandas StringDtype; normalize to object.
    trainall_df = _normalize_string_dtypes(trainall_df)

    conn.register("_trainall_df", trainall_df)
    conn.execute("CREATE OR REPLACE TEMP TABLE _trainall AS SELECT * FROM _trainall_df")

    mode = (mode or "append").lower()
    if mode not in {"overwrite", "append"}:
        raise ValueError("mode must be 'overwrite' or 'append'")

    if mode == "overwrite":
        conn.execute(f"DROP TABLE IF EXISTS {trainall_table}")
        conn.execute(f"CREATE TABLE {trainall_table} AS SELECT * FROM _trainall")
    else:
        if _duckdb_object_type(conn, trainall_table) is None:
            conn.execute(f"CREATE TABLE {trainall_table} AS SELECT * FROM _trainall")
        else:
            cols = conn.execute("DESCRIBE _trainall").fetchall()
            to_add: list[tuple[str, str]] = []
            for row in cols:
                col_name = row[0]
                col_type = row[1]
                if col_name in {"organization", key_col}:
                    continue
                to_add.append((col_name, col_type))
            _ensure_columns_exist(conn, trainall_table, to_add)

            conn.execute(
                f"""
                INSERT INTO {trainall_table}
                SELECT s.*
                FROM _trainall s
                WHERE NOT EXISTS (
                  SELECT 1 FROM {trainall_table} t
                  WHERE t.organization = s.organization
                    AND t.{key_col} = s.{key_col}
                )
                """
            )

    n = conn.execute(f"SELECT COUNT(*) FROM {trainall_table}").fetchone()[0]
    logger.info(f"trainall dataset: {trainall_table} now has {n:,} rows")


def _normalize_string_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Convert pandas StringDtype columns to object so DuckDB accepts them."""
    df = df.copy()
    for col in df.columns:
        try:
            if hasattr(pd, "StringDtype") and isinstance(df[col].dtype, pd.StringDtype):
                df[col] = df[col].astype("object")
            elif "string" in str(df[col].dtype).lower():
                df[col] = df[col].astype("object")
        except Exception:
            pass
    return df


def _write_scores_back(
    conn: duckdb.DuckDBPyConnection,
    base_name: str,
    scored_table: str,
    scores_df: pd.DataFrame,
    mode: str,
    bootstrap_full_df: pd.DataFrame | None = None,
) -> None:
    """Write score outputs to DuckDB (ONE ROW PER FIGHT)."""
    scores_df = _normalize_string_dtypes(scores_df)
    if bootstrap_full_df is not None:
        bootstrap_full_df = _normalize_string_dtypes(bootstrap_full_df)

    obj_type = _duckdb_object_type(conn, base_name)
    if not obj_type:
        raise RuntimeError(f"DuckDB object not found: {base_name}")

    def _pick_key_col(df: pd.DataFrame) -> str:
        for c in ("fight_key", "bout_id", "fight_id"):
            if c not in df.columns:
                continue
            s = df[c]
            try:
                ok = s.notna() & (s.astype(str).str.strip() != "") & (s.astype(str).str.lower() != "nan")
                if bool(ok.any()):
                    return c
            except Exception:
                return c
        return "fight_id"

    key_col = _pick_key_col(scores_df)
    for k in ["organization", key_col]:
        if k not in scores_df.columns:
            raise RuntimeError(f"scores_df missing key column {k!r}; cannot write back")

    mode = (mode or "overwrite").lower()
    if mode not in {"overwrite", "append", "only_upcoming"}:
        raise ValueError("mode must be 'overwrite', 'append', or 'only_upcoming'")

    if mode == "overwrite":
        conn.register("_scores_df", scores_df)
        conn.execute("CREATE OR REPLACE TEMP TABLE _scores AS SELECT * FROM _scores_df")
        conn.execute(f"DROP TABLE IF EXISTS {scored_table}")
        conn.execute(f"CREATE TABLE {scored_table} AS SELECT * FROM _scores")

    elif mode == "append":
        conn.register("_scores_df", scores_df)
        conn.execute("CREATE OR REPLACE TEMP TABLE _scores AS SELECT * FROM _scores_df")
        if _duckdb_object_type(conn, scored_table) is None:
            conn.execute(f"CREATE TABLE {scored_table} AS SELECT * FROM _scores")
        else:
            cols = conn.execute("DESCRIBE _scores").fetchall()
            to_add: list[tuple[str, str]] = []
            for row in cols:
                col_name = row[0]
                col_type = row[1]
                if col_name == "organization":
                    continue
                to_add.append((col_name, col_type))
            _ensure_columns_exist(conn, scored_table, to_add)

            conn.execute(
                f"""
                INSERT INTO {scored_table}
                SELECT s.*
                FROM _scores s
                WHERE NOT EXISTS (
                  SELECT 1 FROM {scored_table} t
                  WHERE t.organization = s.organization
                    AND t.{key_col} = s.{key_col}
                )
                """
            )

    else:
        # only_upcoming refresh
        if _duckdb_object_type(conn, scored_table) is None:
            if bootstrap_full_df is None:
                raise RuntimeError(
                    f"{scored_table} does not exist and bootstrap_full_df was not provided for only_upcoming mode"
                )
            conn.register("_bootstrap_scores_df", bootstrap_full_df)
            conn.execute("CREATE OR REPLACE TEMP TABLE _bootstrap_scores AS SELECT * FROM _bootstrap_scores_df")
            conn.execute(f"CREATE TABLE {scored_table} AS SELECT * FROM _bootstrap_scores")

        conn.register("_upcoming_scores_df", scores_df)
        conn.execute("CREATE OR REPLACE TEMP TABLE _upcoming_scores AS SELECT * FROM _upcoming_scores_df")

        if scores_df.empty:
            logger.info("only_upcoming: no upcoming rows to refresh; leaving scored table unchanged")
        else:
            cols = conn.execute("DESCRIBE _upcoming_scores").fetchall()
            to_add: list[tuple[str, str]] = []
            for row in cols:
                col_name = row[0]
                col_type = row[1]
                if col_name == "organization":
                    continue
                to_add.append((col_name, col_type))
            _ensure_columns_exist(conn, scored_table, to_add)

            conn.execute(
                f"""
                DELETE FROM {scored_table} t
                USING _upcoming_scores s
                WHERE t.organization = s.organization
                  AND t.{key_col} = s.{key_col}
                """
            )

            insert_cols = [r[0] for r in conn.execute("DESCRIBE _upcoming_scores").fetchall()]

            def _q(c: str) -> str:
                return '"' + str(c).replace('"', '""') + '"'

            col_list = ", ".join(_q(c) for c in insert_cols)
            select_list = ", ".join(_q(c) for c in insert_cols)
            conn.execute(
                f"""
                INSERT INTO {scored_table} ({col_list})
                SELECT {select_list} FROM _upcoming_scores
                """
            )

    n = conn.execute(f"SELECT COUNT(*) FROM {scored_table}").fetchone()[0]
    logger.info(f"Created scored TABLE {scored_table} with {n:,} rows (base was {obj_type})")
def _write_metrics_to_duckdb(conn, *, metrics_table: str, target: str, results: dict) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {metrics_table} (
            created_at TIMESTAMP,
            target VARCHAR,
            model_name VARCHAR,
            metrics_json VARCHAR
        )
    """)

    # one row per model in results
    for model_name, metrics in results.items():
        conn.execute(
            f"INSERT INTO {metrics_table} VALUES (?, ?, ?, ?)",
            [datetime.now(timezone.utc), target, model_name, json.dumps(metrics)],
        )

    logger.info(f"Wrote {len(results)} metric rows to {metrics_table}")
    

# -----------------------------
# Identity / collapse / event metadata
# -----------------------------
def _clean_ufc_id(x: object) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    return s if s.lower().startswith("ufc_") else None


def _collapse_one_row_per_fight(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Collapse 2-rows-per-fight into exactly 1 row per fight."""
    required = ["organization", "fighter_name", "opponent_name", time_col]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"One-row-per-fight collapse requires columns: {missing}")

    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    d["_date_key"] = d[time_col].dt.strftime("%Y-%m-%d")

    have_ids = "fighter_id" in d.columns and "opponent_id" in d.columns
    have_name_keys = "fighter_name_key" in d.columns and "opponent_name_key" in d.columns

    if have_name_keys:
        f_name_key = d["fighter_name_key"].astype(str)
        o_name_key = d["opponent_name_key"].astype(str)
        f_ok = ~f_name_key.isin(["", "None", "nan"])
        o_ok = ~o_name_key.isin(["", "None", "nan"])
        fallback_f_key = np.where(f_ok, f_name_key, d["fighter_name"].map(_norm_name))
        fallback_o_key = np.where(o_ok, o_name_key, d["opponent_name"].map(_norm_name))
    else:
        fallback_f_key = d["fighter_name"].map(_norm_name)
        fallback_o_key = d["opponent_name"].map(_norm_name)

    if have_ids:
        f_id = d["fighter_id"].astype(str)
        o_id = d["opponent_id"].astype(str)
        ids_ok = f_id.str.lower().str.startswith("ufc_") & o_id.str.lower().str.startswith("ufc_")
        d["_f_key"] = np.where(ids_ok, f_id, fallback_f_key)
        d["_o_key"] = np.where(ids_ok, o_id, fallback_o_key)
    else:
        d["_f_key"] = fallback_f_key
        d["_o_key"] = fallback_o_key

    d["_pair_min"] = np.where(d["_f_key"] <= d["_o_key"], d["_f_key"], d["_o_key"])
    d["_pair_max"] = np.where(d["_f_key"] <= d["_o_key"], d["_o_key"], d["_f_key"])
    d["fight_key"] = d["_date_key"] + "|" + d["_pair_min"].astype(str) + "|" + d["_pair_max"].astype(str)

    d["_f_norm"] = d["fighter_name"].map(_norm_name)
    d["_o_norm"] = d["opponent_name"].map(_norm_name)
    d["_o_missing"] = (d["_o_norm"] == "") | d["opponent_name"].isna()

    d["_canon_name"] = np.where(d["_o_missing"], d["_f_norm"], np.minimum(d["_f_norm"], d["_o_norm"]))
    d["_is_canon_row"] = d["_f_norm"] == d["_canon_name"]

    sort_cols = ["organization", "fight_key", "_is_canon_row", "_o_missing", time_col, "_f_norm", "_o_norm"]
    ascending = [True, True, False, True, True, True, True]
    d = d.sort_values(sort_cols, ascending=ascending, na_position="last")

    out = d.drop_duplicates(subset=["organization", "fight_key"], keep="first").copy()
    out = out.drop(
        columns=[
            "_date_key",
            "_f_key",
            "_o_key",
            "_pair_min",
            "_pair_max",
            "_f_norm",
            "_o_norm",
            "_o_missing",
            "_canon_name",
            "_is_canon_row",
        ],
        errors="ignore",
    )
    return out


def _attach_event_location(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """Attach event_id/event_url/location from silver.events if missing."""
    if "event_date" not in df.columns:
        return df

    def _has_any_nonnull(col: str) -> bool:
        return col in df.columns and bool(df[col].notna().any())

    if _has_any_nonnull("location") and _has_any_nonnull("event_url") and _has_any_nonnull("event_id"):
        return df

    try:
        ev = conn.execute(
            """
            SELECT
                organization,
                CAST(event_date AS DATE) AS event_date_d,
                MAX(event_id) AS event_id,
                MAX(event_url) AS event_url,
                MAX(location) AS location
            FROM silver.events
            WHERE organization IS NOT NULL
            GROUP BY 1, 2
            """
        ).fetch_df()
    except Exception as e:
        logger.warning(f"Failed to fetch event locations from silver.events: {e}")
        return df

    if ev.empty:
        return df

    out = df.copy()
    out["_event_date_d"] = pd.to_datetime(out["event_date"], errors="coerce").dt.date
    ev["event_date_d"] = pd.to_datetime(ev["event_date_d"], errors="coerce").dt.date
    out = out.merge(
        ev,
        how="left",
        left_on=["organization", "_event_date_d"],
        right_on=["organization", "event_date_d"],
        suffixes=("", "_ev"),
    )

    for col in ("event_id", "event_url", "location"):
        base = out.get(col)
        alt = out.get(f"{col}_ev")
        if base is None and alt is None:
            continue
        if base is None and alt is not None:
            out[col] = alt
        elif base is not None and alt is not None:
            if col in ("event_id", "event_url"):
                b = base.astype(str).str.strip()
                missing = base.isna() | (b == "") | (b.str.lower() == "nan")
                out[col] = np.where(missing, alt, base)
            else:
                out[col] = base.where(base.notna(), alt)

        out = out.drop(columns=[f"{col}_ev"], errors="ignore")

    out = out.drop(columns=["_event_date_d", "event_date_d"], errors="ignore")
    return out


def _attach_event_status_and_upcoming(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """Attach event_status from silver.events + derive is_upcoming + days_until_event."""
    if "event_date" not in df.columns:
        return df

    try:
        ev = conn.execute(
            """
            SELECT
                organization,
                event_url,
                CAST(MAX(event_date) AS DATE) AS event_date_d,
                MAX(status) AS event_status
            FROM silver.events
            WHERE coalesce(event_url, '') <> ''
              AND organization IS NOT NULL
            GROUP BY 1, 2
            """
        ).fetch_df()
    except Exception as e:
        logger.warning(f"Failed to fetch event_status from silver.events: {e}")
        ev = pd.DataFrame()

    out = df.copy()

    if not ev.empty:
        out["_event_date_d"] = pd.to_datetime(out["event_date"], errors="coerce").dt.date
        ev["event_date_d"] = pd.to_datetime(ev["event_date_d"], errors="coerce").dt.date

        if "event_url" in out.columns:
            out = out.merge(
                ev[["organization", "event_url", "event_status", "event_date_d"]],
                how="left",
                on=["organization", "event_url"],
                suffixes=("", "_ev"),
            )
        else:
            out = out.merge(
                ev[["organization", "event_status", "event_date_d"]].drop_duplicates(),
                how="left",
                left_on=["organization", "_event_date_d"],
                right_on=["organization", "event_date_d"],
                suffixes=("", "_ev"),
            )

        if "event_status" not in out.columns:
            out["event_status"] = out.get("event_status_ev")
        else:
            base = out["event_status"]
            alt = out.get("event_status_ev")
            if alt is not None:
                b = base.astype(str).str.strip()
                missing = base.isna() | (b == "") | (b.str.lower() == "nan")
                out["event_status"] = np.where(missing, alt, base)

        out = out.drop(columns=["event_status_ev"], errors="ignore")
        out = out.drop(columns=["_event_date_d", "event_date_d"], errors="ignore")

    event_dt = pd.to_datetime(out["event_date"], errors="coerce", utc=True).dt.tz_convert(None)
    today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    status = out["event_status"].astype(str).str.lower() if "event_status" in out.columns else None
    status_upcoming = (status == "upcoming") if status is not None else False
    date_upcoming = event_dt >= today
    out["is_upcoming"] = np.where(pd.isna(event_dt), status_upcoming, (date_upcoming | status_upcoming)).astype(bool)
    days = (event_dt.dt.normalize() - today).dt.days
    out["days_until_event"] = days.astype("Int64")

    return out


def _sportsbook_to_col_suffix(book: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(book).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "sportsbook"


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    args = parse_args()
    _ensure_deps(args.model)

    db_path = resolve_duckdb_path(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Using DuckDB at {db_path}")
    logger.info(f"Source (scoring): {args.view}")
    logger.info(f"Trainall dataset table: {args.trainall_table}")
    logger.info(
        "This script does not guarantee profits; signal thresholds are conservative by default. "
        "Always validate with proper backtests and risk controls."
    )

    conn = duckdb.connect(db_path)
    conn.execute("PRAGMA enable_progress_bar = FALSE")

    # Load tuned CatBoost params (safe fallback to defaults)
    cat_param_overrides = _load_latest_best_params(
        conn,
        target=args.target,
        model_name=args.catbest_model_name,
        params_table=args.best_params_table,
    )

    # Pull full scoring matrix (usually 2 rows per fight)
    all_df_raw = conn.execute(f"SELECT * FROM {args.view}").fetch_df()
    if all_df_raw.empty:
        raise RuntimeError(f"No rows found in {args.view}")

    required_base = ["organization", "fight_id", "fighter_name", "opponent_name", args.time_col]
    missing_base = [c for c in required_base if c not in all_df_raw.columns]
    if missing_base:
        raise RuntimeError(
            "One-row-per-fight scoring requires these columns in the dataset: "
            f"{missing_base}. Ensure gold.prefight_features selects names + event_date."
        )

    tmp = all_df_raw.copy()

    # Guardrail: IDs are either real UFC ids or NULL.
    if "fighter_id" in tmp.columns:
        tmp["fighter_id"] = tmp["fighter_id"].map(_clean_ufc_id)
    if "opponent_id" in tmp.columns:
        tmp["opponent_id"] = tmp["opponent_id"].map(_clean_ufc_id)

    # Prefer display names if present (nickname-aware)
    if "fighter_name_display" in tmp.columns:
        f_disp = tmp["fighter_name_display"].astype(str).str.strip()
        f_ok = tmp["fighter_name_display"].notna() & (f_disp != "") & (f_disp.str.lower() != "nan")
        tmp["fighter_name"] = np.where(f_ok, tmp["fighter_name_display"], tmp["fighter_name"])
    if "opponent_name_display" in tmp.columns:
        o_disp = tmp["opponent_name_display"].astype(str).str.strip()
        o_ok = tmp["opponent_name_display"].notna() & (o_disp != "") & (o_disp.str.lower() != "nan")
        tmp["opponent_name"] = np.where(o_ok, tmp["opponent_name_display"], tmp["opponent_name"])

    # Ensure we have bout_id
    if "bout_id" not in tmp.columns:
        tmp["bout_id"] = tmp["fight_id"]
    else:
        b = tmp["bout_id"].astype(str).str.strip()
        b_missing = tmp["bout_id"].isna() | (b == "") | (b.str.lower() == "nan")
        if bool(b_missing.any()):
            tmp.loc[b_missing, "bout_id"] = tmp.loc[b_missing, "fight_id"]

    # Collapse to 1 row per fight
    all_df = _collapse_one_row_per_fight(tmp, time_col=args.time_col)
    if all_df.empty:
        raise RuntimeError(
            "After collapsing to one row per fight, no rows remained. "
            "This usually means opponent ids/names are missing."
        )

    # Attach event metadata
    all_df = _attach_event_location(conn, all_df)
    all_df = _attach_event_status_and_upcoming(conn, all_df)

    # Normalize bout_id after collapse
    if "bout_id" in all_df.columns:
        b = all_df["bout_id"].astype(str).str.strip()
        b_missing = all_df["bout_id"].isna() | (b == "") | (b.str.lower() == "nan")
        if bool(b_missing.any()):
            if "fight_key" in all_df.columns:
                all_df.loc[b_missing, "bout_id"] = all_df.loc[b_missing, "fight_key"]
            else:
                all_df.loc[b_missing, "bout_id"] = all_df.loc[b_missing, "fight_id"]
    else:
        all_df["bout_id"] = all_df["fight_key"] if "fight_key" in all_df.columns else all_df["fight_id"]

    labeled_df = all_df[all_df[args.label_col].notna()].copy()
    if labeled_df.empty:
        raise RuntimeError(f"No labeled rows found in {args.view}; check {args.label_col} and upstream ETL")

    feature_cols = _pick_feature_columns(
        labeled_df,
        prefixes=args.feature_prefix,
        exclude=args.exclude_col + [args.label_col],
    )

    all_nan_cols = [c for c in feature_cols if labeled_df[c].isna().all()]
    if all_nan_cols:
        logger.warning(f"Dropping all-NaN feature columns: {all_nan_cols}")
        feature_cols = [c for c in feature_cols if c not in all_nan_cols]

    if not feature_cols:
        raise RuntimeError("No feature columns selected after dropping all-NaN columns.")

    # === Categorical features for CatBoost native encoding ===
    cat_cols = _pick_categorical_columns(labeled_df)
    # Add cat cols to feature list (they must be in feature_cols for indexing)
    for cc in cat_cols:
        if cc not in feature_cols:
            feature_cols.append(cc)
    num_cols = [c for c in feature_cols if c not in cat_cols]
    # Prepare categorical: fill NA with "missing", convert to str
    for cc in cat_cols:
        for frame in [labeled_df, all_df]:
            if cc in frame.columns:
                frame[cc] = frame[cc].fillna("missing").astype(str)
    cat_feature_indices = [feature_cols.index(cc) for cc in cat_cols]
    if cat_cols:
        logger.info(f"Categorical features ({len(cat_cols)}): {cat_cols} at indices {cat_feature_indices}")

    # Build append-only trainall dataset
    trainall_cols: list[str] = []
    for c in [
        "organization",
        "fight_key",
        "fight_id",
        "bout_id",
        args.time_col,
        args.label_col,
        "fighter_id",
        "fighter_name_display",
        "opponent_id",
        "opponent_name_display",
        "event_name",
        "event_url",
        "location",
        "weight_class",
        "is_title_fight",
    ]:
        if c in labeled_df.columns and c not in trainall_cols:
            trainall_cols.append(c)
    for c in feature_cols:
        if c in labeled_df.columns and c not in trainall_cols:
            trainall_cols.append(c)

    trainall_df = labeled_df[trainall_cols].copy()
    trainall_mode = "overwrite" if args.overwrite_trainall else "append"
    _write_trainall_dataset(conn, args.trainall_table, trainall_df, mode=trainall_mode)

    train_base_df = conn.execute(f"SELECT * FROM {args.trainall_table}").fetch_df()
    if train_base_df.empty:
        raise RuntimeError(f"No rows found in training dataset table {args.trainall_table}")

    train_base_df = _apply_training_filters(
        train_base_df,
        time_col=args.time_col,
        min_event_date=args.min_event_date,
        feature_cols=feature_cols,
        cat_cols=cat_cols,
        min_feature_coverage=args.min_feature_coverage,
    )
    if train_base_df.empty:
        raise RuntimeError("Training dataset is empty after applying --min-event-date/--min-feature-coverage")

    pos = int((train_base_df[args.label_col] == 1).sum())
    neg = int((train_base_df[args.label_col] == 0).sum())
    tot = pos + neg
    pos_rate = float(pos / tot) if tot else 0.0
    logger.info(f"Training label balance: pos={pos:,} neg={neg:,} pos_rate={pos_rate:.3f}")

    train_df, test_df = _time_split(train_base_df, time_col=args.time_col, test_frac=args.test_frac)
    train_sub_df, valid_df = _time_split(train_df, time_col=args.time_col, test_frac=0.15)

    # Sanitize feature columns: convert nullable dtypes so pd.NA → np.nan / "missing"
    def _sanitize_features(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        cat_cols = set()
        if cat_feature_indices:
            cat_cols = {feature_cols[i] for i in cat_feature_indices}
        for col in feature_cols:
            if col in cat_cols:
                df[col] = df[col].fillna("missing").astype(str)
            else:
                # Convert nullable integer/float (Int64, Float64) → standard float64
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(np.float64)
        return df

    train_sub_df = _sanitize_features(train_sub_df)
    valid_df = _sanitize_features(valid_df)
    test_df = _sanitize_features(test_df)
    all_df = _sanitize_features(all_df)

    # Drop train-split all-NaN numeric columns and constant categoricals
    drop_num = [c for c in num_cols if train_sub_df[c].isna().all()]
    drop_cat = [c for c in cat_cols if train_sub_df[c].nunique(dropna=True) <= 1]
    drop_cols = set(drop_num + drop_cat)
    if drop_cols:
        logger.warning(f"Dropping train-split degenerate features: {sorted(drop_cols)}")
        feature_cols = [c for c in feature_cols if c not in drop_cols]
        cat_cols = [c for c in cat_cols if c not in drop_cols]
        num_cols = [c for c in feature_cols if c not in cat_cols]
        cat_feature_indices = [feature_cols.index(cc) for cc in cat_cols]

    X_train = train_sub_df[feature_cols].to_numpy(dtype=object if cat_feature_indices else np.float32, copy=True)
    y_train = train_sub_df[args.label_col].to_numpy(dtype=np.int64, copy=True)
    X_valid = valid_df[feature_cols].to_numpy(dtype=object if cat_feature_indices else np.float32, copy=True)
    y_valid = valid_df[args.label_col].to_numpy(dtype=np.int64, copy=True)
    X_test = test_df[feature_cols].to_numpy(dtype=object if cat_feature_indices else np.float32, copy=True)
    y_test = test_df[args.label_col].to_numpy(dtype=np.int64, copy=True)

    X_all = all_df[feature_cols].to_numpy(dtype=object if cat_feature_indices else np.float32, copy=True)

    run_meta = {
        "view": args.view,
        "trainall_table": args.trainall_table,
        "label_col": args.label_col,
        "time_col": args.time_col,
        "test_frac": args.test_frac,
        "min_event_date": args.min_event_date,
        "min_feature_coverage": float(args.min_feature_coverage),
        "n_total": int(len(all_df)),
        "n_train": int(len(train_sub_df)),
        "n_valid": int(len(valid_df)),
        "n_test": int(len(test_df)),
        "n_features": int(len(feature_cols)),
        "feature_cols": feature_cols,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True))

    results: dict[str, dict] = {}

    # Artifacts
    logreg_path = out_dir / "logreg.joblib"
    cat_path = out_dir / "catboost.cbm"

    logreg_model = None
    cat_model = None

    def load_if_exists() -> None:
        nonlocal logreg_model, cat_model
        try:
            import joblib
            if logreg_path.exists():
                logreg_model = joblib.load(logreg_path)
        except Exception as e:
            logger.warning(f"Failed to load existing LogReg: {e}")

        try:
            if cat_path.exists():
                from catboost import CatBoostClassifier
                m = CatBoostClassifier()
                m.load_model(str(cat_path))
                cat_model = m
        except Exception as e:
            logger.warning(f"Failed to load existing CatBoost: {e}")

    if not args.rebuild:
        load_if_exists()

    # Prepare numeric-only arrays for LogReg (it can't handle categoricals)
    if cat_feature_indices:
        numeric_only_indices = sorted(i for i in range(len(feature_cols)) if i not in cat_feature_indices)
        # Replace pandas NA / NAType / None with np.nan before casting to float32
        def _to_float32(arr: np.ndarray) -> np.ndarray:
            col_slice = arr[:, numeric_only_indices]
            # Convert to pandas Series per-column to handle NAType properly
            result = np.empty(col_slice.shape, dtype=np.float32)
            for j in range(col_slice.shape[1]):
                result[:, j] = pd.array(col_slice[:, j], dtype="Float64").to_numpy(dtype=np.float32, na_value=np.nan)
            return result
        X_train_numeric = _to_float32(X_train)
        X_valid_numeric = _to_float32(X_valid)
        X_test_numeric = _to_float32(X_test)
    else:
        X_train_numeric = X_train
        X_valid_numeric = X_valid
        X_test_numeric = X_test

    # Train LogReg
    if args.model in ("logreg", "all"):
        if logreg_model is None or args.rebuild:
            logger.info("Training LogisticRegression...")
            proba_test, model, thr_logreg = train_logreg_df(
                train_sub_df,
                valid_df,
                test_df,
                label_col=args.label_col,
                num_cols=num_cols,
                cat_cols=cat_cols,
                seed=args.seed,
            )
            logreg_model = model
            m = _compute_metrics(y_test, proba_test)
            m.n_train = int(len(train_sub_df))
            results["logreg"] = asdict(m)

            import joblib
            joblib.dump(logreg_model, logreg_path)
            (out_dir / "logreg_metrics.json").write_text(json.dumps(results["logreg"], indent=2, sort_keys=True))
        else:
            logger.info(f"Loaded LogisticRegression from {logreg_path}")

    # Train CatBoost
    if args.model in ("cat", "all"):
        if cat_model is None or args.rebuild:
            # Decide device
            device = args.cat_device
            if device == "auto":
                device = "cuda" if _detect_catboost_gpu_available() else "cpu"

            logger.info(f"Training CatBoost (device={device})...")
            proba_test, model = train_catboost(
                X_train, y_train, X_valid, y_valid, X_test,
                cat_param_overrides,
                device=device,
                seed=args.seed,
                cat_feature_indices=cat_feature_indices if cat_feature_indices else None,
            )
            cat_model = model
            m = _compute_metrics(y_test, proba_test)
            m.n_train = int(len(train_sub_df))
            results["catboost"] = asdict(m)

            cat_model.save_model(str(cat_path))
            (out_dir / "catboost_metrics.json").write_text(json.dumps(results["catboost"], indent=2, sort_keys=True))
        else:
            logger.info(f"Loaded CatBoost from {cat_path}")

    if logreg_model is None or cat_model is None:
        raise RuntimeError("Missing trained models. Run with --model all and/or --rebuild.")
        # -----------------------------
    # Threshold tuning (VALID set)
    # This improves accuracy only (doesn't affect AUC/calibration)
    # -----------------------------
    p_valid_logreg = np.asarray(
        logreg_model.predict_proba(valid_df[num_cols + cat_cols])[:, 1],
        dtype=float,
    )
    if cat_feature_indices:
        from catboost import Pool as _PoolV
        p_valid_cat = np.asarray(cat_model.predict_proba(_PoolV(X_valid, cat_features=cat_feature_indices))[:, 1], dtype=float)
    else:
        p_valid_cat = np.asarray(cat_model.predict_proba(X_valid)[:, 1], dtype=float)

    w_cat, thr_ens, acc_valid_ens = best_weight_and_threshold_for_accuracy(
        y_valid, p_valid_cat, p_valid_logreg
    )
    w_c_tuned = float(w_cat)
    w_l_tuned = float(1.0 - w_cat)
    w_sum_tuned = (w_c_tuned + w_l_tuned)

    thr_logreg, acc_valid_logreg = best_threshold_for_accuracy(y_valid, p_valid_logreg)
    thr_cat, acc_valid_cat = best_threshold_for_accuracy(y_valid, p_valid_cat)
    # ---- TEST accuracy using tuned thresholds (not 0.50) ----
    p_test_logreg = np.asarray(
        logreg_model.predict_proba(test_df[num_cols + cat_cols])[:, 1],
        dtype=float,
    )
    if cat_feature_indices:
        p_test_cat = np.asarray(cat_model.predict_proba(_PoolV(X_test, cat_features=cat_feature_indices))[:, 1], dtype=float)
    else:
        p_test_cat = np.asarray(cat_model.predict_proba(X_test)[:, 1], dtype=float)
    p_test_ens = (w_c_tuned * p_test_cat + w_l_tuned * p_test_logreg) / (w_c_tuned + w_l_tuned)

    acc_test_logreg_thr = float(np.mean((p_test_logreg >= thr_logreg).astype(int) == y_test))
    acc_test_cat_thr    = float(np.mean((p_test_cat    >= thr_cat).astype(int) == y_test))
    acc_test_ens_thr    = float(np.mean((p_test_ens    >= thr_ens).astype(int) == y_test))

    logger.info(
        f"TEST accuracy w/ tuned thresholds: logreg={acc_test_logreg_thr:.4f} | "
        f"cat={acc_test_cat_thr:.4f} | ens={acc_test_ens_thr:.4f}"
    )


    logger.info(
        f"Thresholds (VALID acc): logreg t={thr_logreg:.3f} acc={acc_valid_logreg:.4f} | "
        f"cat t={thr_cat:.3f} acc={acc_valid_cat:.4f} | ens t={thr_ens:.3f} acc={acc_valid_ens:.4f}"
    )

    # Score ALL rows
    # LogReg uses numeric-only features (strip categoricals)
    try:
        p_logreg = np.asarray(
            logreg_model.predict_proba(all_df[num_cols + cat_cols])[:, 1],
            dtype=float,
        )
    except Exception as e:
        raise RuntimeError(f"Failed scoring LogisticRegression: {e}")

    try:
        if cat_feature_indices:
            from catboost import Pool as _Pool
            all_pool = _Pool(X_all, cat_features=cat_feature_indices)
            p_cat = np.asarray(cat_model.predict_proba(all_pool)[:, 1], dtype=float)
        else:
            p_cat = np.asarray(cat_model.predict_proba(X_all)[:, 1], dtype=float)
    except Exception as e:
        raise RuntimeError(f"Failed scoring CatBoost: {e}")

    w_c = float(w_c_tuned)
    w_l = float(w_l_tuned)
    if w_c < 0 or w_l < 0 or (w_c + w_l) <= 0:
        raise ValueError("Ensemble weights must be non-negative and sum to > 0")
    w_sum = w_c + w_l
    p_ens = (w_c * p_cat + w_l * p_logreg) / w_sum

    # Model-only probabilities
    p_cat_model = p_cat.copy()
    p_logreg_model = p_logreg.copy()
    p_ens_model = p_ens.copy()

    conf = _confidence_from_two_models(p_cat, p_logreg)

    # === Odds + implied probability ===
    if args.odds_source in ("fightodds", "bestfightodds"):
            event_dt = pd.to_datetime(all_df["event_date"], errors="coerce", utc=True).dt.tz_convert(None)
            today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
            unlabeled = all_df[args.label_col].isna() if args.label_col in all_df.columns else pd.Series(False, index=all_df.index)
            upcoming_mask = unlabeled | (event_dt >= today)

            odds_df = all_df.loc[upcoming_mask].copy()

            fighter_odds = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            opponent_odds = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            odds_single_book_cols: list[tuple[str, np.ndarray]] = []
            odds_source = args.odds_source

            if odds_df.empty:
                logger.info("No upcoming/unlabeled fights found; skipping odds fetch")
            else:
                used_source = args.odds_source
                try:
                    if args.odds_source == "bestfightodds":
                        f_sub, o_sub = _bfo_two_sided_odds(
                            df=odds_df,
                            promotion_filter=args.bfo_promotion_filter,
                            odds_format=args.odds_format,
                        )
                    else:
                        # legacy fightodds path (may fail if they went fully premium)
                        if args.fightodds_start_date:
                            start_date = args.fightodds_start_date
                        else:
                            dmin = pd.to_datetime(odds_df["event_date"], errors="coerce").min() if not odds_df.empty else pd.NaT
                            if pd.isna(dmin):
                                start_date = (today.date() - timedelta(days=7)).strftime("%Y-%m-%d")
                            else:
                                start_date = (pd.to_datetime(dmin).date() - timedelta(days=7)).strftime("%Y-%m-%d")
                        f_sub, o_sub = _fightodds_two_sided_odds(
                            df=odds_df,
                            promotion_slug=args.fightodds_promotion_slug,
                            sportsbooks=args.sportsbook,
                            odds_format=args.odds_format,
                            start_date=start_date,
                        )
                except Exception as e:
                    logger.warning(f"Primary odds fetch ({args.odds_source}) failed: {e}")
                    if args.odds_fallback == "oddsapi":
                        logger.info("Trying The Odds API fallback...")
                        f_sub, o_sub = _oddsapi_two_sided_odds(
                            df=odds_df,
                            api_key=str(args.the_odds_api_key or "").strip(),
                            sport_key=args.the_odds_api_sport_key,
                            regions=args.the_odds_api_regions,
                            odds_format=args.odds_format,
                        )
                        used_source = "oddsapi"
                    else:
                        raise

                pos = np.where(upcoming_mask.to_numpy(dtype=bool, copy=False))[0]
                fighter_odds[pos] = np.asarray(f_sub, dtype=float)
                opponent_odds[pos] = np.asarray(o_sub, dtype=float)
                odds_source = used_source

            implied_f_vals = [_implied_prob_from_odds_value(v, odds_format=args.odds_format) for v in fighter_odds.tolist()]
            implied_o_vals = [_implied_prob_from_odds_value(v, odds_format=args.odds_format) for v in opponent_odds.tolist()]
            implied_fighter = np.array([np.nan if v is None else float(v) for v in implied_f_vals], dtype=float)
            implied_opponent = np.array([np.nan if v is None else float(v) for v in implied_o_vals], dtype=float)
            odds_sportsbook = ",".join(args.sportsbook) if args.sportsbook else "bestfightodds"

    else:
        odds_col = args.odds_col or _detect_odds_col(all_df)
        if odds_col is None or odds_col not in all_df.columns:
            if odds_col is None:
                logger.warning("No odds column detected. betting_edge/recommended_bet will be NULL/False.")
            else:
                logger.warning(f"Requested odds column {odds_col!r} not found. betting_edge/recommended_bet NULL/False.")
            implied_fighter = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            implied_opponent = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            fighter_odds = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            opponent_odds = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            odds_source = "duckdb"
            odds_sportsbook = None
            odds_single_book_cols = []
        else:
            logger.info(f"Using odds column: {odds_col} (format={args.odds_format})")
            fighter_odds = pd.to_numeric(all_df[odds_col], errors="coerce").to_numpy(dtype=float, copy=True)
            implied_vals = [_implied_prob_from_odds_value(v, odds_format=args.odds_format) for v in all_df[odds_col].tolist()]
            implied_fighter = np.array([np.nan if v is None else float(v) for v in implied_vals], dtype=float)
            implied_opponent = np.where(np.isfinite(implied_fighter), 1.0 - implied_fighter, np.nan)
            opponent_odds = np.full(shape=len(all_df), fill_value=np.nan, dtype=float)
            odds_source = "duckdb"
            odds_sportsbook = None
            odds_single_book_cols = []

    # Market implied probability + blending
    market_p_fighter = _market_prob_from_implied(implied_fighter, implied_opponent)
    w_market = float(args.market_blend_weight)
    if w_market < 0.0 or w_market > 1.0:
        raise ValueError("--market-blend-weight must be in [0,1]")
    market_ok = np.isfinite(market_p_fighter)

    def _blend(p_model: np.ndarray) -> np.ndarray:
        p_out = np.asarray(p_model, dtype=float).copy()
        p_out = np.where(market_ok, w_market * p_out + (1.0 - w_market) * market_p_fighter, p_out)
        return np.clip(p_out, 1e-6, 1.0 - 1e-6)

    p_cat_odds = _blend(p_cat_model)
    p_logreg_odds = _blend(p_logreg_model)
    p_ens_odds = _blend(p_ens_model)

    def _betting_from_prob(p_fighter_in: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        p_f = np.clip(np.asarray(p_fighter_in, dtype=float), 1e-6, 1.0 - 1e-6)
        p_o = 1.0 - p_f

        e_f = p_f - implied_fighter
        e_o = p_o - implied_opponent
        e_f = np.where(np.isfinite(e_f), e_f, np.nan)
        e_o = np.where(np.isfinite(e_o), e_o, np.nan)

        both_ok = np.isfinite(e_f) & np.isfinite(e_o)
        one_ok = np.isfinite(e_f) & ~np.isfinite(e_o)
        bet_f = np.where(both_ok, e_f >= e_o, p_f >= 0.5)
        bet_f = np.where(one_ok, True, bet_f)
        bet_e = np.where(bet_f, e_f, e_o)

        s = _signal_strength(
            confidence=conf,
            edge=np.nan_to_num(bet_e, nan=-999.0),
            strong_min_agreement=float(args.strong_min_agreement),
            strong_min_edge=float(args.strong_min_edge),
            med_min_agreement=float(args.medium_min_agreement),
            med_min_edge=float(args.medium_min_edge),
        )
        rec = (s == "STRONG") & (bet_e >= float(args.strong_min_edge))
        rec = np.where(np.isfinite(bet_e), rec, False)
        return bet_f, bet_e, s.astype(str), rec.astype(bool)

    bet_on_fighter_model, bet_edge_model, strength_model, recommended_model = _betting_from_prob(p_ens_model)
    bet_on_fighter_odds, bet_edge_odds, strength_odds, recommended_odds = _betting_from_prob(p_ens_odds)
    # --- Per-model betting decisions (MODEL-only lane) ---
    bet_on_fighter_logreg_model, bet_edge_logreg_model, strength_logreg_model, recommended_logreg_model = _betting_from_prob(p_logreg_model)
    bet_on_fighter_cat_model, bet_edge_cat_model, strength_cat_model, recommended_cat_model = _betting_from_prob(p_cat_model)

    # --- Per-model betting decisions (ODDS-blended lane) ---
    bet_on_fighter_logreg_odds, bet_edge_logreg_odds, strength_logreg_odds, recommended_logreg_odds = _betting_from_prob(p_logreg_odds)
    bet_on_fighter_cat_odds, bet_edge_cat_odds, strength_cat_odds, recommended_cat_odds = _betting_from_prob(p_cat_odds)

    disagreement = np.abs(p_ens_model - market_p_fighter)
    guard_mask = disagreement > 0.25
    if np.any(guard_mask):
        logger.info(
            f"Guardrail: {np.sum(guard_mask)} fights have model/market disagreement > 0.25; suppressing recommended bet."
        )
    recommended_model = np.where(guard_mask, False, recommended_model)
    recommended_odds = np.where(guard_mask, False, recommended_odds)
    # for log and cat only
    recommended_logreg_model = np.where(guard_mask, False, recommended_logreg_model)
    recommended_cat_model    = np.where(guard_mask, False, recommended_cat_model)
    recommended_logreg_odds  = np.where(guard_mask, False, recommended_logreg_odds)
    recommended_cat_odds     = np.where(guard_mask, False, recommended_cat_odds)


    score_model = _signal_score(conf, np.nan_to_num(bet_edge_model, nan=0.0))
    score_odds = _signal_score(conf, np.nan_to_num(bet_edge_odds, nan=0.0))

    # Build scored dataframe (one row per fight)
    base_cols = [
        "organization",
        "fight_id",
        "event_date",
        "event_id",
        "event_name",
        "event_url",
        "location",
        "event_status",
        "is_upcoming",
        "days_until_event",
        "fighter_name",
        "opponent_name",
    ]
    base_cols = [c for c in base_cols if c in all_df.columns]

    if args.label_col in all_df.columns and args.label_col not in base_cols:
        base_cols.append(args.label_col)
    if "fighter_name_display" in all_df.columns:
        base_cols.insert(base_cols.index("fighter_name") + 1, "fighter_name_display")
    if "opponent_name_display" in all_df.columns:
        base_cols.insert(base_cols.index("opponent_name") + 1, "opponent_name_display")
    if "fighter_name_key" in all_df.columns:
        base_cols.append("fighter_name_key")
    if "opponent_name_key" in all_df.columns:
        base_cols.append("opponent_name_key")
    if "fight_key" in all_df.columns:
        base_cols.insert(2, "fight_key")
    if "bout_id" in all_df.columns:
        base_cols.insert(2, "bout_id")
    if "fighter_id" in all_df.columns:
        base_cols.insert(base_cols.index("fighter_name"), "fighter_id")
    if "opponent_id" in all_df.columns:
        base_cols.insert(base_cols.index("opponent_name"), "opponent_id")

    scores_df = all_df[base_cols].copy()

    # Use display names where available for primary outputs
    if "fighter_name_display" in scores_df.columns:
        f_disp = scores_df["fighter_name_display"].astype(str).str.strip()
        f_ok = scores_df["fighter_name_display"].notna() & (f_disp != "") & (f_disp.str.lower() != "nan")
        scores_df["fighter_name"] = np.where(f_ok, scores_df["fighter_name_display"], scores_df["fighter_name"])
    if "opponent_name_display" in scores_df.columns:
        o_disp = scores_df["opponent_name_display"].astype(str).str.strip()
        o_ok = scores_df["opponent_name_display"].notna() & (o_disp != "") & (o_disp.str.lower() != "nan")
        scores_df["opponent_name"] = np.where(o_ok, scores_df["opponent_name_display"], scores_df["opponent_name"])

    # Fight result outputs for labeled fights
    if args.label_col in scores_df.columns:
        y_num = pd.to_numeric(scores_df[args.label_col], errors="coerce")
        is_win = pd.Series((y_num == 1), index=scores_df.index).fillna(False).to_numpy(dtype=bool)
        is_loss = pd.Series((y_num == 0), index=scores_df.index).fillna(False).to_numpy(dtype=bool)

        scores_df["result"] = np.where(is_win, "win", np.where(is_loss, "loss", None))

        f_name_out = scores_df["fighter_name_display"] if "fighter_name_display" in scores_df.columns else scores_df["fighter_name"]
        o_name_out = scores_df["opponent_name_display"] if "opponent_name_display" in scores_df.columns else scores_df["opponent_name"]
        scores_df["winner_name_display"] = np.where(is_win, f_name_out, np.where(is_loss, o_name_out, None))
        scores_df["loser_name_display"] = np.where(is_win, o_name_out, np.where(is_loss, f_name_out, None))

        if "fighter_id" in scores_df.columns and "opponent_id" in scores_df.columns:
            scores_df["winner_id"] = np.where(is_win, scores_df["fighter_id"], np.where(is_loss, scores_df["opponent_id"], None))
            scores_df["loser_id"] = np.where(is_win, scores_df["opponent_id"], np.where(is_loss, scores_df["fighter_id"], None))

    # Ensure bout_id never blank
    if "bout_id" not in scores_df.columns:
        scores_df["bout_id"] = scores_df["fight_id"]
    else:
        b = scores_df["bout_id"].astype(str).str.strip()
        b_missing = scores_df["bout_id"].isna() | (b == "") | (b.str.lower() == "nan")
        if bool(b_missing.any()):
            scores_df.loc[b_missing, "bout_id"] = scores_df.loc[b_missing, "fight_id"]

    # === Parallel prediction outputs ===
    # MODEL (no odds influence)
    scores_df["catboost_fighter_win_prob_model"] = p_cat_model
    scores_df["catboost_opponent_win_prob_model"] = 1.0 - p_cat_model
    scores_df["logistic_fighter_win_prob_model"] = p_logreg_model
    scores_df["logistic_opponent_win_prob_model"] = 1.0 - p_logreg_model
    scores_df["ensemble_fighter_win_prob_model"] = p_ens_model
    scores_df["ensemble_opponent_win_prob_model"] = 1.0 - p_ens_model

    # Picks from tuned thresholds (accuracy-only)
    scores_df["logreg_pick_model"] = (p_logreg_model >= thr_logreg).astype(int)
    scores_df["catboost_pick_model"] = (p_cat_model >= thr_cat).astype(int)
    scores_df["ensemble_pick_model"] = (p_ens_model >= thr_ens).astype(int)

    scores_df["logreg_pick_threshold"] = float(thr_logreg)
    scores_df["catboost_pick_threshold"] = float(thr_cat)
    scores_df["ensemble_pick_threshold"] = float(thr_ens)


    # ODDS (market-blended where market implied exists)
    scores_df["catboost_fighter_win_prob_odds"] = p_cat_odds
    scores_df["catboost_opponent_win_prob_odds"] = 1.0 - p_cat_odds
    scores_df["logistic_fighter_win_prob_odds"] = p_logreg_odds
    scores_df["logistic_opponent_win_prob_odds"] = 1.0 - p_logreg_odds
    scores_df["ensemble_fighter_win_prob_odds"] = p_ens_odds
    scores_df["ensemble_opponent_win_prob_odds"] = 1.0 - p_ens_odds

    # Backwards-compatible aliases
    scores_df["catboost_fighter_win_prob"] = p_cat_model
    scores_df["logistic_fighter_win_prob"] = p_logreg_model
    scores_df["ensemble_model_fighter_win_prob"] = p_ens_model
    scores_df["ensemble_model_opponent_win_prob"] = 1.0 - p_ens_model
    scores_df["ensemble_fighter_win_prob"] = p_ens_odds
    scores_df["ensemble_opponent_win_prob"] = 1.0 - p_ens_odds

    scores_df["market_implied_fighter_prob"] = market_p_fighter
    scores_df["market_blend_weight"] = float(args.market_blend_weight)
    scores_df["confidence_level"] = conf

    scores_df["odds_source"] = odds_source
    scores_df["odds_sportsbook"] = odds_sportsbook
    scores_df["fighter_odds"] = fighter_odds
    scores_df["opponent_odds"] = opponent_odds
    scores_df["fighter_implied_prob"] = implied_fighter
    scores_df["opponent_implied_prob"] = implied_opponent

    # Betting (two lanes)
    scores_df["betting_edge_model"] = bet_edge_model
    scores_df["betting_edge_odds"] = bet_edge_odds
    scores_df["recommended_bet_model"] = recommended_model
    scores_df["recommended_bet_odds"] = recommended_odds
    scores_df["bet_on_model"] = np.where(bet_on_fighter_model, "fighter", "opponent")

        # --- LogReg betting outputs ---
    scores_df["bet_on_logreg_model"] = np.where(bet_on_fighter_logreg_model, "fighter", "opponent")
    scores_df["betting_edge_logreg_model"] = bet_edge_logreg_model
    scores_df["signal_strength_logreg_model"] = strength_logreg_model
    scores_df["recommended_bet_logreg_model"] = recommended_logreg_model

    scores_df["bet_on_logreg_odds"] = np.where(bet_on_fighter_logreg_odds, "fighter", "opponent")
    scores_df["betting_edge_logreg_odds"] = bet_edge_logreg_odds
    scores_df["signal_strength_logreg_odds"] = strength_logreg_odds
    scores_df["recommended_bet_logreg_odds"] = recommended_logreg_odds

    # --- CatBoost betting outputs ---
    scores_df["bet_on_catboost_model"] = np.where(bet_on_fighter_cat_model, "fighter", "opponent")
    scores_df["betting_edge_catboost_model"] = bet_edge_cat_model
    scores_df["signal_strength_catboost_model"] = strength_cat_model
    scores_df["recommended_bet_catboost_model"] = recommended_cat_model

    scores_df["bet_on_catboost_odds"] = np.where(bet_on_fighter_cat_odds, "fighter", "opponent")
    scores_df["betting_edge_catboost_odds"] = bet_edge_cat_odds
    scores_df["signal_strength_catboost_odds"] = strength_cat_odds
    scores_df["recommended_bet_catboost_odds"] = recommended_cat_odds


    scores_df["signal_strength_model"] = strength_model
    scores_df["signal_strength_odds"] = strength_odds
    scores_df["signal_score_model"] = score_model
    scores_df["signal_score_odds"] = score_odds

    # Explicit per-model picked fighter names (for fast downstream parquet/frontend reads)
    fighter_name_out = scores_df["fighter_name_display"] if "fighter_name_display" in scores_df.columns else scores_df["fighter_name"]
    opponent_name_out = scores_df["opponent_name_display"] if "opponent_name_display" in scores_df.columns else scores_df["opponent_name"]
    scores_df["bet_on_name_logreg_model"] = np.where(
        bet_on_fighter_logreg_model, fighter_name_out, opponent_name_out
    )
    scores_df["bet_on_name_catboost_model"] = np.where(
        bet_on_fighter_cat_model, fighter_name_out, opponent_name_out
    )
    scores_df["bet_on_name_model"] = np.where(
        bet_on_fighter_model, fighter_name_out, opponent_name_out
    )

    # Backwards-compatible betting outputs (odds-influenced)
    scores_df["bet_on"] = np.where(bet_on_fighter_odds, "fighter", "opponent")
    scores_df["bet_on_name"] = np.where(bet_on_fighter_odds, fighter_name_out, opponent_name_out)
    scores_df["bet_on_odds"] = np.where(bet_on_fighter_odds, scores_df["fighter_odds"], scores_df["opponent_odds"])
    scores_df["bet_on_implied_prob"] = np.where(
        bet_on_fighter_odds, scores_df["fighter_implied_prob"], scores_df["opponent_implied_prob"]
    )
    scores_df["betting_edge"] = bet_edge_odds
    scores_df["signal_strength"] = strength_odds.astype(str)
    scores_df["signal_score"] = score_odds
    scores_df["recommended_bet"] = recommended_odds.astype(bool)
    scores_df["recommended_bet_on"] = scores_df["bet_on_name"].astype(str)

    # Drop redundant name columns if display columns exist
    if "fighter_name_display" in scores_df.columns:
        scores_df = scores_df.drop(columns=["fighter_name"], errors="ignore")
    if "opponent_name_display" in scores_df.columns:
        scores_df = scores_df.drop(columns=["opponent_name"], errors="ignore")

    # Single-book odds columns (if requested)
    for col_name, col_vals in odds_single_book_cols:
        scores_df[col_name] = col_vals

    # Persist metrics summary
    (out_dir / "summary_metrics.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    logger.info(f"Wrote artifacts to {out_dir}")
    if results:
        logger.info(f"Summary metrics: {json.dumps(results, indent=2, sort_keys=True)}")

    # Add ensemble metrics on labeled test window
    try:
        p_test_logreg = np.asarray(
            logreg_model.predict_proba(test_df[num_cols + cat_cols])[:, 1],
            dtype=float,
        )
        if cat_feature_indices:
            from catboost import Pool as _PoolT
            p_test_cat = np.asarray(cat_model.predict_proba(_PoolT(X_test, cat_features=cat_feature_indices))[:, 1], dtype=float)
        else:
            p_test_cat = np.asarray(cat_model.predict_proba(X_test)[:, 1], dtype=float)
        p_test_model = (w_c * p_test_cat + w_l * p_test_logreg) / w_sum
        m_ens = _compute_metrics(y_test, p_test_model)
        m_ens.n_train = int(len(train_sub_df))
        results["ensemble_model"] = asdict(m_ens)

        market_series = pd.Series(market_p_fighter, index=all_df.index)
        market_test = market_series.reindex(test_df.index).to_numpy(dtype=float, copy=True)
        ok = np.isfinite(market_test)
        p_test_blend = p_test_model.copy()
        p_test_blend = np.where(ok, w_market * p_test_model + (1.0 - w_market) * market_test, p_test_blend)
        p_test_blend = np.clip(p_test_blend, 1e-6, 1.0 - 1e-6)

        m_blend = _compute_metrics(y_test, p_test_blend)
        m_blend.n_train = int(len(train_sub_df))
        out_blend = asdict(m_blend)
        out_blend["market_coverage_frac"] = float(np.mean(ok)) if len(ok) else 0.0
        results["ensemble_market_blend"] = out_blend
    except Exception as e:
        logger.warning(f"Failed computing ensemble metrics: {e}")

    (out_dir / "summary_metrics.json").write_text(json.dumps(results, indent=2, sort_keys=True))


    
        # -----------------------------
    # Write scored outputs back to DuckDB
    # -----------------------------
    def _upcoming_mask(df: pd.DataFrame, *, label_col: str) -> pd.Series:
        if "is_upcoming" in df.columns:
            return df["is_upcoming"].fillna(False).astype(bool)

        if "event_status" in df.columns and "event_date" in df.columns:
            status = df["event_status"].astype(str).str.strip().str.lower()
            is_up = status.eq("upcoming")
            event_dt = pd.to_datetime(df["event_date"], errors="coerce", utc=True).dt.tz_convert(None)
            today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
            is_future = event_dt.dt.normalize() >= today
            return (is_up.fillna(False) | is_future.fillna(False)).astype(bool)

        if label_col in df.columns:
            return df[label_col].isna()

        return pd.Series(False, index=df.index)

    # Decide write mode (overwrite/append/only_upcoming)
    if getattr(args, "only_upcoming", False):
        write_mode = "only_upcoming"
    elif getattr(args, "append", False):
        write_mode = "append"
    else:
        write_mode = "overwrite"

    if write_mode == "only_upcoming":
        mask = _upcoming_mask(scores_df, label_col=str(args.label_col))
        upcoming_df = scores_df.loc[mask].copy()

        recent_days = int(getattr(args, "recent_completed_refresh_days", 21) or 21)
        completed_df = scores_df.loc[~mask].copy()
        if not completed_df.empty and "event_date" in completed_df.columns:
            event_dt = pd.to_datetime(completed_df["event_date"], errors="coerce", utc=True).dt.tz_convert(None)
            cutoff = pd.Timestamp.now("UTC").tz_localize(None).normalize() - pd.Timedelta(days=recent_days)
            completed_df = completed_df.loc[event_dt >= cutoff].copy()

        if not completed_df.empty and "result" in completed_df.columns:
            result_series = completed_df["result"]
            completed_df = completed_df.loc[result_series.notna()].copy()
            if not completed_df.empty:
                result_txt = completed_df["result"].astype(str).str.strip().str.lower()
                completed_df = completed_df.loc[~result_txt.isin({"", "nan", "nat", "none"})].copy()

        refresh_df = pd.concat([upcoming_df, completed_df], ignore_index=True)

        # Pick a stable key column for matching DELETE/INSERT
        key_col = "fight_id"
        for c in ("fight_key", "bout_id", "fight_id"):
            if c not in refresh_df.columns:
                continue
            s = refresh_df[c]
            try:
                ok_any = bool(
                    (s.notna() & (s.astype(str).str.strip() != "") & (s.astype(str).str.lower() != "nan")).any()
                )
            except Exception:
                ok_any = True
            if ok_any:
                key_col = c
                break

        # Drop rows missing required keys
        if "organization" in refresh_df.columns and key_col in refresh_df.columns:
            k = refresh_df[key_col].astype(str).str.strip()
            ok = refresh_df["organization"].notna() & (k != "") & (k.str.lower() != "nan")
            refresh_df = refresh_df.loc[ok].copy()

        logger.info(
            f"only-upcoming: refreshing rows={len(refresh_df):,} "
            f"(upcoming={len(upcoming_df):,}, recent_completed={len(completed_df):,}, "
            f"window_days={recent_days}, key_col={key_col}) within {args.scored_table}"
        )

        # If scored table doesn't exist, bootstrap it with full scores_df first
        _write_scores_back(
            conn,
            base_name=args.view,
            scored_table=args.scored_table,
            scores_df=refresh_df,
            mode="only_upcoming",
            bootstrap_full_df=scores_df,
        )
    # after writing scores (overwrite/append/only_upcoming)



    else:
        logger.info(f"Writing scored table using mode={write_mode!r} -> {args.scored_table}")
        _write_scores_back(
            conn,
            base_name=args.view,
            scored_table=args.scored_table,
            scores_df=scores_df,
            mode=write_mode,
        )
    _write_metrics_to_duckdb(
        conn,
        metrics_table="gold.model_metrics",
        target=args.target,
        results=results,
    )

    # -----------------------------
    # Create/replace upcoming-only VIEW
    # -----------------------------
    try:
        conn.execute(
            f"""
            CREATE OR REPLACE VIEW {args.scored_upcoming_view} AS
            SELECT *
            FROM {args.scored_table}
            WHERE COALESCE(is_upcoming, FALSE) = TRUE
            """
        )
        logger.info(f"Created/updated upcoming-only VIEW: {args.scored_upcoming_view}")
    except Exception as e:
        logger.warning(f"Failed to create upcoming-only VIEW {args.scored_upcoming_view}: {e}")

    conn.close()
    logger.info("Done ✅")


if __name__ == "__main__":
    main()
