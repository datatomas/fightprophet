# Docker Compose Runbook — Fight Prophet ML Pipeline

Frontend is hosted in Azure Container Apps and is **not** part of this pipeline.

---

# login without a password manager important main login
source ~/.config/ghcr/env
printf '%s' "$GIT_PAT" | docker login ghcr.io -u datatomas --password-stdin

sudo docker login ghcr.io -u datatomas


## 1. First-Time Setup: GHCR Login & Credentials

### Secure login (encrypted credential store)

```bash
# Install tools
sudo apt-get update
sudo apt-get install -y pass gnupg2 pinentry-curses

# Create GPG key (interactive)
gpg --full-generate-key
# Recommended: Key type 9 (ECC sign+encrypt), Curve 1 (Curve25519)

# List keys — copy the long key id
gpg --list-secret-keys --keyid-format LONG

# Init pass with your key id
pass init <YOUR_KEY_ID>
# Example: pass init 4DDA6BF3E817CB85

# Configure Docker to use encrypted creds store
mkdir -p ~/.docker
cat > ~/.docker/config.json <<'JSON'
{
  "credsStore": "pass"
}
JSON

# Recommended: terminal pinentry
echo 'export GPG_TTY=$(tty)' >> ~/.bashrc
source ~/.bashrc
```

### Login to GHCR (start here when repulling)

```bash
# Check PAT is set before logging in
[ -n "$GIT_PAT" ] && echo "GIT_PAT is set" || echo "GIT_PAT is EMPTY"

docker logout ghcr.io
echo "$GIT_PAT" | docker login ghcr.io -u datatomas --password-stdin

# Also make sure this file doesn't hold plaintext credentials
nano ~/.config/ghcr/env
```

### Key files

| Purpose | Path |
|---|---|
| Pipeline env vars | `/home/ares/.config/ml_kuda_sports_lab/pipeline.env` |
| Weekly service | `crons/weekly_sunday_docker.service` |
| Weekly timer | `crons/weekly_sunday_docker.timer` |
| Monthly service | `crons/monthly_full_docker.service` |
| Monthly timer | `crons/monthly_full_docker.timer` |
| Pipeline shell script | `crons/run_pipeline.sh` |

```bash
# Open them quickly
sudo nano /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/weekly_sunday_docker.service
sudo nano /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/monthly_full_docker.service
sudo nano /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/weekly_sunday_docker.timer
sudo nano /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/monthly_full_docker.timer
nano /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/run_pipeline.sh
nano /home/ares/.config/ml_kuda_sports_lab/pipeline.env
```

---

## 2. Docker Daemon Logging (journald)

Logs from all containers go to journalctl automatically.

```bash
# daemon.json is already configured at /etc/docker/daemon.json:
# {
#   "log-driver": "journald",
#   "log-opts": { "tag": "{{.Name}}" }
# }

# After any change to daemon.json:
sudo systemctl restart docker
docker info | grep 'Logging Driver'
# expected: Logging Driver: journald
```

---

## 3. Testing Weekly & Monthly Services (Start Here for Tests)

# more robust set up to docker to last

sudo install -m 0644 crons/weekly_sunday_docker.service /etc/systemd/system/weekly_sunday_docker.service
sudo install -m 0644 crons/weekly_sunday_docker.timer /etc/systemd/system/weekly_sunday_docker.timer

sudo systemctl daemon-reload
sudo systemctl enable --now weekly_sunday_docker.timer

systemctl status weekly_sunday_docker.timer --no-pager -l
systemctl list-timers --all | grep weekly_sunday_docker


### Verify systemd timer status

