# Fight Prophet (Open-Source Module)

**Fight Prophet** is a UFC/MMA prediction platform.  
This repository currently open-sources a **limited module** of the system: ranking logic, feature engineering, and model training components.

> This is **not** the full production codebase.

---

## Why Contribute?

MMA prediction is a hard problem. The models in this repo improve when more people stress-test the feature engineering, experiment with alternative architectures, and surface edge cases the current pipeline misses.

If you improve the ranking logic or find a better modeling approach here, that improvement flows into the public module — and you get credited for it. The scrapers, orchestration, and production data stay private, but the science is open.

Good contributions we're looking for:
- Better feature engineering ideas (fighter styles, matchup dynamics, late-notice effects)
- Alternative model architectures or ensemble strategies
- Diagnostic tooling and evaluation improvements
- Documentation and methodology corrections

See [LICENSE.md](LICENSE.md) for terms — personal, non-commercial, and evaluation use is freely allowed.

---

## Open-Source Scope (Current)

This repo currently includes and maintains the following files, automatically synced via GitHub Actions on every push to `main`:

| Layer | File | Purpose |
|---|---|---|
| Silver | `etl/silver/mma_silver_etl.py` | Silver layer ETL transformations |
| Silver | `etl/silver/mma_silver_schema.py` | Silver schema definitions |
| Gold | `etl/gold/mma_gold_ranking.py` | Fighter/fight ranking generation |
| Gold | `etl/gold/mma_gold_features.py` | Gold feature engineering |
| Gold | `etl/gold/mma_gold_catboost.py` | CatBoost training pipeline |
| Gold | `etl/gold/mma_gold_catboost_tune.py` | CatBoost hyperparameter tuning |

---

## What This Module Covers

### `mma_silver_etl.py` + `mma_silver_schema.py`
Silver layer transformations and schema definitions — cleans and structures raw bronze data into a consistent format for downstream feature engineering.

### `mma_gold_features.py`
Builds the gold prefight feature table used as input to all models. This is where the core feature engineering lives — fighter stats, matchup deltas, streaks, and inactivity signals.

### `mma_gold_ranking.py`
Generates fighter and fight rankings used as additional input features downstream.

### `mma_gold_catboost.py`
Trains and scores UFC win/loss models using CatBoost (and optionally logistic regression).
- Handles missing values natively via CatBoost
- Reads tuned hyperparameters from DuckDB
- Supports market odds blending and incremental scoring
- CLI options for device selection (CPU/GPU), odds source, and more

```bash
python -m ml_kuda_sports_lab.etl.gold.mma_gold_catboost --help
```

### `mma_gold_catboost_tune.py`
Hyperparameter tuning for the CatBoost model using Optuna. Run this before retraining to find optimal parameters, which are written back to DuckDB for the training pipeline to pick up.

```bash
python -m ml_kuda_sports_lab.etl.gold.mma_gold_catboost_tune --help
```

---

## How to Use

1. **Set up your environment** — follow the setup instructions in `copilot-instructions.md` (activate the correct Python environment and set DuckDB paths)
2. **Run feature engineering** — run `mma_gold_features.py` first to build the gold feature table
3. **Tune hyperparameters** — run `mma_gold_catboost_tune.py` to find optimal parameters (written back to DuckDB)
4. **Run model training** — use `mma_gold_catboost.py` with your preferred CLI arguments
5. **Outputs** — model artifacts saved to the specified output directory; scored predictions written back to DuckDB for downstream use

---

## Not Included (Private)

The following are private and not part of this open-source release:

- Front end (not available yet)
- Full ETL/lakehouse orchestration
- Internal infra and deployment code
- Production data assets and configs

---

## License

This project is licensed under **[Business Source License 1.1 (BSL 1.1)](LICENSE.md)**.

**Plain English:** You can use, study, and contribute to this code freely for personal, non-commercial, and evaluation purposes. Commercial or production use requires a separate license. On **2030-01-01** this code converts to **GPL-2.0-or-later** and becomes fully open source.

For commercial licensing inquiries contact: datatomas@uppercutanalytics.com

---

## Contact

- Business LinkedIn: https://www.linkedin.com/company/fight-prophet
- Founder LinkedIn: https://www.linkedin.com/in/datatomas/
- Business GitHub: https://github.com/datatomas/fight_prophet
- Business Email: datatomas@uppercutanalytics.com
