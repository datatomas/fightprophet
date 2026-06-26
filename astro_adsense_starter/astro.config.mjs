import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://fightprophet.com',
  // English at /, Russian at /ru/. We translate page by page: a real
  // src/pages/ru/<page>.astro is served as Russian. The language switcher only
  // appears on pages that have a Russian version (see LOCALIZED_PATHS in
  // src/lib/i18n.ts), so links never 404. No fallback rewrite — that needs SSR
  // and emits empty files in this static build.
  i18n: {
    locales: ['en', 'es', 'pt', 'ru', 'ka'],
    defaultLocale: 'en',
    routing: {
      prefixDefaultLocale: false,
    },
  },
  integrations: [
    sitemap({
      filter: (page) => !page.includes('/fighter-profile/'),
    }),
  ],
});