```bash

sudo systemctl daemon-reload
sudo systemctl enable --now weekly_sunday_docker.timer

systemctl status weekly_sunday_docker.timer --no-pager -l
systemctl list-timers --all | grep weekly_sunday_docker
systemctl show weekly_sunday_docker.timer \
  -p LoadState -p ActiveState -p UnitFileState -p FragmentPath -p NextElapseUSecRealtime

# List all kuda-related units
systemctl list-units --all | grep kuda

# Or check timers specifically
systemctl list-timers | grep kuda

# Check status of a specific unit
systemctl status weekly_sunday_docker.timer
systemctl status weekly_sunday_docker.service
systemctl status monthly_full_docker.timer
```

### Install / update timers from repo

```bash
sudo systemctl link /home/ares/Documents/gitrepos/ml_kuda_sports_lab/crons/weekly_sunday_docker.timer
sudo systemctl daemon-reload
sudo systemctl enable --now weekly_sunday_docker.timer

# Check next scheduled run
systemctl list-timers --all | grep weekly_sunday_docker
```

### Run weekly or monthly pipeline manually (full)

```bash
# Weekly
sudo systemctl start weekly_sunday_docker.service

# Monthly
sudo systemctl start monthly_full_docker.service
```

### Verify compose env wiring before running

```bash
export PIPELINE_ENV_FILE="/home/ares/.config/ml_kuda_sports_lab/pipeline.env"
docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday config >/tmp/mlkuda_sunday_config.yml && echo "compose env OK"
```

### Weekly Buttondown email

The Sunday profile includes `mma_buttondown_weekly_email` after the Azure dashboard export.
It creates or sends a Buttondown email that links to:

```text
https://app.fightprophet.com/?page=predictions
```

Add these to `/home/ares/.config/ml_kuda_sports_lab/pipeline.env`:

```bash
BUTTONDOWN_ENABLED=true
BUTTONDOWN_API_KEY=your_buttondown_api_key
BUTTONDOWN_EMAIL_STATUS=draft
BUTTONDOWN_CONFIRM_SEND=false
PUBLIC_APP_URL=https://app.fightprophet.com
NEWSLETTER_TIMEZONE=America/Bogota
```

Test without sending:

```bash
BUTTONDOWN_ENABLED=true BUTTONDOWN_DRY_RUN=true \
PYTHONPATH=src python3 -m ml_kuda_sports_lab.etl.gold.mma_buttondown_weekly_email
```

When the draft looks good, change `BUTTONDOWN_EMAIL_STATUS=about_to_send`. For the
first live API send on a new Buttondown API key, set `BUTTONDOWN_CONFIRM_SEND=true`
once, then set it back to `false` after the send succeeds.

### Run the full pipeline directly (bypassing systemd)
# update pipeline

export PIPELINE_ENV_FILE="/home/ares/.config/ml_kuda_sports_lab/pipeline.env"
docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday pull

```bash
export PIPELINE_ENV_FILE="/home/ares/.config/ml_kuda_sports_lab/pipeline.env"

# Weekly (sunday profile)
sudo -E docker compose \
  -f /home/ares/Documents/gitrepos/ml_kuda_sports_lab/docker-compose.yml \
  --env-file "$PIPELINE_ENV_FILE" \
  --profile sunday up --remove-orphans

# Monthly
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly up --remove-orphans
```

---

## 4. Logs

### journalctl — container logs (recommended)


# show if its active
systemctl list-timers --all | grep -i sunday

# reload dwemon so it survives
sudo systemctl daemon-reload

# re eneble and start time to make it persistent
sudo systemctl enable --now weekly_sunday_docker.timer

# verify it will survie boot
systemctl is-enabled weekly_sunday_docker.timer   # should print: enabled
systemctl is-active  weekly_sunday_docker.timer   # should print: active
systemctl list-timers --all | grep sunday         # should show a real NEXT time
ls -l /etc/systemd/system/timers.target.wants/ | grep sunday   # symlink must exist

#test run

