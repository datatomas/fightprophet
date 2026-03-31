#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gold helper: tune CatBoost hyperparameters and persist best params into DuckDB.

Design goals
- Manual/occasional script (not part of daily cron runs unless you want)
- Writes best hyperparameters into `gold.best_params_catboost` as JSON
- Keeps the main trainer/scorer stable; it can later read from this table

Typical usage
  source bootstrap_scripts/envloader.sh && \
    python3 -m ml_kuda_sports_lab.etl.gold.mma_catboost_tune --target dev --n-trials 40 --seed 42

Notes
- Uses a time-based split on `--time-col`.
- Uses a simple random search (no Optuna/Hyperopt).
- Uses CatBoost early stopping via od_type/od_wait.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd

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
    p = argparse.ArgumentParser(description="Tune CatBoost hyperparameters and store best params in DuckDB")
    p.add_argument("--duckdb-path", help="Path to the DuckDB file")
    p.add_argument("--target", choices=["dev", "prod"], default="dev")

    p.add_argument(
        "--trainall-table",
        default="gold.trainall_prefight_features",
        help="Source one-row-per-fight training table (append-only) produced by the gold trainer",
    )
    p.add_argument("--label-col", default="y_win")
    p.add_argument("--time-col", default="event_date")

    p.add_argument(
        "--feature-prefix",
        action="append",
        default=["delta_", "prof_", "opp_prof_", "prof_missing", "opp_prof_missing"],
        help="Keep columns whose name starts with this prefix (repeatable)",
    )
    p.add_argument(
        "--exclude-col",
        action="append",
        default=["y_win"],
        help="Explicitly exclude a column (repeatable)",
    )

    p.add_argument("--test-frac", type=float, default=0.2, help="Most-recent fraction held out as test")
    p.add_argument("--valid-frac", type=float, default=0.15, help="Most-recent fraction of train used as validation")
    p.add_argument(
        "--min-event-date",
        default=None,
        help="Optional YYYY-MM-DD; drop training rows before this date",
    )
    p.add_argument(
        "--min-feature-coverage",
        type=float,
        default=0.0,
        help="Drop training rows whose non-null feature coverage is below this fraction (0-1)",
    )

    p.add_argument("--metric", choices=["auc", "logloss", "accuracy"], default="auc")
    p.add_argument("--n-trials", type=int, default=40)
    p.add_argument("--seed", type=int, default=42)

    p.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="CatBoost device; auto tries GPU if available else CPU",
    )

    p.add_argument(
        "--params-table",
        default="gold.best_params_catboost",
        help="DuckDB table to store tuned hyperparameters (JSON)",
    )
    p.add_argument(
        "--model-name",
        default="catboost",
        help="Model name key stored in gold.best_params_catboost (e.g., 'catboost')",
    )
    p.add_argument(
        "--overwrite-latest",
        action="store_true",
        default=False,
        help="Delete existing rows for (target, model_name) before inserting this run",
    )

    return p.parse_args()


def _pick_feature_columns(df: pd.DataFrame, prefixes: Iterable[str], exclude: Iterable[str]) -> list[str]:
    exclude_set = set(exclude)
    keep: list[str] = []
    for col in df.columns:
        if col in exclude_set:
            continue
        for pfx in prefixes:
            if col == pfx or col.startswith(pfx):
                keep.append(col)
                break
    return [c for c in keep if pd.api.types.is_numeric_dtype(df[c])]


# Known categorical columns that CatBoost can handle natively.
_KNOWN_CAT_COLS: list[str] = [
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
    allowed: list[str] | None = None,
    max_cardinality: int = 60,
) -> list[str]:
    candidates = allowed if allowed is not None else list(_KNOWN_CAT_COLS)
    result: list[str] = []
    for c in candidates:
        if c not in df.columns:
            continue
        nuniq = df[c].nunique(dropna=True)
        if nuniq < 1 or nuniq > max_cardinality:
            continue
        result.append(c)
    return result


