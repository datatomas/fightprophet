// Routed UI i18n for static Astro. English is the default (served at /), with
// Spanish /es/, Portuguese /pt/, Russian /ru/, Georgian /ka/. Only the static
// "chrome" + translated pages use this; dynamic data (fighter names, odds) is
// not translated. Add a route to AVAILABLE (per-locale) + create the
// src/pages/<lang>/<page>.astro to light it up in the switcher.

export const locales = ['en', 'es', 'pt', 'ru', 'ka'] as const;
export type Lang = (typeof locales)[number];
export const defaultLang: Lang = 'en';
export const localeNames: Record<Lang, string> = { en: 'EN', es: 'ES', pt: 'PT', ru: 'RU', ka: 'KA' };
// Flag selector (matches the original "cool flag" switcher; es uses 🇨🇴 brand flag).
export const localeFlags: Record<Lang, string> = { en: '🇺🇸', es: '🇨🇴', pt: '🇧🇷', ru: '🇷🇺', ka: '🇬🇪' };
export const localeTitles: Record<Lang, string> = {
  en: 'English', es: 'Español', pt: 'Português', ru: 'Русский', ka: 'ქართული',
};

export const ui = {
  en: {
    'nav.home': 'Home & Rankings', 'nav.predictions': 'Predictions',
    'nav.fighterCards': 'Fighter Stats', 'nav.beltHolders': 'Belt Holders',
    'nav.eventsHistory': 'Events History', 'nav.fightLab': 'Fight Lab',
    'nav.bettingEducation': 'Betting Education', 'nav.about': 'About', 'nav.terms': 'Terms & Conditions',
    'footer.home': 'Home', 'footer.newsletter': 'Newsletter', 'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Editorial Policy', 'footer.responsibleUse': 'Responsible Use',
    'footer.contact': 'Contact', 'footer.privacy': 'Privacy',
    'footer.meta': '© Fight Prophet · Information and education only — not financial advice.',
    'lang.label': 'Language',
    'edu.eyebrow': 'Betting Education', 'edu.title': 'Betting Education',
    'edu.intro': 'Enter two odds, see the math live — and the exact formula behind every number Fight Prophet shows. Educational only, not betting advice.',
    'edu.formulasTitle': 'The formulas',
    'calc.heading': 'Odds calculator',
    'calc.sub': "Enter both fighters' American odds — the rest is computed live.",
    'calc.fighterA': 'Fighter A odds', 'calc.fighterB': 'Fighter B odds',
    'calc.hold': 'Sportsbook hold', 'calc.implied': 'Implied', 'calc.fair': 'Fair (no-vig)',
    'calc.fairOdds': 'Fair odds', 'calc.sideA': 'Fighter A', 'calc.sideB': 'Fighter B',
    'calc.note': "Implied includes the book's margin; fair removes it. Leave a field blank and hold/fair can't be computed.",
    'c1.title': 'American odds', 'c1.blurb': 'How a price pays on a $100 stake.',
    'c2.title': 'Implied probability', 'c2.blurb': 'The break-even win rate a price implies.',
    'c3.title': 'Sportsbook hold', 'c3.blurb': "The book's margin baked into both prices.",
    'c4.title': 'Fair probability', 'c4.blurb': 'Devigged, so both sides sum to 100%.',
    'c5.title': 'Fair odds', 'c5.blurb': 'Fair probability turned back into a price.',
    'c6.title': 'Expected value', 'c6.blurb': 'Average dollars per bet, over the long run.',
    'c7.title': 'ROI', 'c7.blurb': 'Return per dollar staked.',
    'c8.title': 'Edge', 'c8.blurb': "The model's gap over the fair price — the value signal.",
    'c9.title': 'Parlays', 'c9.blurb': 'Legs multiply, so they are much harder to hit.',
    'signalsTitle': 'How to read it',
    'c1.signal': 'Minus = favorite, plus = underdog.',
    'c2.signal': "If it's above your model's %, there's likely no value.",
    'c3.signal': 'Lower is better — under 3% is a sharp market.',
    'c4.signal': 'Compare your model to this, not the raw price.',
    'c5.signal': 'If the book pays more than fair odds, that is value.',
    'c6.signal': 'Positive = the bet has value; negative = skip it.',
    'c7.signal': 'Positive expected ROI means a long-term edge.',
    'c8.signal': 'Edge above 0 is the green light — bigger is stronger.',
    'c9.signal': 'Every leg cuts your real chance — stake smaller.',
  },
  es: {
    'nav.home': 'Inicio y Rankings', 'nav.predictions': 'Predicciones',
    'nav.fighterCards': 'Estadísticas de peleadores', 'nav.beltHolders': 'Campeones',
    'nav.eventsHistory': 'Historial de eventos', 'nav.fightLab': 'Laboratorio',
    'nav.bettingEducation': 'Educación en apuestas', 'nav.about': 'Acerca de', 'nav.terms': 'Términos y condiciones',
    'footer.home': 'Inicio', 'footer.newsletter': 'Boletín', 'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Política editorial', 'footer.responsibleUse': 'Uso responsable',
    'footer.contact': 'Contacto', 'footer.privacy': 'Privacidad',
    'footer.meta': '© Fight Prophet · Solo información y educación — no es consejo financiero.',
    'lang.label': 'Idioma',
    'edu.eyebrow': 'Educación en apuestas', 'edu.title': 'Educación en apuestas',
    'edu.intro': 'Introduce dos cuotas y mira las matemáticas en vivo — y la fórmula exacta detrás de cada número que muestra Fight Prophet. Solo educativo, no es consejo de apuestas.',
    'edu.formulasTitle': 'Las fórmulas',
    'calc.heading': 'Calculadora de cuotas',
    'calc.sub': 'Introduce las cuotas americanas de ambos peleadores — el resto se calcula en vivo.',
    'calc.fighterA': 'Cuota del peleador A', 'calc.fighterB': 'Cuota del peleador B',
    'calc.hold': 'Margen de la casa', 'calc.implied': 'Implícita', 'calc.fair': 'Justa (sin margen)',
    'calc.fairOdds': 'Cuota justa', 'calc.sideA': 'Peleador A', 'calc.sideB': 'Peleador B',
    'calc.note': 'La implícita incluye el margen de la casa; la justa lo quita. Si dejas un campo vacío, no se pueden calcular el margen ni la justa.',
    'c1.title': 'Cuotas americanas', 'c1.blurb': 'Cómo paga un precio sobre una apuesta de $100.',
    'c2.title': 'Probabilidad implícita', 'c2.blurb': 'La tasa de equilibrio que implica un precio.',
    'c3.title': 'Margen de la casa', 'c3.blurb': 'El margen de la casa incluido en ambos precios.',
    'c4.title': 'Probabilidad justa', 'c4.blurb': 'Sin vig, para que ambos lados sumen 100%.',
    'c5.title': 'Cuotas justas', 'c5.blurb': 'La probabilidad justa convertida de nuevo en precio.',
    'c6.title': 'Valor esperado', 'c6.blurb': 'Dólares promedio por apuesta, a largo plazo.',
    'c7.title': 'ROI', 'c7.blurb': 'Retorno por dólar apostado.',
    'c8.title': 'Edge', 'c8.blurb': 'La ventaja del modelo sobre el precio justo — la señal de valor.',
    'c9.title': 'Combinadas', 'c9.blurb': 'Las selecciones se multiplican, por eso son más difíciles.',
    'signalsTitle': 'Cómo leerlo',
    'c1.signal': 'Menos = favorito, más = no favorito.',
    'c2.signal': 'Si supera el % de tu modelo, probablemente no hay valor.',
    'c3.signal': 'Menos es mejor — bajo 3% es un mercado afilado.',
    'c4.signal': 'Compara tu modelo con esto, no con el precio bruto.',
    'c5.signal': 'Si la casa paga más que la cuota justa, hay valor.',
    'c6.signal': 'Positivo = la apuesta tiene valor; negativo = pásala.',
    'c7.signal': 'Un ROI esperado positivo indica ventaja a largo plazo.',
    'c8.signal': 'Edge mayor que 0 es luz verde — más grande, más fuerte.',
    'c9.signal': 'Cada selección baja tu probabilidad real — apuesta menos.',
  },
  pt: {
    'nav.home': 'Início e Rankings', 'nav.predictions': 'Previsões',
    'nav.fighterCards': 'Estatísticas de lutadores', 'nav.beltHolders': 'Campeões',
    'nav.eventsHistory': 'Histórico de eventos', 'nav.fightLab': 'Laboratório',
    'nav.bettingEducation': 'Educação em apostas', 'nav.about': 'Sobre', 'nav.terms': 'Termos e condições',
    'footer.home': 'Início', 'footer.newsletter': 'Newsletter', 'footer.rankings': 'Rankings',
    'footer.editorialPolicy': 'Política editorial', 'footer.responsibleUse': 'Uso responsável',
    'footer.contact': 'Contato', 'footer.privacy': 'Privacidade',
    'footer.meta': '© Fight Prophet · Apenas informação e educação — não é aconselhamento financeiro.',
    'lang.label': 'Idioma',
    'edu.eyebrow': 'Educação em apostas', 'edu.title': 'Educação em apostas',
    'edu.intro': 'Insira duas odds e veja a matemática ao vivo — e a fórmula exata por trás de cada número que o Fight Prophet mostra. Apenas educativo, não é conselho de apostas.',
    'edu.formulasTitle': 'As fórmulas',
    'calc.heading': 'Calculadora de odds',
    'calc.sub': 'Insira as odds americanas dos dois lutadores — o resto é calculado ao vivo.',
    'calc.fighterA': 'Odds do lutador A', 'calc.fighterB': 'Odds do lutador B',
    'calc.hold': 'Margem da casa', 'calc.implied': 'Implícita', 'calc.fair': 'Justa (sem margem)',
    'calc.fairOdds': 'Odds justas', 'calc.sideA': 'Lutador A', 'calc.sideB': 'Lutador B',
    'calc.note': 'A implícita inclui a margem da casa; a justa a remove. Se deixar um campo vazio, a margem e a justa não podem ser calculadas.',
    'c1.title': 'Odds americanas', 'c1.blurb': 'Como um preço paga sobre uma aposta de $100.',
    'c2.title': 'Probabilidade implícita', 'c2.blurb': 'A taxa de equilíbrio que um preço implica.',
    'c3.title': 'Margem da casa', 'c3.blurb': 'A margem da casa embutida nos dois preços.',
    'c4.title': 'Probabilidade justa', 'c4.blurb': 'Sem vig, para os dois lados somarem 100%.',
    'c5.title': 'Odds justas', 'c5.blurb': 'A probabilidade justa convertida de volta em preço.',
    'c6.title': 'Valor esperado', 'c6.blurb': 'Dólares médios por aposta, no longo prazo.',
    'c7.title': 'ROI', 'c7.blurb': 'Retorno por dólar apostado.',
    'c8.title': 'Edge', 'c8.blurb': 'A vantagem do modelo sobre o preço justo — o sinal de valor.',
    'c9.title': 'Múltiplas', 'c9.blurb': 'As pernas se multiplicam, por isso são mais difíceis.',
    'signalsTitle': 'Como ler',
    'c1.signal': 'Menos = favorito, mais = azarão.',
    'c2.signal': 'Se passar do % do seu modelo, provavelmente não há valor.',
    'c3.signal': 'Menos é melhor — abaixo de 3% é um mercado afiado.',
    'c4.signal': 'Compare seu modelo com isto, não com o preço bruto.',
    'c5.signal': 'Se a casa paga mais que a odd justa, há valor.',
    'c6.signal': 'Positivo = a aposta tem valor; negativo = pule.',
    'c7.signal': 'ROI esperado positivo indica vantagem no longo prazo.',
    'c8.signal': 'Edge acima de 0 é sinal verde — maior é mais forte.',
    'c9.signal': 'Cada perna reduz sua chance real — aposte menos.',
  },
  ru: {
    'nav.home': 'Главная и рейтинги', 'nav.predictions': 'Прогнозы',
    'nav.fighterCards': 'Статистика бойцов', 'nav.beltHolders': 'Чемпионы',
    'nav.eventsHistory': 'История турниров', 'nav.fightLab': 'Лаборатория боёв',
    'nav.bettingEducation': 'Обучение ставкам', 'nav.about': 'О проекте', 'nav.terms': 'Условия использования',
    'footer.home': 'Главная', 'footer.newsletter': 'Рассылка', 'footer.rankings': 'Рейтинги',
    'footer.editorialPolicy': 'Редакционная политика', 'footer.responsibleUse': 'Ответственная игра',
    'footer.contact': 'Контакты', 'footer.privacy': 'Конфиденциальность',
    'footer.meta': '© Fight Prophet · Только информация и обучение — не финансовый совет.',
    'lang.label': 'Язык',
    'edu.eyebrow': 'Обучение ставкам', 'edu.title': 'Обучение ставкам',
    'edu.intro': 'Введите два коэффициента и смотрите расчёты вживую — и точную формулу за каждым числом, которое показывает Fight Prophet. Только для образования, не совет по ставкам.',
    'edu.formulasTitle': 'Формулы',
    'calc.heading': 'Калькулятор коэффициентов',
    'calc.sub': 'Введите американские коэффициенты обоих бойцов — остальное считается вживую.',
    'calc.fighterA': 'Коэффициент бойца A', 'calc.fighterB': 'Коэффициент бойца B',
    'calc.hold': 'Маржа букмекера', 'calc.implied': 'Подразумеваемая', 'calc.fair': 'Честная (без маржи)',
    'calc.fairOdds': 'Честный коэф.', 'calc.sideA': 'Боец A', 'calc.sideB': 'Боец B',
    'calc.note': 'Подразумеваемая включает маржу букмекера; честная её убирает. Если оставить поле пустым, маржу и честную рассчитать нельзя.',
    'c1.title': 'Американские коэффициенты', 'c1.blurb': 'Сколько платит цена при ставке $100.',
    'c2.title': 'Подразумеваемая вероятность', 'c2.blurb': 'Безубыточная вероятность, заложенная в цене.',
    'c3.title': 'Маржа букмекера', 'c3.blurb': 'Маржа букмекера, встроенная в обе цены.',
    'c4.title': 'Честная вероятность', 'c4.blurb': 'Без вига — обе стороны дают 100%.',
    'c5.title': 'Честные коэффициенты', 'c5.blurb': 'Честная вероятность, переведённая обратно в цену.',
    'c6.title': 'Ожидаемая ценность', 'c6.blurb': 'Средний доход на ставку в долгую.',
    'c7.title': 'ROI', 'c7.blurb': 'Доход на каждый поставленный доллар.',
    'c8.title': 'Edge', 'c8.blurb': 'Перевес модели над честной ценой — сигнал ценности.',
    'c9.title': 'Экспрессы', 'c9.blurb': 'Ноги перемножаются — поэтому их труднее пройти.',
    'signalsTitle': 'Как читать',
    'c1.signal': 'Минус = фаворит, плюс = андердог.',
    'c2.signal': 'Если выше вероятности модели — ценности скорее нет.',
    'c3.signal': 'Меньше — лучше; ниже 3% — острый рынок.',
    'c4.signal': 'Сравнивайте модель с этим, а не с сырой ценой.',
    'c5.signal': 'Если букмекер платит больше честного — это ценность.',
    'c6.signal': 'Плюс = в ставке есть ценность; минус — пропустить.',
    'c7.signal': 'Положительный ожидаемый ROI — перевес в долгую.',
    'c8.signal': 'Edge больше 0 — зелёный свет; больше — сильнее.',
    'c9.signal': 'Каждая нога снижает реальный шанс — ставьте меньше.',
  },
  ka: {
    'nav.home': 'მთავარი და რეიტინგები', 'nav.predictions': 'პროგნოზები',
    'nav.fighterCards': 'მებრძოლების სტატისტიკა', 'nav.beltHolders': 'ჩემპიონები',
    'nav.eventsHistory': 'ღონისძიებების ისტორია', 'nav.fightLab': 'ბრძოლის ლაბორატორია',
    'nav.bettingEducation': 'ფსონების სწავლება', 'nav.about': 'შესახებ', 'nav.terms': 'წესები და პირობები',
    'footer.home': 'მთავარი', 'footer.newsletter': 'სიახლეები', 'footer.rankings': 'რეიტინგები',
    'footer.editorialPolicy': 'სარედაქციო პოლიტიკა', 'footer.responsibleUse': 'პასუხისმგებლიანი გამოყენება',
    'footer.contact': 'კონტაქტი', 'footer.privacy': 'კონფიდენციალურობა',
    'footer.meta': '© Fight Prophet · მხოლოდ ინფორმაცია და სწავლება — არ არის ფინანსური რჩევა.',
    'lang.label': 'ენა',
    'edu.eyebrow': 'ფსონების სწავლება', 'edu.title': 'ფსონების სწავლება',
    'edu.intro': 'შეიყვანეთ ორი კოეფიციენტი და ნახეთ გამოთვლა ცოცხლად — და ზუსტი ფორმულა ყველა რიცხვის უკან, რომელსაც Fight Prophet აჩვენებს. მხოლოდ საგანმანათლებლო, არ არის რჩევა ფსონებზე.',
    'edu.formulasTitle': 'ფორმულები',
    'calc.heading': 'კოეფიციენტების კალკულატორი',
    'calc.sub': 'შეიყვანეთ ორივე მებრძოლის ამერიკული კოეფიციენტი — დანარჩენი ცოცხლად გამოითვლება.',
    'calc.fighterA': 'მებრძოლი A-ს კოეფიციენტი', 'calc.fighterB': 'მებრძოლი B-ს კოეფიციენტი',
    'calc.hold': 'ბუკმეკერის მარჟა', 'calc.implied': 'ნაგულისხმევი', 'calc.fair': 'სამართლიანი (მარჟის გარეშე)',
    'calc.fairOdds': 'სამართლიანი კოეფ.', 'calc.sideA': 'მებრძოლი A', 'calc.sideB': 'მებრძოლი B',
    'calc.note': 'ნაგულისხმევი მოიცავს ბუკმეკერის მარჟას; სამართლიანი მას აშორებს. თუ ველს ცარიელს დატოვებთ, მარჟისა და სამართლიანის გამოთვლა ვერ მოხერხდება.',
    'c1.title': 'ამერიკული კოეფიციენტები', 'c1.blurb': 'როგორ იხდის ფასი $100-იან ფსონზე.',
    'c2.title': 'ნაგულისხმევი ალბათობა', 'c2.blurb': 'უტყუარი ალბათობა, რომელსაც ფასი გულისხმობს.',
    'c3.title': 'ბუკმეკერის მარჟა', 'c3.blurb': 'ბუკმეკერის მარჟა ჩაშენებული ორივე ფასში.',
    'c4.title': 'სამართლიანი ალბათობა', 'c4.blurb': 'მარჟის გარეშე — ორივე მხარე ჯამში 100%.',
    'c5.title': 'სამართლიანი კოეფიციენტები', 'c5.blurb': 'სამართლიანი ალბათობა უკან ფასად გადაყვანილი.',
    'c6.title': 'მოსალოდნელი ღირებულება', 'c6.blurb': 'საშუალო მოგება ფსონზე გრძელვადიანად.',
    'c7.title': 'ROI', 'c7.blurb': 'უკუგება ყოველ დადებულ დოლარზე.',
    'c8.title': 'Edge', 'c8.blurb': 'მოდელის უპირატესობა სამართლიან ფასთან — ღირებულების სიგნალი.',
    'c9.title': 'ექსპრესები', 'c9.blurb': 'ფეხები მრავლდება — ამიტომ უფრო რთული მოსაგებია.',
    'signalsTitle': 'როგორ წავიკითხოთ',
    'c1.signal': 'მინუსი = ფავორიტი, პლუსი = აუტსაიდერი.',
    'c2.signal': 'თუ მოდელის %-ზე მაღალია, ღირებულება ალბათ არ არის.',
    'c3.signal': 'ნაკლები სჯობს — 3%-ზე დაბალი მკვეთრი ბაზარია.',
    'c4.signal': 'შეადარეთ მოდელი ამას, და არა სუფთა ფასს.',
    'c5.signal': 'თუ ბუკმეკერი სამართლიანზე მეტს იხდის — ღირებულებაა.',
    'c6.signal': 'დადებითი = ფსონს აქვს ღირებულება; უარყოფითი = გამოტოვე.',
    'c7.signal': 'დადებითი მოსალოდნელი ROI = გრძელვადიანი უპირატესობა.',
    'c8.signal': 'Edge 0-ზე მეტი მწვანე შუქია — მეტი ძლიერია.',
    'c9.signal': 'ყოველი ფეხი ამცირებს რეალურ შანსს — დადეთ ნაკლები.',
  },
} as const;

export type UIKey = keyof (typeof ui)['en'];

// Per-route, per-locale availability. A route appears in the switcher only for
// locales whose src/pages/<lang>/<page>.astro actually exists.
export const AVAILABLE: Record<string, readonly Lang[]> = {
  '/betting-education/': ['es', 'pt', 'ru', 'ka'],
  '/contact/': ['es', 'pt', 'ru', 'ka'],
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