```bash
# live test service logs
sudo journalctl -fu weekly_sunday_docker.service --output=cat

# see logs from last run
journalctl -u weekly_sunday_docker.service --since today --no-pager --output=cat

# Live tail a specific container
journalctl -f CONTAINER_NAME=mma_gold_catboost_tune_sunday-1

# All logs from last run
journalctl CONTAINER_NAME=mma_gold_catboost_tune_sunday-1

# All ML pipeline containers since yesterday (prefix filter)
journalctl -S yesterday CONTAINER_TAG=mma_gold

# Any mma_ container from the last hour
journalctl -S "1 hour ago" CONTAINER_TAG=mma_
```

### journalctl — systemd service logs

```bash
# Weekly service — live
journalctl -fu weekly_sunday_docker.service --output=cat

# Weekly service — today
journalctl -u weekly_sunday_docker.service --since today --no-pager --output=cat

# Monthly service — today
journalctl -u monthly_sunday_docker.service --since today --no-pager

# Docker daemon itself
sudo journalctl -b --unit docker.service --no-pager | tail -200
```

### docker compose logs (also works, reads from journald)

```bash
# Live tail a service
docker compose logs -f mma_gold_catboost_tune_sunday

# Full logs after run
docker compose logs mma_gold_catboost_train_tuned

# Last 100 lines
docker compose logs --tail=100 mma_gold_catboost_tune_sunday

# All services live
docker compose logs -f
```

---

## 5. Individual Service Runs (--no-deps)

Run any single service without triggering its `depends_on` chain.

```bash
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
export PIPELINE_ENV_FILE="/home/ares/.config/ml_kuda_sports_lab/pipeline.env"

# pull all
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile "*" pull

sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile "*" pull --include-deps


#pull only  sunday 
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday pull

```

### Sunday pipeline — step by step

```bash
# Manual data
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_title_vacates_load
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_title_vacates_upload

# Scrapers
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_recent
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_upcoming
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_based_fighters_prod
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_based_fighters_upcoming
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_based_fights_recent
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_based_fights_upcoming
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps ws_ufc_events_based_fights_refresh_stats_recent

# ETL
# ETL
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_silver_schema

# Active/inactive canonical status step
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_fighter_status

sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_features
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_ranking
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_belt_holders
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_fighter_countries_sync

# ML training chain
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_catboost_train_sunday_initial
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_catboost_tune_sunday
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_catboost_train_tuned

# test catboost edge and signal
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" \
  --profile sunday run --rm --no-deps --entrypoint python3 \
  mma_parquets_dashboard -c "
import os, tempfile, pathlib, duckdb
from azure.storage.blob import ContainerClient

acct = os.environ['AZURE_STORAGE_ACCOUNT']
key  = os.environ['AZURE_STORAGE_KEY']
cont = os.environ.get('AZURE_STORAGE_CONTAINER', 'fightprophet-dashboard')

cli = ContainerClient(
    account_url=f'https://{acct}.blob.core.windows.net',
    container_name=cont,
    credential=key,
)

all_blobs = [
    b.name
    for b in cli.list_blobs(name_starts_with='mma/diamond/dashboard_upcoming_cards_ensemble/')
    if b.name.endswith('.parquet')
]
print('found blobs:', len(all_blobs))
print('sample blobs:', all_blobs[:3])

if not all_blobs:
    raise SystemExit('No parquet blobs found')

latest_export = sorted({p.split('/')[3] for p in all_blobs})[-1]
blobs = [b for b in all_blobs if f'/{latest_export}/' in b]
print('latest export:', latest_export)
print('latest export blob count:', len(blobs))

tmpdir = tempfile.mkdtemp(prefix='upcoming_ensemble_')
for i, blob_name in enumerate(blobs):
    out = pathlib.Path(tmpdir) / f'part_{i}.parquet'
    out.write_bytes(cli.download_blob(blob_name).readall())

con = duckdb.connect()
glob_path = f'{tmpdir}/*.parquet'

print('rows:', con.execute('SELECT COUNT(*) FROM read_parquet(?)', [glob_path]).fetchone()[0])
print('edge non-null:', con.execute('SELECT COUNT(edge) FROM read_parquet(?)', [glob_path]).fetchone()[0])
print('signal counts:', con.execute('SELECT signal_strength, COUNT(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 2 DESC', [glob_path]).fetchall())
print(
    con.execute(
        'SELECT event_name, fighter_name_display, opponent_name_display, edge, signal_strength FROM read_parquet(?) LIMIT 8',
        [glob_path]
    ).fetchall()
)
"



# Export + backup
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_parquets_dashboard
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps duckdb_disaster_clone_weekly

# Manual overrides refresh (countries + vacated belts) → then re-export
# Use this flow after editing datasets/manual_overrides/*.csv
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_fighter_countries_sync
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_title_vacates_load
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_manual_title_vacates_upload
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_belt_holders
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_parquets_dashboard
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps duckdb_disaster_clone_weekly
```

