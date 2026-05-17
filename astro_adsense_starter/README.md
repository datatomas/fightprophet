# Astro + AdSense Starter (Cloudflare Pages)

## Open Source Front End & Rankings

This front end and the MMA rankings logic are open source as part of the FightProphet project — the home of MA intelligence.

- The Astro front end provides the public website and rankings UI.
- The Streamlit dashboard for MMA predictions and analytics is also open source. See [../src/ml_kuda_sports_lab/front_end/README.md](../src/ml_kuda_sports_lab/front_end/README.md) for details and usage.

Contributions are welcome!

Production-oriented starter for adding AdSense to an Astro site deployed on Cloudflare Pages.

## 1) Install and run

```bash
cd astro_adsense_starter
npm install
npm run dev
```

## 2) Configure env

Copy `.env.example` to `.env` and set:

- `PUBLIC_ADSENSE_CLIENT=ca-pub-your-real-id`
- `PUBLIC_ADSENSE_ENABLED=true` only after privacy/consent setup is ready
- `PUBLIC_GOOGLE_SITE_VERIFICATION=your-search-console-token` if Google asks for a meta-tag verification token
- `PUBLIC_CONTACT_EMAIL=hello@fightprophet.com` for the public contact/privacy pages
- `PUBLIC_KOFI_WIDGET_ENABLED=false` during AdSense review, so support widgets do not compete with publisher content
- `PUBLIC_SITE_URL=https://yourdomain.com`
- `PUBLIC_APP_URL=https://app.fightprophet.com`
- `PUBLIC_BUTTONDOWN_SUBSCRIBE_URL=https://buttondown.com/api/emails/embed-subscribe/fightprophet`

## 3) Replace placeholders

- In `src/pages/index.astro`, replace `slot="1234567890"` with your real ad slot IDs
- In `public/ads.txt`, replace `pub-XXXXXXXXXXXXXXXX` with your real publisher ID

## 4) Cloudflare Pages

- Build command: `npm run build`
- Output directory: `dist`
- Framework preset: `Astro`
- Add env vars in Cloudflare Pages project settings

### Cloudflare Pages settings for this monorepo

Use these exact values in **Set up builds and deployments**:

- Project name: `fightprophet` (or keep `ml-kuda-sports-lab` if you prefer)
- Production branch: `main`
- Framework preset: `Astro`
- Build command: `npm run build`
- Build output directory: `dist`
- Root directory (advanced): `astro_adsense_starter`

#### Exact UI mapping (as shown in Cloudflare form)

- Build command field: `npm ci && npm run build`
- Build output directory field:
	- left prefix box: `/`
	- value box: `dist`
- Root directory (advanced) Path field:
	- left prefix box: `/`
	- value box: `astro_adsense_starter`

This is the preferred setup (do not combine with the repo-root fallback commands below).

If Cloudflare does not allow root directory selection and forces repo root, use this fallback:

- Build command: `cd astro_adsense_starter && npm ci && npm run build`
- Build output directory: `astro_adsense_starter/dist`

### CLI fallback (if dashboard flow is blocked)

Run from repo root:

```bash
cd astro_adsense_starter
npm ci
npm run build
npx wrangler pages project create fightprophet
npx wrangler pages deploy dist --project-name fightprophet
```

If project already exists, only deploy:

```bash
cd astro_adsense_starter
npm ci
npm run build
npx wrangler pages deploy dist --project-name fightprophet
```

## 5) Revenue flow

Ad revenue is paid by Google to your AdSense account and then to your configured payout method (bank, etc.) once account verification and payout thresholds are met.

## 6) App entry + subscription capture

The home page now includes:

- **Open Fight Prophet App** button (uses `PUBLIC_APP_URL`)
- **Fight Prophet Weekly** form (POSTs to Buttondown via `PUBLIC_BUTTONDOWN_SUBSCRIBE_URL`)

No Azure subscription endpoint is required for newsletter capture. Buttondown stores subscribers, handles unsubscribe flows, and provides the hosted archive at `https://buttondown.com/fightprophet`.

## 7) Compliance checklist

- Publish `privacy` page and link it in footer/nav
- Publish `about`, `contact`, `methodology`, `editorial-policy`, and `responsible-use` pages and link them in the footer
- Serve `ads.txt` from your site root (`/ads.txt`)
- Keep main navigation on `fightprophet.com`; link to the app as the deeper CTA, not as the only page content
- Remove copied/RSS article bodies and avoid claiming third-party news as Fight Prophet content
- Replace placeholder ad slots before enabling visible ad units; the starter suppresses slot `1234567890`
- Implement consent management where required (EEA/UK/other regulated regions)
- Avoid incentivized clicks and policy-violating ad placement



# test logs from online worker

cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab/astro_adsense_starter
npx wrangler pages deployment tail --project-name ml-kuda-sports-lab --environment production


# test build
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab/astro_adsense_starter
npm run build


# run local preview
bash -lc '
source ~/.bashrc
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab/astro_adsense_starter
trap "rm -f .dev.vars" EXIT
cat > .dev.vars <<EOF
AZURE_STORAGE_ACCOUNT=$AZURE_STORAGE_ACCOUNT
AZURE_STORAGE_KEY=$AZURE_STORAGE_KEY
AZURE_STORAGE_CONTAINER=${AZURE_STORAGE_CONTAINER:-fightprophet-dashboard}
PARQUET_PREFIX=${PARQUET_PREFIX:-mma/diamond}
PUBLIC_SITE_URL=${PUBLIC_SITE_URL:-https://fightprophet.com}
PUBLIC_APP_URL=$PUBLIC_APP_URL
PUBLIC_ADSENSE_CLIENT=$PUBLIC_ADSENSE_CLIENT
PUBLIC_ADSENSE_ENABLED=$PUBLIC_ADSENSE_ENABLED
PUBLIC_BUTTONDOWN_SUBSCRIBE_URL=${PUBLIC_BUTTONDOWN_SUBSCRIBE_URL:-https://buttondown.com/api/emails/embed-subscribe/fightprophet}
EOF
npm run build
npm run preview
'


# azure dat test

bash -lc '
source ~/.bashrc
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab/astro_adsense_starter
export AZURE_STORAGE_CONTAINER=${AZURE_STORAGE_CONTAINER:-fightprophet-dashboard}
export PARQUET_PREFIX=${PARQUET_PREFIX:-mma/diamond}
export PUBLIC_SITE_URL=${PUBLIC_SITE_URL:-https://fightprophet.com}
npm run dev
'
