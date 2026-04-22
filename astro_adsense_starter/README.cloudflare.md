# Cloudflare deploy guide (Astro)

This project lives in a subfolder:

- Root directory: `astro_adsense_starter`

Use this guide if Cloudflare only shows the **Worker from repo** wizard and does not let you pick a subdirectory directly in Pages.

## Option A (recommended): Cloudflare Pages flow

1. Go to **Workers & Pages**.
2. Click **Create application**.
3. Choose **Pages** (not Worker template).
4. Connect GitHub repo: `ml_kuda_sports_lab`.
5. Build settings:
   - Framework preset: `Astro`
   - Root directory: `astro_adsense_starter`
   - Build command: `npm run build`
   - Output directory: `dist`

## Option B: Worker-from-repo fallback

If you can only use the Worker wizard, set these fields:

- Build command:

```bash
cd astro_adsense_starter && npm ci && npm run build
```

- Deploy command:

```bash
cd astro_adsense_starter && npx wrangler pages deploy dist --project-name ml-kuda-sports-lab
```

- Non-production branch deploy command: leave empty
- Path: `/`
- Variables: none required initially

## Notes

- `wrangler.toml` is included in this folder to keep configuration close to the Astro app.
- Add environment variables only when your app needs them.
- If deploy succeeds but site does not update, trigger a redeploy and clear CDN cache for the project.