def _time_split(df: pd.DataFrame, time_col: str, frac_recent: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    d = d.sort_values(time_col, ascending=True)
    n = len(d)
    if n < 200:
        logger.warning(f"Only {n} rows available; tuning will be noisy")
    cut = int(np.floor((1.0 - float(frac_recent)) * n))
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


def _score_metric(metric: str, y_true: np.ndarray, y_proba: np.ndarray) -> float:
    metric = (metric or "auc").lower()
    if metric == "auc":
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y_true, y_proba))
    if metric == "logloss":
        from sklearn.metrics import log_loss
        return float(log_loss(y_true, np.clip(y_proba, 1e-6, 1 - 1e-6)))
    if metric == "accuracy":
        from sklearn.metrics import accuracy_score
        y_pred = (np.asarray(y_proba) >= 0.5).astype(int)
        return float(accuracy_score(y_true, y_pred))
    raise ValueError(f"Unknown metric: {metric}")


def _better(metric: str, a: float, b: float) -> bool:
    metric = (metric or "auc").lower()
    if metric == "logloss":
        return a < b
    return a > b


def _detect_gpu_available() -> bool:
    try:
        from catboost.utils import get_gpu_device_count
        return int(get_gpu_device_count()) > 0
    except Exception:
        return False


def _random_params(rng, device: str) -> dict:
    depth = int(rng.integers(4, 11))
    learning_rate = float(rng.choice([0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.1, 0.12]))
    iterations = int(rng.choice([600, 800, 1200, 1600, 2000, 2500, 3000]))

    l2_leaf_reg = float(rng.choice([0.5, 1.0, 3.0, 5.0, 7.0, 9.0, 12.0, 15.0, 20.0]))
    random_strength = float(rng.choice([0.5, 1.0, 3.0, 5.0, 8.0, 10.0, 15.0]))

    bootstrap_type = str(rng.choice(["Bayesian", "Bernoulli", "MVS"]))

    params = {
        "depth": depth,
        "learning_rate": learning_rate,
        "iterations": iterations,
        "l2_leaf_reg": l2_leaf_reg,
        "random_strength": random_strength,
        "bootstrap_type": bootstrap_type,
        "min_data_in_leaf": int(rng.choice([1, 3, 5, 10, 20, 30, 50, 80])),
        "border_count": int(rng.choice([64, 128, 200, 254])),
        "od_wait": int(rng.choice([30, 50, 80, 100, 150])),
    }

    # Grow policy (SymmetricTree is default, Lossguide needs max_leaves)
    grow_policy = str(rng.choice(["SymmetricTree", "SymmetricTree", "Depthwise", "Lossguide"]))
    params["grow_policy"] = grow_policy
    if grow_policy == "Lossguide":
        params["max_leaves"] = int(rng.choice([16, 31, 48, 64, 96, 128]))

    # GPU-safe: only sample rsm on CPU
    if device != "cuda":
        params["rsm"] = float(rng.choice([0.5, 0.6, 0.7, 0.8, 0.9, 1.0]))

    if bootstrap_type == "Bayesian":
        params["bagging_temperature"] = float(rng.choice([0.0, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0]))
    else:
        params["subsample"] = float(rng.choice([0.5, 0.6, 0.7, 0.8, 0.9, 1.0]))

    # Class imbalance handling (randomly try different strategies)
    class_weight_strategy = str(rng.choice(["none", "none", "balanced", "sqrt"]))
    if class_weight_strategy == "balanced":
        params["auto_class_weights"] = "Balanced"
    elif class_weight_strategy == "sqrt":
        params["auto_class_weights"] = "SqrtBalanced"
    # else: keep default scale_pos_weight from caller

    return params


@dataclass
class TuneResult:
    metric: str
    score_valid: float
    score_test: float
    params: dict
    n_train: int
    n_valid: int
    n_test: int
    feature_cols: list[str]