### Run ML chain forward only (skip scraping + ETL)

`--no-deps` prevents compose from starting the full dependency chain (16+ containers).
`--profile sunday` ensures the service definition is found.
Must `cd` to repo root first so compose finds `docker-compose.yml`.

```bash
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
export PIPELINE_ENV_FILE="/home/ares/.config/ml_kuda_sports_lab/pipeline.env"

sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_gold_catboost_train_tuned && \
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps mma_parquets_dashboard && \
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile sunday run --rm --no-deps duckdb_disaster_clone_weekly
```

### Monthly pipeline — step by step

```bash
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps ws_ufc_events_recent_monthly
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps ws_ufc_events_upcoming_monthly
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps ws_ufc_events_based_fights_full
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps ws_ufc_events_based_fights_refresh_stats_full
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps ws_ufc_fighters_full
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps duckdb_disaster_clone
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps duckdb_dev_clone
```

### Manual profile

```bash
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile manual run --rm --no-deps mma_gold_catboost_train
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile manual run --rm --no-deps mma_gold_catboost_tune
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile manual run --rm --no-deps mma_parquets_dashboard_manual
```

### Quick validation queries after a run

```bash
duckdb "$DUCK_WH_DB" -c "SELECT COUNT(*) AS n FROM gold.manual_title_vacates;"
duckdb "$DUCK_WH_DB" -c "SELECT COUNT(*) AS n FROM gold.belt_holders;"
duckdb "$DUCK_WH_DB" -c "SELECT COUNT(*) AS n FROM gold.title_fight_history;"
duckdb "$DUCK_WH_DB" -c "SELECT COUNT(*) AS n FROM gold.manual_fighter_countries;"
```

---

## 6. Direct docker run Calls

### Image variables

```bash
export GHCR_OWNER="datatomas"

export BRONZE_WS_IMAGE="ghcr.io/${GHCR_OWNER}/ml_kuda_sports_lab-bronze-webscraping:latest"
export SILVER_ETL_IMAGE="ghcr.io/${GHCR_OWNER}/ml_kuda_sports_lab-silver-etl:latest"
export GOLD_ETL_IMAGE="ghcr.io/${GHCR_OWNER}/ml_kuda_sports_lab-gold-etl:latest"
export FRONT_IMAGE="ghcr.io/${GHCR_OWNER}/ml_kuda_sports_lab-front-end:latest"
export DUCK_IMAGE="ghcr.io/${GHCR_OWNER}/ml_kuda_sports_lab-duckdb:latest"
# Verify
echo "WS_IMAGE=[$WS_IMAGE]"
echo "SILVER_ETL_IMAGE=[$SILVER_ETL_IMAGE]"
echo "GOLD_ETL_IMAGE=[$GOLD_ETL_IMAGE]"
echo "FRONT_IMAGE=[$FRONT_IMAGE]"
echo "DUCK_IMAGE=[$DUCK_IMAGE]"
```

### Pull all images

