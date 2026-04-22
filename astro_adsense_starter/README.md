# Astro + AdSense Starter (Cloudflare Pages)

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
- `PUBLIC_SITE_URL=https://yourdomain.com`
- `PUBLIC_APP_URL=https://app.fightprophet.com`
- `PUBLIC_SUBSCRIBE_ENDPOINT=https://<your-azure-endpoint>`

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
- **Subscribe for updates** form (POSTs JSON to `PUBLIC_SUBSCRIBE_ENDPOINT`)

Subscription payload shape:

```json
{
	"email": "user@example.com",
	"source": "fightprophet-astro"
}
```

Recommended Azure target options for `PUBLIC_SUBSCRIBE_ENDPOINT`:

1. **Azure Function (HTTP trigger) + Table Storage** (best control)
2. **Logic App HTTP endpoint + Blob/Table sink** (fastest no-code)

Return HTTP `200`/`201` for success so the UI shows confirmation.

## 7) Compliance checklist

- Publish `privacy` page and link it in footer/nav
- Serve `ads.txt` from your site root (`/ads.txt`)
- Implement consent management where required (EEA/UK/other regulated regions)
- Avoid incentivized clicks and policy-violating ad placement