def _train_eval_catboost(
    *,
    params: dict,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_valid: np.ndarray,
    y_valid: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    metric: str,
    device: str,
    seed: int,
    cat_feature_indices: list[int] | None = None,
) -> TuneResult:
    from catboost import CatBoostClassifier, Pool

    metric = (metric or "auc").lower()

    # Always train on the requested device
    task_type = "GPU" if device == "cuda" else "CPU"

    # GPU cannot use AUC eval_metric -> use Logloss for early stopping
    if task_type == "GPU" and metric == "auc":
        eval_metric = "Logloss"
    else:
        if metric == "auc":
            eval_metric = "AUC"
        elif metric == "accuracy":
            eval_metric = "Accuracy"
        else:
            eval_metric = "Logloss"

    loss_function = "Logloss"

    pos = int((y_train == 1).sum())
    neg = int((y_train == 0).sum())
    scale_pos_weight = float(neg / pos) if pos > 0 else 1.0

    # If auto_class_weights is set, don't also set scale_pos_weight
    use_auto_cw = "auto_class_weights" in params

    base = {
        "loss_function": loss_function,
        "eval_metric": eval_metric,
        "random_seed": int(seed),
        "verbose": False,
        "allow_writing_files": False,
        "task_type": task_type,
        # Early stopping:
        "od_type": "Iter",
        "od_wait": params.pop("od_wait", 50),
        "use_best_model": True,
    }

    if not use_auto_cw:
        base["scale_pos_weight"] = scale_pos_weight

    if task_type == "GPU" and "rsm" in params:
        params.pop("rsm", None)
        logger.info("Removed rsm for GPU; not supported for classification on GPU")

    clf = CatBoostClassifier(**base, **params)
    if cat_feature_indices:
        train_pool = Pool(X_train, label=y_train, cat_features=cat_feature_indices)
        valid_pool = Pool(X_valid, label=y_valid, cat_features=cat_feature_indices)
        clf.fit(train_pool, eval_set=valid_pool)
        test_pool = Pool(X_test, cat_features=cat_feature_indices)
        p_valid = clf.predict_proba(valid_pool)[:, 1]
        p_test = clf.predict_proba(test_pool)[:, 1]
    else:
        clf.fit(X_train, y_train, eval_set=(X_valid, y_valid))
        p_valid = clf.predict_proba(X_valid)[:, 1]
        p_test = clf.predict_proba(X_test)[:, 1]

    # You still score AUC using sklearn (good)
    score_valid = _score_metric(metric, y_valid, p_valid)
    score_test = _score_metric(metric, y_test, p_test)

    return TuneResult(
        metric=metric,
        score_valid=float(score_valid),
        score_test=float(score_test),
        params=params,
        n_train=int(len(y_train)),
        n_valid=int(len(y_valid)),
        n_test=int(len(y_test)),
        feature_cols=[],
    )



def _trial_worker(kwargs: dict, result_queue: multiprocessing.Queue) -> None:
    """Run a single CatBoost trial in an isolated subprocess.

    Each subprocess gets its own CUDA context, preventing C++ GPU state
    corruption between sequential trials (CatBoost 1.2.x on Blackwell GPUs).
    """
    try:
        r = _train_eval_catboost(**kwargs)
        result_queue.put(("ok", r))
    except Exception as e:
        result_queue.put(("err", str(e)))


def _run_trial_in_subprocess(kwargs: dict, timeout: int = 600) -> TuneResult:
    """Spawn a fresh process for a single trial and collect the result."""
    ctx = multiprocessing.get_context("spawn")
    q: multiprocessing.Queue = ctx.Queue()
    p = ctx.Process(target=_trial_worker, args=(kwargs, q))
    p.start()
    p.join(timeout=timeout)

    if p.exitcode is None:
        p.kill()
        p.join()
        raise RuntimeError("Trial subprocess timed out")
    if p.exitcode != 0:
        raise RuntimeError(f"Trial subprocess crashed (exit {p.exitcode})")

    tag, payload = q.get_nowait()
    if tag == "err":
        raise RuntimeError(payload)
    return payload


