import { defineConfig } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  output: 'server',
  adapter: cloudflare(),
  site: 'https://fightprophet.com',
  integrations: [
    sitemap({
      filter: (page) => !page.includes('/fighter-profile/'),
    }),
  ],
});
