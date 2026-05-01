Orange Pi (ARM64)          Tailscale VPN           PC (AMD64 + Registry)
├─ Streamlit Dashboard ←───────────────────────→ ├─ Docker Registry (port 5000)
├─ WOL Service                                   ├─ ML Workloads (GPU)
└─ Arpwatch                                      └─ DuckDB Data Lake

Flow:
1. Orange Pi needs image update
2. Sends WOL packet to PC
3. Waits for PC to boot (~30s)
4. Pulls image via Tailscale (100.x.x.x:5000)
5. PC auto-sleeps after 10min idle


# find rsa pages

curl -s https://mmajunkie.usatoday.com | grep -i "rss\|feed"
curl -I https://mmajunkie.usatoday.com/feed

#activate venv
source /home/ares/Documents/uppercutanalytics/.venv-front/bin/activate

# to call the rsa finder
python find_rss.py https://www.espn.com --section mma
python find_rss.py https://sports.yahoo.com --section mma
python find_rss.py https://www.sherdog.com --section mma




# Straemlit check dashboard
source /home/ares/Documents/uppercutanalytics/.venv-front/bin/activate
streamlit run src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py 
http://localhost:8501

# run it with logs
python src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py 2>&1 | tee logs.txt

# run from root
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
streamlit run src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py

# absolute path

streamlit run /home/ares/Documents/gitrepos/ml_kuda_sports_lab/src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py

# color number for red
#b91c1c for the darker end of the red gradient
#dc2626 in some Astro/CTA gradients
#f87171 indirectly via rgba(248, 113, 113, ...) for softer borders/highlights