def _ensure_best_params_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            created_at TIMESTAMP,
            target VARCHAR,
            model_name VARCHAR,
            source_table VARCHAR,
            label_col VARCHAR,
            time_col VARCHAR,
            metric VARCHAR,
            score_valid DOUBLE,
            score_test DOUBLE,
            n_train BIGINT,
            n_valid BIGINT,
            n_test BIGINT,
            feature_prefixes_json VARCHAR,
            exclude_cols_json VARCHAR,
            feature_cols_json VARCHAR,
            params_json VARCHAR
        );
        """
    )


def main() -> None:
    args = parse_args()

    db_path = resolve_duckdb_path(args)
    logger.info(f"Using DuckDB at {db_path}")

    conn = duckdb.connect(db_path)
    conn.execute("PRAGMA enable_progress_bar = FALSE")

    try:
        base_df = conn.execute(f"SELECT * FROM {args.trainall_table}").fetch_df()
    except Exception as e:
        raise RuntimeError(f"Failed to read {args.trainall_table}: {e}")

    if base_df.empty:
        raise RuntimeError(f"No rows found in {args.trainall_table}")

    if args.label_col not in base_df.columns:
        raise RuntimeError(f"Label column {args.label_col!r} not found in {args.trainall_table}")

    base_df = base_df[base_df[args.label_col].notna()].copy()
    if base_df.empty:
        raise RuntimeError("No labeled rows available for tuning")

    feature_cols = _pick_feature_columns(
        base_df,
        prefixes=args.feature_prefix,
        exclude=args.exclude_col + [args.label_col],
    )
    if not feature_cols:
        raise RuntimeError("No feature columns selected. Adjust --feature-prefix.")

    cat_cols = _pick_categorical_columns(base_df)
    for cc in cat_cols:
        if cc not in feature_cols:
            feature_cols.append(cc)
    num_cols = [c for c in feature_cols if c not in cat_cols]

    base_df = _apply_training_filters(
        base_df,
        time_col=args.time_col,
        min_event_date=args.min_event_date,
        feature_cols=feature_cols,
        cat_cols=cat_cols,
        min_feature_coverage=args.min_feature_coverage,
    )
    if base_df.empty:
        raise RuntimeError("Training dataset is empty after applying --min-event-date/--min-feature-coverage")

    pos = int((base_df[args.label_col] == 1).sum())
    neg = int((base_df[args.label_col] == 0).sum())
    tot = pos + neg
    pos_rate = float(pos / tot) if tot else 0.0
    logger.info(f"Training label balance: pos={pos:,} neg={neg:,} pos_rate={pos_rate:.3f}")

    train_df, test_df = _time_split(base_df, time_col=args.time_col, frac_recent=args.test_frac)
    train_sub_df, valid_df = _time_split(train_df, time_col=args.time_col, frac_recent=args.valid_frac)

    def _sanitize_features(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in feature_cols:
            if col in cat_cols:
                df[col] = df[col].fillna("missing").astype(str)
            else:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(np.float64)
        return df

    train_sub_df = _sanitize_features(train_sub_df)
    valid_df = _sanitize_features(valid_df)
    test_df = _sanitize_features(test_df)

    # Drop train-split degenerate columns
    drop_num = [c for c in num_cols if train_sub_df[c].isna().all()]
    drop_cat = [c for c in cat_cols if train_sub_df[c].nunique(dropna=True) <= 1]
    drop_cols = set(drop_num + drop_cat)
    if drop_cols:
        logger.warning(f"Dropping train-split degenerate features: {sorted(drop_cols)}")
        feature_cols = [c for c in feature_cols if c not in drop_cols]
        cat_cols = [c for c in cat_cols if c not in drop_cols]
        num_cols = [c for c in feature_cols if c not in cat_cols]

    if cat_cols:
        X_train = train_sub_df[feature_cols].to_numpy(dtype=object, copy=True)
        X_valid = valid_df[feature_cols].to_numpy(dtype=object, copy=True)
        X_test = test_df[feature_cols].to_numpy(dtype=object, copy=True)
    else:
        X_train = train_sub_df[feature_cols].to_numpy(dtype=np.float32, copy=True)
        X_valid = valid_df[feature_cols].to_numpy(dtype=np.float32, copy=True)
        X_test = test_df[feature_cols].to_numpy(dtype=np.float32, copy=True)

    y_train = train_sub_df[args.label_col].to_numpy(dtype=np.int64, copy=True)
    y_valid = valid_df[args.label_col].to_numpy(dtype=np.int64, copy=True)
    y_test = test_df[args.label_col].to_numpy(dtype=np.int64, copy=True)

    metric = (args.metric or "auc").lower()

    # Decide device
    device = args.device
    if device == "auto":
        device = "cuda" if _detect_gpu_available() else "cpu"

    rng = np.random.default_rng(int(args.seed))
    best: TuneResult | None = None

    # GPU trials run in isolated subprocesses to prevent CUDA context corruption
    use_subprocess = device == "cuda"
    if use_subprocess:
        logger.info("GPU mode: each trial will run in an isolated subprocess")

    for i in range(int(args.n_trials)):
        params = _random_params(rng, device=device)

        trial_kwargs = dict(
            params=params,
            X_train=X_train,
            y_train=y_train,
            X_valid=X_valid,
            y_valid=y_valid,
            X_test=X_test,
            y_test=y_test,
            metric=metric,
            device=device,
            seed=int(args.seed),
            cat_feature_indices=[feature_cols.index(c) for c in cat_cols] if cat_cols else None,
        )

        try:
            if use_subprocess:
                r = _run_trial_in_subprocess(trial_kwargs)
            else:
                r = _train_eval_catboost(**trial_kwargs)
        except Exception as e:
            logger.warning(f"Trial {i+1}/{args.n_trials} failed: {e}")
            continue

        if best is None or _better(metric, r.score_valid, best.score_valid):
            best = r
            logger.info(
                f"Best so far @ trial {i+1}/{args.n_trials}: valid_{metric}={best.score_valid:.6f} test_{metric}={best.score_test:.6f}"
            )

    if best is None:
        raise RuntimeError("All tuning trials failed")

    best.feature_cols = feature_cols

    _ensure_best_params_table(conn, args.params_table)

    if args.overwrite_latest:
        conn.execute(
            f"DELETE FROM {args.params_table} WHERE target = ? AND model_name = ?",
            [args.target, args.model_name],
        )

    payload = {
        "params": best.params,
        "metric": best.metric,
        "score_valid": best.score_valid,
        "score_test": best.score_test,
        "n_train": best.n_train,
        "n_valid": best.n_valid,
        "n_test": best.n_test,
        "feature_cols": feature_cols,
        "feature_prefixes": list(args.feature_prefix),
        "exclude_cols": list(args.exclude_col),
        "min_event_date": args.min_event_date,
        "min_feature_coverage": float(args.min_feature_coverage),
        "device": device,
    }

    conn.execute(
        f"""
        INSERT INTO {args.params_table} (
            created_at, target, model_name, source_table, label_col, time_col,
            metric, score_valid, score_test, n_train, n_valid, n_test,
            feature_prefixes_json, exclude_cols_json, feature_cols_json, params_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            datetime.now(timezone.utc),
            args.target,
            args.model_name,
            args.trainall_table,
            args.label_col,
            args.time_col,
            metric,
            float(best.score_valid),
            float(best.score_test),
            int(best.n_train),
            int(best.n_valid),
            int(best.n_test),
            json.dumps(list(args.feature_prefix)),
            json.dumps(list(args.exclude_col)),
            json.dumps(feature_cols),
            json.dumps(best.params),
        ],
    )

    conn.close()
    logger.info("Wrote best CatBoost params to DuckDB")
    logger.info(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
