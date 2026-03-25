# Fight Prophet

**Fight Prophet** is a data engineering + machine learning platform for UFC/MMA fight outcome prediction.  
It is built for bettors and analysts who want **signal over noise**: reproducible pipelines, transparent features, and production-ready scoring.

---

## What it does

- Ingests historical UFC/MMA fight and fighter data
- Processes fight-level and fighter-level metrics in a medallion pipeline
- Trains and serves ML predictions for upcoming fights
- Delivers insights in a Streamlit dashboard

---

## Architecture

Fight Prophet follows a lakehouse + medallion pattern:

- **Bronze**: raw scraped/source data  
- **Silver**: cleaned, normalized, feature-ready tables  
- **Gold**: model-ready datasets, rankings, and prediction outputs  

### Storage & Compute

- **DuckDB** for analytics and transformations
- **Parquet** datasets in local disk / Azure Blob-backed lake
- **Python ML stack** (XGBoost/CatBoost, Pandas, NumPy, scikit-learn)
- **Streamlit** frontend for predictions and analytics

---

## Key Features

- 📊 Upcoming fight prediction probabilities  
- 🧠 Historical model performance tracking  
- 🥋 Fighter rankings and profile analytics  
- 🗂️ Event history and backtesting views  
- ☁️ Local + Azure Blob data source support  

---

## Repository Structure

```text
src/ml_kuda_sports_lab/
  etl/                  # Bronze/Silver/Gold pipelines
  weak_supervision/     # ML training + feature pipelines
  front_end/            # Streamlit app
  webscraping/          # Data collection
.github/workflows/      # CI/CD and sync automations
```

---

## Quick Start

### 1) Clone

```bash
git clone https://github.com/datatomas/fight_prophet.git
cd fight_prophet
```

### 2) Set Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3) Configure environment variables

Create a `.env` (or export in shell), e.g.:

```bash
export DUCK_DEV_DB="/path/to/dev.duckdb"
export DUCK_WH_DB="/path/to/warehouse.duckdb"
export PARQUET_BASE_URI="/path/to/lake"
```

If using Azure Blob, also set:

```bash
export AZURE_STORAGE_ACCOUNT="..."
export AZURE_STORAGE_KEY="..."
```

### 4) Run ETL (example)

```bash
python3 -m ml_kuda_sports_lab.etl.silver.mma_silver_schema --target dev --steps fighter_stats
```

### 5) Run dashboard

```bash
streamlit run src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py
```

---

## Modeling Notes

- Predictions are probabilistic, not guarantees.
- Features are designed to avoid leakage (pre-fight only).
- Always validate data freshness and table counts before training/inference.

---

## Roadmap

- [ ] Expanded explainability (feature importance per fight)
- [ ] Automated drift checks
- [ ] More robust cross-promotion and market signal layers
- [ ] Public model cards and evaluation reports

---

## Disclaimer

Fight Prophet is for **informational and educational purposes only**.  
It does **not** provide financial or betting advice.

---

## Contact

- **Business LinkedIn:** https://www.linkedin.com/company/fight-prophet  
- **Founder LinkedIn:** https://www.linkedin.com/in/datatomas/  
- **Business GitHub:** https://github.com/datatomas/fight_prophet