```bash
docker pull "$BRONZE_WS_IMAGE"
docker pull "$SILVER_ETL_IMAGE"
docker pull "$GOLD_ETL_IMAGE"
docker pull "$DUCK_IMAGE"
docker pull "$FRONT_IMAGE"

# Quick runtime smoke test
sudo docker run --rm --entrypoint python3 "$SILVER_ETL_IMAGE" --version
sudo docker run --rm --entrypoint python3 "$GOLD_ETL_IMAGE" --version
```

### Scrapers

```bash
# Past events
sudo -E docker run -d --name ufc-events-past \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_all.py \
  -e SCRAPER_ARGS="--target prod --events past" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Upcoming events
sudo -E docker run -d --name ufc-events-upcoming \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_all.py \
  -e SCRAPER_ARGS="--target prod --events upcoming" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fighters — last 90 days (updates stats)
sudo -E docker run -d --name ufc-fighters-prod \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fighters.py \
  -e SCRAPER_ARGS="--target prod --threads 24 --concurrency 2 --days-back 90" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fighters — upcoming events
sudo -E docker run -d --name ufc-fighters-upcoming \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fighters.py \
  -e SCRAPER_ARGS="--target prod --threads 24 --concurrency 2 --events upcoming" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fights — incremental recent completed
sudo -E docker run -d --name ufc-fights-recent \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --mode recent --fights completed --events-source db" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fights — full refresh all completed
sudo -E docker run -d --name ufc-fights-full \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --mode full --status completed --limit-events 0 --limit-fights 0" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fights — full refresh last 7 events
sudo -E docker run --rm --name ufc-fights-last7 \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --mode full --status completed --events-source db --limit-events 7" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Fights — upcoming only
sudo -E docker run -d --name ufc-fights-upcoming \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --mode full --status upcoming" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"

# Stats refresh — recent
sudo -E docker run --rm --name ufc-fights-refresh-stats-recent \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --refresh-stats --status completed --mode recent --events-source db" \
  -v "${DUCK_HOST_DB_DIR}:/data/db" \
  "$WS_IMAGE"

# Stats refresh — full
sudo -E docker run --rm --name ufc-fights-refresh-stats-full \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_events_based_fights.py \
  -e SCRAPER_ARGS="--target prod --refresh-stats --status completed --mode full --events-source db" \
  -v "${DUCK_HOST_DB_DIR}:/data/db" \
  "$WS_IMAGE"

# Fighters — full A–Z scan (monthly)
sudo -E docker run --rm --name ufc-fighters-full \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e SCRAPER_NAME=ws_ufc_fighters_all.py \
  -e SCRAPER_ARGS="--target prod --type urls,fighters,stats" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$WS_IMAGE"
```

### Silver ETL

```bash
sudo -E docker run --rm --name mma-silver-etl \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.silver.mma_silver_schema \
  -e ETL_ARGS="--target prod" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$SILVER_ETL_IMAGE"
```

### Gold ETL

```bash
# Features
sudo -E docker run --rm --name mma-gold-etl \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_features \
  -e ETL_ARGS="--target prod --rebuild" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$GOLD_ETL_IMAGE"

# Rankings
sudo -E docker run --rm --name mma-gold-ranking \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_ranking \
  -e ETL_ARGS="--target prod" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$GOLD_ETL_IMAGE"

# Manual title vacates — load
sudo -E docker run --rm --name mma-manual-title-vacates-load \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_manual_title_vacates \
  -e ETL_ARGS="--target prod --rebuild --csv-path /workspace/datasets/manual_overrides/ufc_title_vacates.csv" \
  -v /home/ares/db/duck/warehouse:/data/db \
  -v /home/ares/Documents/gitrepos/ml_kuda_sports_lab:/workspace:ro \
  "$GOLD_ETL_IMAGE"

# Manual title vacates — upload to Azure
sudo --preserve-env=AZURE_STORAGE_ACCOUNT,AZURE_STORAGE_KEY,AZURE_STORAGE_CONTAINER docker run --rm --name mma-manual-title-vacates-upload \
  -e AZURE_STORAGE_ACCOUNT \
  -e AZURE_STORAGE_KEY \
  -e AZURE_STORAGE_CONTAINER \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_upload_manual_title_vacates \
  -e ETL_ARGS="--csv-path /workspace/datasets/manual_overrides/ufc_title_vacates.csv" \
  -v /home/ares/Documents/gitrepos/ml_kuda_sports_lab:/workspace:ro \
  "$GOLD_ETL_IMAGE"

# Belt holders + title fight history
sudo -E docker run --rm --name mma-gold-belt-holders \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_belt_holders \
  -e ETL_ARGS="--target prod --rebuild" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$GOLD_ETL_IMAGE"

# Fighter countries sync
sudo -E docker run --rm --name mma-manual-fighter-countries-sync \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_fighter_countries \
  -e ETL_ARGS="--target prod --csv-path /workspace/datasets/manual_overrides/ufc_fighter_manual_country.csv" \
  -v /home/ares/db/duck/warehouse:/data/db \
  -v /home/ares/Documents/gitrepos/ml_kuda_sports_lab:/workspace \
  "$GOLD_ETL_IMAGE"
```

