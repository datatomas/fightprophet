# Fight Prophet (Open-Source Module)

**Fight Prophet** is a UFC/MMA prediction platform.  
This repository currently open-sources a **limited module** of the system: ranking logic and model training components.

> This is **not** the full production codebase.

---

## Open-Source Scope (Current)

This repo currently includes and maintains:

- `src/ml_kuda_sports_lab/etl/gold/mma_gold_ranking.py`
- `src/ml_kuda_sports_lab/etl/gold/mma_gold_catboost.py`

These are automatically synced to the public repo via GitHub Actions.

---

## Not Included (Private)

The following areas are currently private and not part of this open-source release:

- Data scraping pipelines
- Full ETL/lakehouse orchestration
- Internal infra/deployment code
- Production data assets and configs

---

## What This Module Covers

- Fighter/fight ranking generation
- CatBoost training pipeline for fight outcome modeling
- Iterative model experimentation on curated features

---

## License

This project is licensed under **Business Source License 1.1 (BSL 1.1)**.  
See the full terms here: [LICENSE.md](LICENSE.md).

Key points:

- Personal, non-commercial, and evaluation use allowed
- Commercial/production use requires a commercial license
- Change Date: **2030-01-01**
- Change License: **GPL-2.0-or-later**

---

## Contact

- Business LinkedIn: https://www.linkedin.com/company/fight-prophet  
- Founder LinkedIn: https://www.linkedin.com/in/datatomas/  
- Business GitHub: https://github.com/datatomas/fight_prophet
- Business Email: datatomas@uppercutanalytics.com
