import fallbackCountriesMaster from '../../../configs/countries_master.json';

export interface CountryEntry {
  canonical_name: string;
  iso2: string;
  aliases?: string[];
}

export interface CountryMasterPayload {
  countries?: CountryEntry[];
}

export interface CountryMasterIndex {
  payload: CountryMasterPayload;
  byAlias: Record<string, CountryEntry>;
}

export type RuntimeEnv = Record<string, unknown>;

const DEFAULT_BLOB_PATH = 'mma/reference/countries/countries_master.json';
const DEFAULT_CONTAINER = 'fightprophet-dashboard';
const DEFAULT_CACHE_TTL_SECONDS = 43200;
const AZURE_BLOB_API_VERSION = '2023-11-03';

let cachedPayload: CountryMasterPayload | null = null;
let cachedIndex: CountryMasterIndex | null = null;
let cacheExpiresAt = 0;

function normalizeCountryKey(value: string): string {
  return value.trim().toUpperCase();
}

function envValue(env: RuntimeEnv | undefined, key: string, fallback = ''): string {
  const value = env?.[key];
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value).trim();
  if (typeof process !== 'undefined' && process?.env && typeof process.env[key] === 'string') {
    return String(process.env[key]).trim();
  }
  return fallback;
}

function cacheTtlMilliseconds(env: RuntimeEnv | undefined): number {
  const raw = envValue(env, 'COUNTRY_MASTER_CACHE_TTL_SECONDS', String(DEFAULT_CACHE_TTL_SECONDS));
  const parsed = Number.parseInt(raw, 10);
  const seconds = Number.isFinite(parsed) ? Math.max(60, parsed) : DEFAULT_CACHE_TTL_SECONDS;
  return seconds * 1000;
}

function blobPath(env: RuntimeEnv | undefined): string {
  return envValue(env, 'COUNTRY_MASTER_BLOB_PATH', DEFAULT_BLOB_PATH).replace(/^\/+/, '');
}

function fallbackPayload(): CountryMasterPayload {
  return fallbackCountriesMaster as CountryMasterPayload;
}

function mergeCountryMasterPayloads(primary: CountryMasterPayload, fallback: CountryMasterPayload): CountryMasterPayload {
  const primaryCountries = Array.isArray(primary.countries) ? primary.countries : [];
  const fallbackCountries = Array.isArray(fallback.countries) ? fallback.countries : [];
  const countries: CountryEntry[] = primaryCountries.map((entry) => ({ ...entry }));
  const seen = new Set<string>();

  for (const entry of countries) {
    const iso2 = String(entry.iso2 ?? '').trim().toUpperCase();
    const name = String(entry.canonical_name ?? '').trim().toUpperCase();
    if (iso2) seen.add(`iso2:${iso2}`);
    if (name) seen.add(`name:${name}`);
  }

  for (const entry of fallbackCountries) {
    const iso2 = String(entry.iso2 ?? '').trim().toUpperCase();
    const name = String(entry.canonical_name ?? '').trim().toUpperCase();
    const keys = [
      iso2 ? `iso2:${iso2}` : '',
      name ? `name:${name}` : '',
    ].filter(Boolean);
    if (keys.some((key) => seen.has(key))) continue;
    countries.push({ ...entry });
    for (const key of keys) seen.add(key);
  }

  countries.sort((a, b) => String(a.canonical_name ?? '').localeCompare(String(b.canonical_name ?? '')));
  return { ...primary, countries };
}

function isCountryMasterPayload(value: unknown): value is CountryMasterPayload {
  return !!value && typeof value === 'object' && Array.isArray((value as CountryMasterPayload).countries);
}

function base64ToBytes(value: string): Uint8Array {
  if (typeof atob === 'function') {
    const binary = atob(value);
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
  }
  return Uint8Array.from(Buffer.from(value, 'base64'));
}

