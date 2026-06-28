// Routed UI i18n for static Astro. English is the default (served at /), with
// Spanish /es/, Portuguese /pt/, Russian /ru/, Georgian /ka/. Only the static
// "chrome" + translated pages use this; dynamic data (fighter names, odds) is
// not translated. Add a route to AVAILABLE (per-locale) + create the
// src/pages/<lang>/<page>.astro to light it up in the switcher.

import en from './i18n/en.json';
import es from './i18n/es.json';
import pt from './i18n/pt.json';
import ru from './i18n/ru.json';
import ka from './i18n/ka.json';

export const locales = ['en', 'es', 'pt', 'ru', 'ka'] as const;
export type Lang = (typeof locales)[number];
export const defaultLang: Lang = 'en';
export const localeNames: Record<Lang, string> = { en: 'EN', es: 'ES', pt: 'PT', ru: 'RU', ka: 'KA' };
// Flag selector (matches the original "cool flag" switcher; es uses 🇨🇴 brand flag).
export const localeFlags: Record<Lang, string> = { en: '🇺🇸', es: '🇨🇴', pt: '🇧🇷', ru: '🇷🇺', ka: '🇬🇪' };
export const localeTitles: Record<Lang, string> = {
  en: 'English', es: 'Español', pt: 'Português', ru: 'Русский', ka: 'ქართული',
};

// Dictionaries live in src/lib/i18n/<lang>.json. English is the source of
// truth; translations are auto-filled by scripts/i18n_autotranslate.mjs.
export const ui = { en, es, pt, ru, ka } as const;

export type UIKey = keyof (typeof ui)['en'];

// Per-route, per-locale availability. A route appears in the switcher only for
// locales whose src/pages/<lang>/<page>.astro actually exists.
export const AVAILABLE: Record<string, readonly Lang[]> = {
  '/betting-education/': ['es', 'pt', 'ru', 'ka'],
  '/contact/': ['es', 'pt', 'ru', 'ka'],
  '/responsible-use/': ['es', 'pt', 'ru', 'ka'],
};

export function availableLocales(strippedPath: string): readonly Lang[] {
  return AVAILABLE[strippedPath] ?? [];
}
export function isLocalized(strippedPath: string): boolean {
  return strippedPath in AVAILABLE;
}

const LOCALE_PREFIX_RE = /^\/(es|pt|ru|ka)(?=\/|$)/;

/** Path with any locale prefix removed, normalised with a trailing slash. */
export function stripLocale(pathname: string): string {
  let p = pathname.replace(LOCALE_PREFIX_RE, '') || '/';
  if (p !== '/' && !p.endsWith('/')) p += '/';
  return p;
}

/** Locale from the URL path: /es|/pt|/ru|/ka -> that lang, else 'en'. */
export function getLangFromUrl(url: URL): Lang {
  const seg = url.pathname.split('/')[1];
  return (locales as readonly string[]).includes(seg) && seg !== 'en' ? (seg as Lang) : 'en';
}

/** Returns a t(key) that falls back to English, then to the key itself. */
export function useTranslations(lang: Lang) {
  return function t(key: UIKey): string {
    return (ui[lang] as Record<string, string>)[key] ?? ui[defaultLang][key] ?? key;
  };
}

/** Prefix an app path with the locale, but only when a translated page exists
 * for it — otherwise stay on the English URL so nav never points to a 404. */
export function localizeUrl(path: string, lang: Lang): string {
  const clean = '/' + path.replace(/^\/+/, '');
  if (lang === defaultLang) return clean;
  const base = clean.split('#')[0];
  if (!availableLocales(base).includes(lang)) return clean;
  return `/${lang}${clean === '/' ? '/' : clean}`;
}

/** The same page in the given locale, for the language switcher. */
export function switchLocalePath(url: URL, lang: Lang): string {
  return localizeUrl(stripLocale(url.pathname), lang);
}
