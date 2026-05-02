export function buildAppPageUrl(appUrl: string, slug: string, extraParams: Record<string, string> = {}): string {
  const safeAppUrl = (appUrl || '').trim();
  const safeSlug = (slug || '').trim();

  if (!safeAppUrl) {
    return safeSlug ? `?page=${encodeURIComponent(safeSlug)}` : '?';
  }

  try {
    const url = new URL(safeAppUrl);
    if (safeSlug) url.searchParams.set('page', safeSlug);
    for (const [key, value] of Object.entries(extraParams)) {
      if (value) url.searchParams.set(key, value);
    }
    return url.toString();
  } catch {
    const hashIndex = safeAppUrl.indexOf('#');
    const hash = hashIndex >= 0 ? safeAppUrl.slice(hashIndex) : '';
    const baseWithoutHash = hashIndex >= 0 ? safeAppUrl.slice(0, hashIndex) : safeAppUrl;
    const queryIndex = baseWithoutHash.indexOf('?');
    const pathname = queryIndex >= 0 ? baseWithoutHash.slice(0, queryIndex) : baseWithoutHash;
    const query = queryIndex >= 0 ? baseWithoutHash.slice(queryIndex + 1) : '';
    const params = new URLSearchParams(query);

    if (safeSlug) params.set('page', safeSlug);
    for (const [key, value] of Object.entries(extraParams)) {
      if (value) params.set(key, value);
    }

    const search = params.toString();
    return `${pathname}${search ? `?${search}` : ''}${hash}`;
  }
}
