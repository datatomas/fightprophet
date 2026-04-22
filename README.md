# Fight Prophet Community Repo

Fight Prophet is an MMA analytics project focused on making fight data easier to explore, discuss, and build on.

This repository is the community-facing layer of the project. It includes the public website, the Streamlit app, and selected data-prep and ranking logic that are useful for contributors, designers, analysts, and curious MMA fans who want to help shape the product.

## Why this repo exists

We want Fight Prophet to feel open, collaborative, and useful even if not every production detail is public.

This repo is meant to help the community:

- improve the user experience
- contribute copy, layouts, and content
- explore rankings and feature ideas
- suggest better ways to present MMA data
- build trust around the product direction

## What is included

The current public surface area includes:

- the Astro site in `astro_adsense_starter/`
- the Streamlit app in `src/ml_kuda_sports_lab/front_end/`
- selected `silver` data-shaping logic
- selected `gold` ranking and feature logic
- frontend docs and deployment-friendly config files

## What is intentionally not the focus here

This repository is centered on the community/product layer, not every internal production detail.

That means the public README avoids low-level server operations, private deployment routines, and model-training internals that are better kept out of the main onboarding path.

## Project layout

```text
astro_adsense_starter/                  Astro marketing and landing site
src/ml_kuda_sports_lab/front_end/       Streamlit dashboard and frontend docs
src/ml_kuda_sports_lab/etl/silver/      Public data-shaping layer
src/ml_kuda_sports_lab/etl/gold/        Public ranking + feature logic
datasets/manual_overrides/              Manual override examples and notes
.github/workflows/                      CI and open-source sync workflows
docker-compose.yml                      Compose file for container-based runs
```

## Quick start

### 1. Clone the repo

```bash
git clone https://github.com/datatomas/fight_prophet.git
cd fight_prophet
```

### 2. Run the Astro site

```bash
cd astro_adsense_starter
npm ci
npm run dev
```

Useful files:

- `astro_adsense_starter/src/pages/`
- `astro_adsense_starter/src/components/`
- `astro_adsense_starter/public/`
- `astro_adsense_starter/README.md`

### 3. Run the Streamlit app

From repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.front.txt
streamlit run src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py
```

If you want more frontend context, see:

- [streamlit.md](src/ml_kuda_sports_lab/front_end/streamlit.md)
- `docker-compose.yml`

## Good first contribution areas

- improve navigation, layout, and mobile polish in Astro
- improve usability in Streamlit
- improve public docs and onboarding
- improve labels, explanations, and MMA terminology for casual fans
- improve ranking presentation and feature transparency
- propose new public pages, content sections, or comparison views

## Design direction

Fight Prophet should feel:

- clear rather than overly academic
- sharp and modern without looking generic
- useful to serious fans, but readable for newcomers
- opinionated in presentation, careful in claims

## Community principles

We want contributors who care about:

- clarity
- honesty
- thoughtful product design
- MMA domain curiosity
- respectful collaboration

If you open an issue or PR, context is appreciated. Explain what feels broken, confusing, misleading, or worth improving.

## Public roadmap ideas

- cleaner event-by-event prediction browsing
- better fighter profile storytelling
- richer public rankings pages
- clearer methodology pages
- community-requested comparison tools
- more polished Astro content and landing flows

## Notes for contributors

- keep secrets and environment-specific values out of commits
- avoid committing generated build output unless explicitly needed
- prefer small, focused pull requests
- optimize for readability and maintainability

## Repo docs

- [datasets/manual_overrides/readme.md](datasets/manual_overrides/readme.md)
- [streamlit.md](src/ml_kuda_sports_lab/front_end/streamlit.md)

## Vision

Fight Prophet is not just a picks page.

The long-term goal is to build a strong MMA analytics product with a real community around it: fans, builders, designers, analysts, and contributors who want better tools, better presentation, and a better conversation around fights.