### CatBoost train / tune

```bash
# Initial train (fightodds source — best scorer)
sudo docker run --rm --gpus all \
  --name mma-catboost-train-fightodds \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_catboost \
  -e "ETL_ARGS=--target prod --model all --rebuild --overwrite-trainall --min-event-date 1995-01-01 --min-feature-coverage 0.0 --odds-source fightodds --odds-format american --fightodds-promotion-slug ufc --sportsbook Pinnacle --sportsbook Stake --market-blend-weight 0.70 --strong-min-edge 0.08 --strong-min-agreement 0.80 --medium-min-edge 0.04 --medium-min-agreement 0.65" \
  -v /home/ares/db/duck/warehouse:/data/db \
  -v /home/ares/Documents/uppercutanalytics/models:/root/Documents/uppercutanalytics/models \
  "$GOLD_ETL_IMAGE"

# Tune with Optuna TPE (250 trials)
sudo docker run --rm --gpus all \
  --name mma-catboost-tune \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_gold_catboost_tune \
  -e "ETL_ARGS=--target prod --n-trials 250 --metric auc --device cuda --min-event-date 1995-01-01 --min-feature-coverage 0.0" \
  -v /home/ares/db/duck/warehouse:/data/db \
  "$GOLD_ETL_IMAGE"
```

### Parquet export to Azure

```bash
sudo --preserve-env=AZURE_STORAGE_ACCOUNT,AZURE_STORAGE_KEY,AZURE_STORAGE_CONTAINER docker run --rm \
  --name mma-parquet-export-prod \
  -e DUCK_WH_DB=/data/db/sports_ml_warehouse.duckdb \
  -e AZURE_STORAGE_ACCOUNT \
  -e AZURE_STORAGE_KEY \
  -e AZURE_STORAGE_CONTAINER \
  -e ETL_MODULE=ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \
  -e ETL_ARGS="--target prod --dataset all --prefix mma/diamond --parquet-base az://fightprophet-dashboard" \
  -v /home/ares/db/duck/warehouse:/data/db \
  ghcr.io/datatomas/ml_kuda_sports_lab-gold-etl:latest
```

### DuckDB disaster recovery clone

```bash
DUCK_IMAGE="${DUCK_IMAGE:-ghcr.io/datatomas/ml_kuda_sports_lab-duckdb:latest}"

sudo docker run --rm --name duckdb-disaster-clone \
  -v "$(dirname "${DUCK_WH_DB:?DUCK_WH_DB not set}"):/data/db" \
  -e DB_SCRIPT_NAME=duckdb_disaster_clone.py \
  -e "DB_SCRIPT_ARGS=/data/db/sports_ml_warehouse.duckdb /data/db/drdb /data/db/drdatasets" \
  "$DUCK_IMAGE"

# Via compose (one-off)
sudo -E docker compose --env-file "$PIPELINE_ENV_FILE" --profile monthly run --rm --no-deps duckdb_disaster_clone
```

