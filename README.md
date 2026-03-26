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

This repo currently includes and maintains:

| File | Purpose |
|---|---|
| `src/ml_kuda_sports_lab/etl/gold/mma_gold_ranking.py` | Fighter/fight ranking generation |
| `src/ml_kuda_sports_lab/etl/gold/mma_gold_catboost.py` | CatBoost training pipeline |
| `src/ml_kuda_sports_lab/etl/gold/mma_gold_train_models.py` | Logistic regression + XGBoost training pipeline |
| `src/ml_kuda_sports_lab/etl/gold/mma_gold_features_theory.md` | Feature engineering methodology |
| `src/ml_kuda_sports_lab/etl/gold/ml_model.md` | Modeling approach and diagnostics |

These are automatically synced to the public repo via GitHub Actions.

---

## What This Module Covers

### `mma_gold_ranking.py`
Generates fighter and fight rankings used as input features downstream.

### `mma_gold_catboost.py`
Trains and scores UFC win/loss models using CatBoost (and optionally logistic regression).
- Handles missing values natively via CatBoost
- Reads tuned hyperparameters from DuckDB
- Supports market odds blending and incremental scoring
- CLI options for device selection (CPU/GPU), odds source, and more

```bash
python -m ml_kuda_sports_lab.etl.gold.mma_gold_catboost --help
```

### `mma_gold_train_models.py`
Trains and scores UFC win/loss models using logistic regression and XGBoost, based on the gold prefight features table.
- Time-based data split to prevent data leakage
- Numeric-only features for robust modeling
- Supports blending model predictions with betting market odds
- Outputs model artifacts and writes scored predictions back to DuckDB
- CLI options for retraining, incremental scoring, and odds source selection

```bash
python -m ml_kuda_sports_lab.etl.gold.mma_gold_train_models --help
```

### `mma_gold_features_theory.md`
Explains the theory and methodology behind the gold feature engineering — including Bayesian shrinkage for finish rates and streak/inactivity features. Read this before modifying or extending the feature set.

### `ml_model.md`
Documents the modeling approach, diagnostics, and best practices for UFC win/loss prediction. Reference this for model configuration, evaluation metrics, and improvement guidance.

---

## How to Use

1. **Set up your environment** — follow the setup instructions in `copilot-instructions.md` (activate the correct Python environment and set DuckDB paths)
2. **Run model training** — use `mma_gold_train_models.py` or `mma_gold_catboost.py`, adjusting CLI arguments for your workflow
3. **Review the docs** — read the markdown files for a deep dive into feature engineering and modeling choices
4. **Outputs** — model artifacts are saved to the specified output directory; scored predictions are written back to DuckDB tables/views for downstream use

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
