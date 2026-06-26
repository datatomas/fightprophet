// Routed UI i18n for static Astro. English is the default (served at /), with
// Spanish at /es/, Portuguese at /pt/, Russian at /ru/. Only the static "chrome"
// + translated pages use this; dynamic data (fighter names, odds) is not
// translated. We translate page by page — add a route to LOCALIZED_PATHS and a
// src/pages/<lang>/<page>.astro when its translation exists.

export const locales = ['en', 'es', 'pt', 'ru'] as const;
export type Lang = (typeof locales)[number];
export const defaultLang: Lang = 'en';
export const localeNames: Record<Lang, string> = { en: 'EN', es: 'ES', pt: 'PT', ru: 'RU' };

export const ui = {
  en: {
    'nav.home': 'Home & Rankings',
    'nav.predictions': 'Predictions',
    'nav.fighterCards': 'Fighter Cards',
    'nav.beltHolders': 'Belt Holders',
    'nav.eventsHistory': 'Events History',
    'nav.fightLab': 'Fight Lab',
    'nav.bettingEducation': 'Betting Education',
    'nav.about': 'About',
    'nav.terms': 'Terms & Conditions',
    'footer.home': 'Home',
    'footer.newsletter': 'Newsletter',
    'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Editorial Policy',
    'footer.responsibleUse': 'Responsible Use',
    'footer.contact': 'Contact',
    'footer.privacy': 'Privacy',
    'footer.meta': '© Fight Prophet · Information and education only — not financial advice.',
    'lang.label': 'Language',
  },
  es: {
    'nav.home': 'Inicio y Rankings',
    'nav.predictions': 'Predicciones',
    'nav.fighterCards': 'Fichas de peleadores',
    'nav.beltHolders': 'Campeones',
    'nav.eventsHistory': 'Historial de eventos',
    'nav.fightLab': 'Laboratorio',
    'nav.bettingEducation': 'Educación en apuestas',
    'nav.about': 'Acerca de',
    'nav.terms': 'Términos y condiciones',
    'footer.home': 'Inicio',
    'footer.newsletter': 'Boletín',
    'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Política editorial',
    'footer.responsibleUse': 'Uso responsable',
    'footer.contact': 'Contacto',
    'footer.privacy': 'Privacidad',
    'footer.meta': '© Fight Prophet · Solo información y educación — no es consejo financiero.',
    'lang.label': 'Idioma',
  },
  pt: {
    'nav.home': 'Início e Rankings',
    'nav.predictions': 'Previsões',
    'nav.fighterCards': 'Cartões de lutadores',
    'nav.beltHolders': 'Campeões',
    'nav.eventsHistory': 'Histórico de eventos',
    'nav.fightLab': 'Laboratório',
    'nav.bettingEducation': 'Educação em apostas',
    'nav.about': 'Sobre',
    'nav.terms': 'Termos e condições',
    'footer.home': 'Início',
    'footer.newsletter': 'Newsletter',
    'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Política editorial',
    'footer.responsibleUse': 'Uso responsável',
    'footer.contact': 'Contato',
    'footer.privacy': 'Privacidade',
    'footer.meta': '© Fight Prophet · Apenas informação e educação — não é aconselhamento financeiro.',
    'lang.label': 'Idioma',
  },
  ru: {
    'nav.home': 'Главная и рейтинги',
    'nav.predictions': 'Прогнозы',
    'nav.fighterCards': 'Карточки бойцов',
    'nav.beltHolders': 'Чемпионы',
    'nav.eventsHistory': 'История турниров',
    'nav.fightLab': 'Лаборатория боёв',
    'nav.bettingEducation': 'Обучение ставкам',
    'nav.about': 'О проекте',
    'nav.terms': 'Условия использования',
    'footer.home': 'Главная',
    'footer.newsletter': 'Рассылка',
    'footer.rankings': 'Рейтинги',
    'footer.editorialPolicy': 'Редакционная политика',
    'footer.responsibleUse': 'Ответственная игра',
    'footer.contact': 'Контакты',
    'footer.privacy': 'Конфиденциальность',
    'footer.meta': '© Fight Prophet · Только информация и обучение — не финансовый совет.',
    'lang.label': 'Язык',
  },
} as const;

export type UIKey = keyof (typeof ui)['en'];

// Routes that have real src/pages/<lang>/<page>.astro translations for every
// non-default locale. Add a route here once its es/pt/ru pages exist.
export const LOCALIZED_PATHS = new Set<string>(['/betting-education/']);

export function isLocalized(strippedPath: string): boolean {
  return LOCALIZED_PATHS.has(strippedPath);
}

const LOCALE_PREFIX_RE = /^\/(es|pt|ru)(?=\/|$)/;

/** Path with any locale prefix removed, normalised with a trailing slash. */
export function stripLocale(pathname: string): string {
  let p = pathname.replace(LOCALE_PREFIX_RE, '') || '/';
  if (p !== '/' && !p.endsWith('/')) p += '/';
  return p;
}

/** Locale from the URL path: /es|/pt|/ru -> that lang, else 'en'. */
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
  if (!isLocalized(base)) return clean; // no translated page → keep English URL
  return `/${lang}${clean === '/' ? '/' : clean}`;
}

/** The same page in the given locale, for the language switcher. */
export function switchLocalePath(url: URL, lang: Lang): string {
  return localizeUrl(stripLocale(url.pathname), lang);
}