### Frontend (local test — not part of compose pipeline)

```bash
export FRONT_IMAGE="ghcr.io/datatomas/ml_kuda_sports_lab-front-end:latest"

# Verify Azure vars
source ~/.bashrc
echo "$AZURE_STORAGE_ACCOUNT"
[ -n "$AZURE_STORAGE_KEY" ] && echo "AZURE_STORAGE_KEY set" || echo "AZURE_STORAGE_KEY missing"

# Run Streamlit container
sudo docker rm -f mma-front 2>/dev/null || true
sudo --preserve-env=AZURE_STORAGE_ACCOUNT,AZURE_STORAGE_KEY,AZURE_STORAGE_CONTAINER docker run -d --name mma-front \
  -p 8501:8501 \
  -e PARQUET_BASE_URI="az://fightprophet-dashboard" \
  -e AZURE_STORAGE_ACCOUNT \
  -e AZURE_STORAGE_KEY \
  -e AZURE_STORAGE_CONTAINER \
  "$FRONT_IMAGE"
```

---

## 7. Python Direct Calls (no Docker)

```bash
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
source /home/ares/Documents/uppercutanalytics/venv/bin/activate
export PYTHONPATH="/home/ares/Documents/gitrepos/ml_kuda_sports_lab/src:${PYTHONPATH:-}"
export DUCK_WH_DB="/home/ares/db/duck/warehouse/sports_ml_warehouse.duckdb"

# Manual title vacates
python3 -m ml_kuda_sports_lab.etl.gold.mma_gold_manual_title_vacates \
  --target prod \
  --rebuild \
  --csv-path datasets/manual_overrides/ufc_title_vacates.csv

python3 -m ml_kuda_sports_lab.etl.gold.mma_upload_manual_title_vacates \
  --csv-path datasets/manual_overrides/ufc_title_vacates.csv

# Belt holders + title fight history
python3 -m ml_kuda_sports_lab.etl.gold.mma_gold_belt_holders \
  --target prod \
  --rebuild

# Fighter countries sync
python3 -m ml_kuda_sports_lab.etl.gold.mma_gold_fighter_countries \
  --target prod \
  --csv-path datasets/manual_overrides/ufc_fighter_manual_country.csv
```

---

## 8. Pipeline Order Reference

```
mma_manual_title_vacates_load
  -> mma_manual_title_vacates_upload
  -> ws_ufc_events_recent ... mma_silver_schema ... mma_gold_features
  -> mma_gold_ranking
  -> mma_gold_belt_holders
  -> mma_manual_fighter_countries_sync
  -> mma_gold_catboost_train_sunday_initial
  -> mma_gold_catboost_tune_sunday          (Optuna TPE, 250 trials)
  -> mma_gold_catboost_train_tuned
  -> mma_parquets_dashboard                 (Azure export)
  -> mma_buttondown_weekly_email            (Buttondown draft/send)
  -> duckdb_disaster_clone_weekly           (last)
```

Rules:
- Never run stats refresh before events + fights are populated.
- Never run fights with `--events-source db` before events are scraped.
- Use `--no-deps` for individual service runs; the full profile `up` enforces order via `depends_on`.


# test export and rankin in python

cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab


PYTHONPATH=src python3 -m ml_kuda_sports_lab.etl.gold.mma_gold_ranking \
  --target prod \
  --rebuild \
  --as-of-date 2026-04-29

PYTHONPATH=src python3 -m ml_kuda_sports_lab.etl.gold.mma_parquets_dashboard \
  --target prod \
  --dataset rankings overall_rankings fighter_profiles belt_holders



# Add compose as sudo docker

# 1. Add your user to the docker group
sudo usermod -aG docker $USER

# 2. Activate the new group in your current shell (or log out/in)
newgrp docker

# 3. Verify — this must work with NO sudo:
docker ps
docker compose version