function bytesToBase64(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  if (typeof btoa === 'function') {
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  return Buffer.from(bytes).toString('base64');
}

async function azureSharedKeySignature(accountKey: string, stringToSign: string): Promise<string> {
  const keyBytes = base64ToBytes(accountKey);
  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    keyBytes,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(stringToSign));
  return bytesToBase64(signature);
}

async function fetchCountryMasterFromAzure(env: RuntimeEnv | undefined): Promise<CountryMasterPayload | null> {
  const account = envValue(env, 'AZURE_STORAGE_ACCOUNT');
  const accountKey = envValue(env, 'AZURE_STORAGE_KEY');
  const container = envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER);
  const path = blobPath(env);
  if (!account || !accountKey || !container || !path) {
    return null;
  }

  const requestDate = new Date().toUTCString();
  const canonicalHeaders = `x-ms-date:${requestDate}\nx-ms-version:${AZURE_BLOB_API_VERSION}\n`;
  const canonicalResource = `/${account}/${container}/${path}`;
  const stringToSign = `GET\n\n\n\n\n\n\n\n\n\n\n\n${canonicalHeaders}${canonicalResource}`;
  const signature = await azureSharedKeySignature(accountKey, stringToSign);
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  const url = `https://${account}.blob.core.windows.net/${container}/${encodedPath}`;

  const response = await fetch(url, {
    headers: {
      Authorization: `SharedKey ${account}:${signature}`,
      'x-ms-date': requestDate,
      'x-ms-version': AZURE_BLOB_API_VERSION,
    },
  });
  if (!response.ok) {
    return null;
  }

  const payload = await response.json();
  if (isCountryMasterPayload(payload)) {
    return payload;
  }
  return null;
}

export async function getCountryMasterPayload(env?: RuntimeEnv): Promise<CountryMasterPayload> {
  const now = Date.now();
  if (cachedPayload && now < cacheExpiresAt) {
    return cachedPayload;
  }

  let payload: CountryMasterPayload | null = null;
  try {
    payload = await fetchCountryMasterFromAzure(env);
  } catch {
    payload = null;
  }

  const fallback = fallbackPayload();
  cachedPayload = payload ? mergeCountryMasterPayloads(payload, fallback) : fallback;
  cachedIndex = null;
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedPayload;
}

export async function getCountryMasterIndex(env?: RuntimeEnv): Promise<CountryMasterIndex> {
  if (cachedIndex && Date.now() < cacheExpiresAt) {
    return cachedIndex;
  }

  const payload = await getCountryMasterPayload(env);
  const byAlias: Record<string, CountryEntry> = {};
  const countries = Array.isArray(payload.countries) ? payload.countries : [];

  for (const entry of countries) {
    const canonicalName = String(entry.canonical_name ?? '').trim();
    const iso2 = String(entry.iso2 ?? '').trim().toUpperCase();
    const aliases = Array.isArray(entry.aliases) ? entry.aliases : [];
    const record: CountryEntry = {
      canonical_name: canonicalName,
      iso2,
      aliases,
    };
    for (const alias of [canonicalName, iso2, ...aliases]) {
      const key = normalizeCountryKey(String(alias ?? ''));
      if (key) {
        byAlias[key] = record;
      }
    }
  }

  cachedIndex = { payload, byAlias };
  return cachedIndex;
}

export function runtimeEnvFromLocals(locals: Record<string, unknown> | undefined): RuntimeEnv | undefined {
  const merged: RuntimeEnv = {};

  if (typeof process !== 'undefined' && process?.env && typeof process.env === 'object') {
    Object.assign(merged, process.env as RuntimeEnv);
  }

  const runtime = locals?.runtime;
  if (runtime && typeof runtime === 'object') {
    try {
      const env = (runtime as { env?: RuntimeEnv }).env;
      if (env && typeof env === 'object') {
        Object.assign(merged, env);
      }
    } catch {
      // Astro v6 Cloudflare intentionally removes locals.runtime.env.
    }
  }

  return Object.keys(merged).length > 0 ? merged : undefined;
}
