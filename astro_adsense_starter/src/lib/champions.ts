import { parquetReadObjects } from 'hyparquet';
import { compressors } from 'hyparquet-compressors';
import type { CountryEntry, RuntimeEnv } from './country-master';
import { getCountryMasterIndex } from './country-master';

export interface ChampionRecord {
  fighter_id?: string;
  name: string;
  country?: string;
  country_iso2?: string;
  weight_class?: string;
  is_champion?: boolean;
  title_defenses?: number;
  wins?: number;
  losses?: number;
  draws?: number;
  win_streak?: number;
  loss_streak?: number;
  longest_win_streak?: number;
  longest_loss_streak?: number;
  finish_rate?: number | null;
  sub_rate?: number | null;
  ko_rate?: number | null;
  fighter_status?: 'active' | 'inactive';
}

export interface RankedFighterRecord extends ChampionRecord {
  rank?: number | null;
  points?: number | null;
  global_rank?: number | null;
  global_points?: number | null;
  normalized_global_score?: number | null;
  last_fight_date?: string;
}

export interface HomepageRankingsDebug {
  hasAzureAccount: boolean;
  hasAzureKey: boolean;
  container: string;
  prefix: string;
  resolvedBase: string;
  blobPath: string;
  usedAzureRuntime: boolean;
  fetchedBlob: boolean;
  fighterCount: number;
  note: string;
}

const DEFAULT_CONTAINER = 'fightprophet-dashboard';
const DEFAULT_PREFIX = 'mma/diamond';
const DEFAULT_CACHE_TTL_SECONDS = 43200;
const AZURE_BLOB_API_VERSION = '2023-11-03';
const HOMEPAGE_RANKINGS_WALL_FILENAME = 'rankings_wall.json';
const FOLDER_BELT_HOLDERS = 'dashboard_belt_holders';
const FOLDER_FIGHTER_PROFILES = 'dashboard_fighter_profiles';
const FOLDER_RANKINGS = 'dashboard_rankings';

let cachedChampions: ChampionRecord[] | null = null;
let cachedRankedFighters: RankedFighterRecord[] | null = null;
let cachedAllRankedFighters: RankedFighterRecord[] | null = null;
let cachedHomepageGoats: RankedFighterRecord[] | null = null;
let cacheExpiresAt = 0;
let cacheAllRankedExpiresAt = 0;
let lastHomepageRankingsDebug: HomepageRankingsDebug | null = null;

const DIVISION_ORDER = [
  'Flyweight',
  'Bantamweight',
  'Featherweight',
  'Lightweight',
  'Welterweight',
  'Middleweight',
  'Light Heavyweight',
  'Heavyweight',
  "Women's Strawweight",
  "Women's Flyweight",
  "Women's Bantamweight",
  "Women's Featherweight",
];

function divisionSortIndex(value: string): number {
  const idx = DIVISION_ORDER.indexOf(value);
  return idx === -1 ? DIVISION_ORDER.length : idx;
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

function championsCacheTtlMilliseconds(env: RuntimeEnv | undefined): number {
  const raw = envValue(env, 'CHAMPIONS_CACHE_TTL_SECONDS', String(DEFAULT_CACHE_TTL_SECONDS));
  const parsed = Number.parseInt(raw, 10);
  const seconds = Number.isFinite(parsed) ? Math.max(60, parsed) : DEFAULT_CACHE_TTL_SECONDS;
  return seconds * 1000;
}

function resolveBase(env: RuntimeEnv | undefined): string {
  const explicit = envValue(env, 'PARQUET_BASE_URI');
  if (explicit) return explicit.replace(/\/+$/, '');
  const azureBase = envValue(env, 'AZURE_PARQUET_BASE_URI');
  if (azureBase) return azureBase.replace(/\/+$/, '');
  if (envValue(env, 'AZURE_STORAGE_ACCOUNT') && envValue(env, 'AZURE_STORAGE_KEY')) {
    return `az://${envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER)}`;
  }
  return '';
}

function resolvePrefix(env: RuntimeEnv | undefined): string {
  return envValue(env, 'PARQUET_PREFIX', DEFAULT_PREFIX).replace(/^\/+|\/+$/g, '');
}

function applyPrefix(folder: string, prefix: string): string {
  return prefix ? `${prefix}/${folder}` : folder;
}

function homepageReferencePrefix(prefix: string): string {
  const parts = prefix.split('/').filter(Boolean);
  const root = parts[0] || '';
  return root ? `${root}/reference/homepage` : 'reference/homepage';
}

function homepageBlobPath(env: RuntimeEnv | undefined, filename: string): string {
  const explicit = envValue(env, 'HOMEPAGE_RANKINGS_WALL_BLOB_PATH');
  if (filename === HOMEPAGE_RANKINGS_WALL_FILENAME && explicit) return explicit.replace(/^\/+/, '');
  return `${homepageReferencePrefix(resolvePrefix(env))}/${filename}`;
}

function isAzure(base: string): boolean {
  return base.startsWith('az://') || base.startsWith('azure://');
}

function homepageRankingsDebug(
  env: RuntimeEnv | undefined,
  overrides: Partial<HomepageRankingsDebug> = {},
): HomepageRankingsDebug {
  const resolvedBase = resolveBase(env);
  return {
    hasAzureAccount: !!envValue(env, 'AZURE_STORAGE_ACCOUNT'),
    hasAzureKey: !!envValue(env, 'AZURE_STORAGE_KEY'),
    container: envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER),
    prefix: resolvePrefix(env),
    resolvedBase,
    blobPath: homepageBlobPath(env, HOMEPAGE_RANKINGS_WALL_FILENAME),
    usedAzureRuntime: isAzure(resolvedBase),
    fetchedBlob: false,
    fighterCount: 0,
    note: '',
    ...overrides,
  };
}

export function getHomepageRankingsDebug(env?: RuntimeEnv): HomepageRankingsDebug {
  return lastHomepageRankingsDebug ?? homepageRankingsDebug(env);
}

function normalizeCountryKey(value: object): string {
  return String(value ?? '').trim().toUpperCase();
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

async function azureRequestHeaders(
  env: RuntimeEnv | undefined,
  blobPath: string,
  verb = 'GET',
): Promise<Record<string, string> | null> {
  const account = envValue(env, 'AZURE_STORAGE_ACCOUNT');
  const accountKey = envValue(env, 'AZURE_STORAGE_KEY');
  const container = envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER);
  if (!account || !accountKey || !container || !blobPath) {
    return null;
  }

  const requestDate = new Date().toUTCString();
  const canonicalHeaders = `x-ms-date:${requestDate}\nx-ms-version:${AZURE_BLOB_API_VERSION}\n`;
  const encodedBlobPath = blobPath.split('/').map(encodeURIComponent).join('/');
  const canonicalResource = `/${account}/${container}/${encodedBlobPath}`;
  const stringToSign = `${verb}\n\n\n\n\n\n\n\n\n\n\n\n${canonicalHeaders}${canonicalResource}`;
  const signature = await azureSharedKeySignature(accountKey, stringToSign);

  return {
    Authorization: `SharedKey ${account}:${signature}`,
    'x-ms-date': requestDate,
    'x-ms-version': AZURE_BLOB_API_VERSION,
  };
}

function azureBlobUrl(env: RuntimeEnv | undefined, blobPath: string): string | null {
  const account = envValue(env, 'AZURE_STORAGE_ACCOUNT');
  const container = envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER);
  if (!account || !container || !blobPath) return null;
  const encoded = blobPath.split('/').map(encodeURIComponent).join('/');
  return `https://${account}.blob.core.windows.net/${container}/${encoded}`;
}

async function fetchAzureJson(env: RuntimeEnv | undefined, blobPath: string): Promise<Record<string, any> | null> {
  const headers = await azureRequestHeaders(env, blobPath);
  const url = azureBlobUrl(env, blobPath);
  if (!headers || !url) {
    console.warn('[fightprophet] Azure JSON fetch skipped: missing storage config', {
      blobPath,
      hasAzureAccount: !!envValue(env, 'AZURE_STORAGE_ACCOUNT'),
      hasAzureKey: !!envValue(env, 'AZURE_STORAGE_KEY'),
      container: envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER),
    });
    return null;
  }
  const response = await fetch(url, { headers });
  if (!response.ok) {
    console.warn('[fightprophet] Azure JSON fetch failed', {
      blobPath,
      status: response.status,
      statusText: response.statusText,
    });
    return null;
  }
  const payload = await response.json();
  if (payload && typeof payload === 'object') return payload as Record<string, any>;
  return null;
}

async function resolveLatestAzurePath(env: RuntimeEnv | undefined, folder: string): Promise<string | null> {
  const latest = await fetchAzureJson(env, `${folder}/LATEST.json`);
  const path = latest?.path;
  return typeof path === 'string' && path.trim() ? path.trim() : null;
}

function azureBlobPathFromRemotePath(env: RuntimeEnv | undefined, remotePath: string): string {
  const container = envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER);
  return remotePath
    .replace(`az://${container}/`, '')
    .replace(`azure://${container}/`, '');
}

async function listAzureBlobPaths(env: RuntimeEnv | undefined, prefix: string): Promise<string[]> {
  const account = envValue(env, 'AZURE_STORAGE_ACCOUNT');
  const accountKey = envValue(env, 'AZURE_STORAGE_KEY');
  const container = envValue(env, 'AZURE_STORAGE_CONTAINER', DEFAULT_CONTAINER);
  if (!account || !accountKey || !container || !prefix) {
    return [];
  }

  const collected = new Set<string>();
  let marker = '';

  do {
    const requestDate = new Date().toUTCString();
    const canonicalHeaders = `x-ms-date:${requestDate}\nx-ms-version:${AZURE_BLOB_API_VERSION}\n`;
    const queryPairs = [
      ['comp', 'list'],
      ['prefix', prefix],
      ['restype', 'container'],
      ...(marker ? [['marker', marker]] : []),
    ].sort(([a], [b]) => a.localeCompare(b));
    const canonicalQuery = queryPairs.map(([key, value]) => `${key}:${value}`).join('\n');
    const stringToSign = `GET\n\n\n\n\n\n\n\n\n\n\n\n${canonicalHeaders}/${account}/${container}\n${canonicalQuery}`;
    const signature = await azureSharedKeySignature(accountKey, stringToSign);

    const search = new URLSearchParams({
      restype: 'container',
      comp: 'list',
      prefix,
    });
    if (marker) search.set('marker', marker);

    const url = `https://${account}.blob.core.windows.net/${container}?${search.toString()}`;
    const response = await fetch(url, {
      headers: {
        Authorization: `SharedKey ${account}:${signature}`,
        'x-ms-date': requestDate,
        'x-ms-version': AZURE_BLOB_API_VERSION,
      },
    });
    if (!response.ok) {
      console.error('azure blob list failed', {
        status: response.status,
        statusText: response.statusText,
        prefix,
        marker,
      });
      return [...collected];
    }

    const xml = await response.text();
    for (const match of xml.matchAll(/<Name>([^<]+)<\/Name>/g)) {
      const name = String(match[1] || '').trim();
      if (name) collected.add(name);
    }
    marker = xml.match(/<NextMarker>([^<]*)<\/NextMarker>/)?.[1]?.trim() || '';
  } while (marker);

  return [...collected];
}

async function expandAzureParquetBlobPaths(
  env: RuntimeEnv | undefined,
  remotePath: string,
): Promise<string[]> {
  const blobPath = azureBlobPathFromRemotePath(env, remotePath);
  if (!blobPath.includes('*')) {
    return [blobPath];
  }

  const prefix = blobPath.split('*')[0] || '';
  const listed = await listAzureBlobPaths(env, prefix);
  return listed.filter((path) => path.endsWith('.parquet')).sort();
}

async function readParquetRowsAzure(
  env: RuntimeEnv | undefined,
  remotePath: string,
  columns: string[],
): Promise<Record<string, any>[]> {
  const blobPaths = await expandAzureParquetBlobPaths(env, remotePath);
  const rows: Record<string, any>[] = [];

  for (const blobPath of blobPaths) {
    const headers = await azureRequestHeaders(env, blobPath);
    const url = azureBlobUrl(env, blobPath);
    if (!headers || !url) continue;

    const response = await fetch(url, { headers });
    if (!response.ok) {
      console.error('azure parquet fetch failed', {
        status: response.status,
        statusText: response.statusText,
        blobPath,
        columns,
      });
      continue;
    }

    const file = await response.arrayBuffer();
    const fileRows = await parquetReadObjects({
      file,
      columns,
      compressors,
    });
    rows.push(...fileRows as Record<string, any>[]);
  }

  return rows;
}

function toBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (value == null) return false;
  if (typeof value === 'number') return Number(value) === 1;
  return ['1', 'true', 't', 'yes', 'y'].includes(String(value).trim().toLowerCase());
}

function coerceInt(value: unknown): number | null {
  if (value == null || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? Math.trunc(num) : null;
}

function coerceNum(value: unknown): number | null {
  if (value == null || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function coercePct(value: unknown): number | null {
  if (value == null || value === '') return null;
  const num = Number(value);
  if (!Number.isFinite(num)) return null;
  const pct = num <= 1 ? num * 100 : num;
  return Math.round(pct * 10) / 10;
}

function coerceStr(value: unknown): string {
  return value == null ? '' : String(value).trim();
}

function coerceDate(value: unknown): string {
  if (value == null || value === '') return '';
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(0, 10);
}

function normalizeFighterStatus(statusValue: unknown): 'active' | 'inactive' {
  return coerceStr(statusValue).toLowerCase() === 'inactive' ? 'inactive' : 'active';
}

function sortRankedFighters(cards: RankedFighterRecord[]): RankedFighterRecord[] {
  return [...cards].sort((a, b) => {
    const aIdx = divisionSortIndex(a.weight_class || '');
    const bIdx = divisionSortIndex(b.weight_class || '');
    if (aIdx !== bIdx) return aIdx - bIdx;
    return (a.rank ?? 999) - (b.rank ?? 999);
  });
}

function normalizeRankedFighterRows(rows: unknown[]): RankedFighterRecord[] {
  return rows
    .map((row) => {
      const record = row as Record<string, unknown>;
      const last_fight_date = coerceDate(record.last_fight_date);
      const fighter_status = normalizeFighterStatus(record.fighter_status);
      return {
        fighter_id: coerceStr(record.fighter_id),
        name: coerceStr(record.name),
        country: coerceStr(record.country),
        country_iso2: coerceStr(record.country_iso2),
        weight_class: coerceStr(record.weight_class),
        is_champion: toBool(record.is_champion),
        wins: coerceInt(record.wins) ?? 0,
        losses: coerceInt(record.losses) ?? 0,
        draws: coerceInt(record.draws) ?? 0,
        win_streak: coerceInt(record.win_streak) ?? 0,
        loss_streak: coerceInt(record.loss_streak) ?? 0,
        longest_win_streak: coerceInt(record.longest_win_streak) ?? 0,
        longest_loss_streak: coerceInt(record.longest_loss_streak) ?? 0,
        finish_rate: coercePct(record.finish_rate),
        sub_rate: coercePct(record.sub_rate),
        ko_rate: coercePct(record.ko_rate),
        rank: coerceInt(record.rank),
        points: coerceNum(record.points),
        global_rank: coerceInt(record.global_rank),
        global_points: coerceNum(record.global_points),
        normalized_global_score: coerceNum(record.normalized_global_score),
        last_fight_date,
        fighter_status,
      };
    })
    .filter((row) => !!row.name);
}

function compareGoatOrder(a: RankedFighterRecord, b: RankedFighterRecord): number {
  return (a.global_rank ?? Number.POSITIVE_INFINITY) - (b.global_rank ?? Number.POSITIVE_INFINITY)
    || (b.global_points ?? Number.NEGATIVE_INFINITY) - (a.global_points ?? Number.NEGATIVE_INFINITY)
    || (a.rank ?? Number.POSITIVE_INFINITY) - (b.rank ?? Number.POSITIVE_INFINITY)
    || a.name.localeCompare(b.name);
}

function fighterIdentityKeys(record: RankedFighterRecord): string[] {
  const keys = new Set<string>();
  const fighterId = (record.fighter_id || '').trim().toLowerCase();
  const name = (record.name || '').trim().toLowerCase();
  if (fighterId) keys.add(`id:${fighterId}`);
  if (name) keys.add(`name:${name}`);
  return [...keys];
}

function isRicherHomepageRecord(candidate: RankedFighterRecord, existing: RankedFighterRecord | undefined): boolean {
  if (!existing) return true;
  const score = (record: RankedFighterRecord) => [
    record.country,
    record.country_iso2,
    record.finish_rate,
    record.sub_rate,
    record.ko_rate,
    record.wins,
    record.losses,
    record.win_streak,
    record.loss_streak,
  ].filter((value) => value != null && value !== '').length;
  const candidateScore = score(candidate);
  const existingScore = score(existing);
  if (candidateScore !== existingScore) return candidateScore > existingScore;
  if ((candidate.fighter_status || 'active') !== (existing.fighter_status || 'active')) {
    return (candidate.fighter_status || 'active') === 'active';
  }
  if ((candidate.last_fight_date || '') !== (existing.last_fight_date || '')) {
    return (candidate.last_fight_date || '') > (existing.last_fight_date || '');
  }
  return false;
}

function rankingFreshnessValue(value: unknown): number {
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function normalizeGoatRankingRows(rows: unknown[]): RankedFighterRecord[] {
  return rows
    .map((row) => {
      const record = row as Record<string, unknown>;
      const last_fight_date = coerceDate(record.last_fight_date);
      return {
        fighter_id: coerceStr(record.fighter_id),
        name: coerceStr(record.fighter_name_display) || coerceStr(record.fighter_name) || coerceStr(record.name),
        weight_class: coerceStr(record.weight_class),
        rank: coerceInt(record.rank),
        global_rank: coerceInt(record.global_rank),
        global_points: coerceNum(record.global_points),
        normalized_global_score: coerceNum(record.normalized_global_score),
        last_fight_date,
        fighter_status: normalizeFighterStatus(record.fighter_status),
      };
    })
    .filter((row) => !!row.name && row.global_rank != null);
}

function dedupeLatestGoatRankingRows(rows: unknown[]): RankedFighterRecord[] {
  const latestByFighter = new Map<string, { record: RankedFighterRecord; asOf: number; computedAt: number }>();

  for (const row of rows) {
    const record = normalizeGoatRankingRows([row])[0];
    if (!record) continue;
    const source = row as Record<string, unknown>;
    const key = (record.fighter_id || record.name || '').trim().toLowerCase();
    if (!key) continue;

    const asOf = rankingFreshnessValue(source.as_of_date);
    const computedAt = rankingFreshnessValue(source.computed_at);
    const existing = latestByFighter.get(key);
    if (
      !existing
      || asOf > existing.asOf
      || (asOf === existing.asOf && computedAt > existing.computedAt)
    ) {
      latestByFighter.set(key, { record, asOf, computedAt });
    }
  }

  return [...latestByFighter.values()].map((entry) => entry.record).sort(compareGoatOrder);
}

function mergeGoatCardDetails(
  orderedRows: RankedFighterRecord[],
  detailRows: RankedFighterRecord[],
): RankedFighterRecord[] {
  const detailsByKey = new Map<string, RankedFighterRecord>();

  for (const row of detailRows) {
    for (const key of fighterIdentityKeys(row)) {
      const existing = detailsByKey.get(key);
      if (isRicherHomepageRecord(row, existing)) {
        detailsByKey.set(key, {
          ...row,
          is_champion: row.is_champion || existing?.is_champion || false,
        });
      } else if (row.is_champion && existing && !existing.is_champion) {
        detailsByKey.set(key, {
          ...existing,
          is_champion: true,
        });
      }
    }
  }

  return orderedRows.map((row) => {
    const detail = fighterIdentityKeys(row)
      .map((key) => detailsByKey.get(key))
      .find(Boolean);

    return {
      ...detail,
      ...row,
      country: detail?.country ?? row.country,
      country_iso2: detail?.country_iso2 ?? row.country_iso2,
      is_champion: detail?.is_champion ?? row.is_champion,
      wins: detail?.wins ?? row.wins,
      losses: detail?.losses ?? row.losses,
      draws: detail?.draws ?? row.draws,
      win_streak: detail?.win_streak ?? row.win_streak,
      loss_streak: detail?.loss_streak ?? row.loss_streak,
      longest_win_streak: detail?.longest_win_streak ?? row.longest_win_streak,
      longest_loss_streak: detail?.longest_loss_streak ?? row.longest_loss_streak,
      finish_rate: detail?.finish_rate ?? row.finish_rate,
      sub_rate: detail?.sub_rate ?? row.sub_rate,
      ko_rate: detail?.ko_rate ?? row.ko_rate,
      last_fight_date: detail?.last_fight_date || row.last_fight_date,
      fighter_status: detail?.fighter_status ?? row.fighter_status,
    };
  });
}

function countryRecord(index: Record<string, CountryEntry>, value: unknown): CountryEntry | undefined {
  const key = normalizeCountryKey(value);
  return key ? index[key] : undefined;
}

function canonicalCountryName(index: Record<string, CountryEntry>, value: unknown): string {
  return countryRecord(index, value)?.canonical_name ?? coerceStr(value);
}

function countryIso2(index: Record<string, CountryEntry>, value: unknown): string {
  const hit = countryRecord(index, value)?.iso2;
  if (hit) return hit;
  const raw = coerceStr(value);
  return /^[A-Za-z]{2}$/.test(raw) ? raw.toUpperCase() : '';
}

async function loadCanonicalRankedCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const base = resolveBase(env);
  const prefix = resolvePrefix(env);
  if (!isAzure(base)) return [];

  const rankingsFolder = applyPrefix(FOLDER_RANKINGS, prefix);
  const rankingsPath = await resolveLatestAzurePath(env, rankingsFolder);
  if (!rankingsPath) return [];

  const rankingColumns = [
    'fighter_id',
    'fighter_name',
    'fighter_name_display',
    'weight_class',
    'rank',
    'global_rank',
    'global_points',
    'normalized_global_score',
    'fighter_status',
    'last_fight_date',
    'as_of_date',
    'computed_at',
  ];
  const rankingRows = await readParquetRowsAzure(env, rankingsPath, rankingColumns);
  return sortRankedFighters(normalizeRankedFighterRows(rankingRows));
}

export async function getChampionRecords(env?: RuntimeEnv): Promise<ChampionRecord[]> {
  const now = Date.now();
  if (cachedChampions && now < cacheExpiresAt) {
    return cachedChampions;
  }

  const base = resolveBase(env);
  const prefix = resolvePrefix(env);
  if (!isAzure(base)) {
    cachedChampions = [];
    cacheExpiresAt = now + championsCacheTtlMilliseconds(env);
    return cachedChampions;
  }

  const beltFolder = applyPrefix(FOLDER_BELT_HOLDERS, prefix);
  const profilesFolder = applyPrefix(FOLDER_FIGHTER_PROFILES, prefix);

  let beltPath: string | null = null;
  let profilesPath: string | null = null;
  [beltPath, profilesPath] = await Promise.all([
    resolveLatestAzurePath(env, beltFolder),
    resolveLatestAzurePath(env, profilesFolder),
  ]);

  if (!beltPath || !profilesPath) {
    cachedChampions = [];
    cacheExpiresAt = now + championsCacheTtlMilliseconds(env);
    return cachedChampions;
  }

  const beltColumns = [
    'champion_fighter_id',
    'champion_fighter_name',
    'weight_class',
    'title_defenses',
    'title_won_date',
    'is_vacant',
  ];
  const profileColumns = [
    'fighter_id',
    'fighter_name_display',
    'fighter_name',
    'country',
    'wins',
    'losses',
    'draws',
    'win_streak',
    'loss_streak',
    'longest_win_streak',
    'longest_loss_streak',
    'finish_rate_win_shrunk',
    'finish_rate',
    'sub_rate_win_shrunk',
    'ko_rate_win_shrunk',
  ];

  const [beltRows, profileRows, countryIndex] = await Promise.all([
    readParquetRowsAzure(env, beltPath, beltColumns),
    readParquetRowsAzure(env, profilesPath, profileColumns),
    getCountryMasterIndex(env),
  ]);

  const profilesById = new Map<string, Record<string, any>>();
  const profilesByName = new Map<string, Record<string, any>>();
  for (const row of profileRows) {
    const id = coerceStr(row.fighter_id);
    const displayName = coerceStr(row.fighter_name_display);
    const name = coerceStr(row.fighter_name);
    if (id) profilesById.set(id, row);
    if (displayName) profilesByName.set(displayName, row);
    if (name) profilesByName.set(name, row);
  }

  const divisionOrder = [
    'Flyweight',
    'Bantamweight',
    'Featherweight',
    'Lightweight',
    'Welterweight',
    'Middleweight',
    'Light Heavyweight',
    'Heavyweight',
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
  ];

  const champions: ChampionRecord[] = [];
  for (const row of beltRows) {
    if (toBool(row.is_vacant)) continue;
    const champId = coerceStr(row.champion_fighter_id);
    const champName = coerceStr(row.champion_fighter_name);
    if (!champName) continue;

    const profile = (champId && profilesById.get(champId)) || profilesByName.get(champName) || {};
    const countryRaw = profile.country;

    champions.push({
      fighter_id: champId || coerceStr(profile.fighter_id),
      name: coerceStr(profile.fighter_name_display) || coerceStr(profile.fighter_name) || champName,
      country: canonicalCountryName(countryIndex.byAlias, countryRaw),
      country_iso2: countryIso2(countryIndex.byAlias, countryRaw),
      weight_class: coerceStr(row.weight_class),
      is_champion: true,
      title_defenses: coerceInt(row.title_defenses) ?? 0,
      title_won_date: coerceDate(row.title_won_date),
      wins: coerceInt(profile.wins) ?? 0,
      losses: coerceInt(profile.losses) ?? 0,
      draws: coerceInt(profile.draws) ?? 0,
      win_streak: coerceInt(profile.win_streak) ?? 0,
      loss_streak: coerceInt(profile.loss_streak) ?? 0,
      longest_win_streak: coerceInt(profile.longest_win_streak) ?? 0,
      longest_loss_streak: coerceInt(profile.longest_loss_streak) ?? 0,
      finish_rate: coercePct(profile.finish_rate_win_shrunk) ?? coercePct(profile.finish_rate),
      sub_rate: coercePct(profile.sub_rate_win_shrunk),
      ko_rate: coercePct(profile.ko_rate_win_shrunk),
    });
  }

  champions.sort((a, b) => {
    const aIdx = divisionOrder.indexOf(a.weight_class || '');
    const bIdx = divisionOrder.indexOf(b.weight_class || '');
    return (aIdx === -1 ? divisionOrder.length : aIdx) - (bIdx === -1 ? divisionOrder.length : bIdx);
  });

  cachedChampions = champions;
  cacheExpiresAt = now + championsCacheTtlMilliseconds(env);
  return champions;
}

async function loadHomepageRankingsWallPayload(env?: RuntimeEnv): Promise<Record<string, any> | null> {
  const base = resolveBase(env);
  if (!isAzure(base)) {
    lastHomepageRankingsDebug = homepageRankingsDebug(env, {
      note: 'Azure runtime was not resolved. Rankings wall fetch was skipped.',
    });
    console.warn('[fightprophet] homepage rankings unavailable', lastHomepageRankingsDebug);
    return null;
  }

  const payload = await fetchAzureJson(env, homepageBlobPath(env, HOMEPAGE_RANKINGS_WALL_FILENAME));
  if (!payload) {
    lastHomepageRankingsDebug = homepageRankingsDebug(env, {
      fetchedBlob: false,
      note: 'Rankings wall JSON could not be loaded from Azure.',
    });
    console.warn('[fightprophet] homepage rankings fetch returned no payload', lastHomepageRankingsDebug);
    return null;
  }

  return payload;
}

async function loadAllRankedFighterCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const payload = await loadHomepageRankingsWallPayload(env);
  if (!payload) return [];

  const fighters = Array.isArray(payload.fighters) ? payload.fighters : [];
  const homepageDetails = sortRankedFighters(normalizeRankedFighterRows(fighters));
  const canonicalCards = await loadCanonicalRankedCards(env);
  const cards = canonicalCards.length > 0
    ? mergeGoatCardDetails(canonicalCards, homepageDetails)
    : homepageDetails;

  lastHomepageRankingsDebug = homepageRankingsDebug(env, {
    fetchedBlob: true,
    fighterCount: cards.length,
    note: cards.length > 0
      ? 'Rankings wall JSON loaded successfully.'
      : 'Rankings wall JSON loaded but contained no fighters.',
  });

  if (cards.length > 0) {
    console.info('[fightprophet] homepage rankings loaded', lastHomepageRankingsDebug);
  } else {
    console.warn('[fightprophet] homepage rankings JSON was empty', lastHomepageRankingsDebug);
  }

  return cards;
}

async function loadHomepageGoatCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const payload = await loadHomepageRankingsWallPayload(env);
  if (!payload) return [];

  const fighters = Array.isArray(payload.fighters) ? payload.fighters : [];
  const goats = Array.isArray(payload.goats) ? payload.goats : [];
  const homepageDetails = normalizeRankedFighterRows([...goats, ...fighters]);

  const base = resolveBase(env);
  const prefix = resolvePrefix(env);
  if (isAzure(base)) {
    const rankingsFolder = applyPrefix(FOLDER_RANKINGS, prefix);
    const rankingsPath = await resolveLatestAzurePath(env, rankingsFolder);
    if (rankingsPath) {
      const rankingColumns = [
        'fighter_id',
        'fighter_name',
        'fighter_name_display',
        'weight_class',
        'rank',
        'global_rank',
        'global_points',
        'normalized_global_score',
        'fighter_status',
        'last_fight_date',
        'as_of_date',
        'computed_at',
      ];
      const rankingRows = await readParquetRowsAzure(env, rankingsPath, rankingColumns);
      const canonicalGoats = dedupeLatestGoatRankingRows(rankingRows);
      if (canonicalGoats.length > 0) {
        return mergeGoatCardDetails(canonicalGoats, homepageDetails);
      }
    }
  }

  const deduped = new Map<string, RankedFighterRecord>();
  for (const fighter of homepageDetails) {
    const key = (fighter.fighter_id || fighter.name || '').trim().toLowerCase();
    if (!key) continue;
    const existing = deduped.get(key);
    if (!existing) {
      deduped.set(key, fighter);
      continue;
    }
    const shouldReplace =
      (fighter.global_rank ?? Number.POSITIVE_INFINITY) < (existing.global_rank ?? Number.POSITIVE_INFINITY)
      || (
        (fighter.global_rank ?? Number.POSITIVE_INFINITY) === (existing.global_rank ?? Number.POSITIVE_INFINITY)
        && (
          (fighter.global_points ?? Number.NEGATIVE_INFINITY) > (existing.global_points ?? Number.NEGATIVE_INFINITY)
          || (
            (fighter.global_points ?? Number.NEGATIVE_INFINITY) === (existing.global_points ?? Number.NEGATIVE_INFINITY)
            && (fighter.rank ?? Number.POSITIVE_INFINITY) < (existing.rank ?? Number.POSITIVE_INFINITY)
          )
        )
      );
    if (shouldReplace) deduped.set(key, fighter);
  }
  return [...deduped.values()].sort(compareGoatOrder);
}

export async function getAllRankedFighterCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const now = Date.now();
  if (cachedAllRankedFighters && now < cacheAllRankedExpiresAt) {
    return cachedAllRankedFighters;
  }
  const cards = await loadAllRankedFighterCards(env);
  cachedAllRankedFighters = cards;
  const ttl = cards.length > 0 ? championsCacheTtlMilliseconds(env) : Math.min(championsCacheTtlMilliseconds(env), 10 * 60 * 1000);
  cacheAllRankedExpiresAt = now + ttl;
  return cards;
}

export async function getHomepageGoatCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const now = Date.now();
  if (cachedHomepageGoats && now < cacheAllRankedExpiresAt) {
    return cachedHomepageGoats;
  }
  const cards = await loadHomepageGoatCards(env);
  cachedHomepageGoats = cards;
  const ttl = cards.length > 0 ? championsCacheTtlMilliseconds(env) : Math.min(championsCacheTtlMilliseconds(env), 10 * 60 * 1000);
  cacheAllRankedExpiresAt = now + ttl;
  return cards;
}

export async function getRankedFighterCards(env?: RuntimeEnv): Promise<RankedFighterRecord[]> {
  const now = Date.now();
  if (cachedRankedFighters && now < cacheExpiresAt) {
    return cachedRankedFighters;
  }

  const all = await getAllRankedFighterCards(env);
  const featuredByDivision = new Map<string, RankedFighterRecord>();
  for (const card of all) {
    if ((card.rank ?? 999) > 2) continue;
    const wc = card.weight_class || '';
    if (!wc) continue;
    if (!featuredByDivision.has(wc)) featuredByDivision.set(wc, card);
  }
  const featured = featuredByDivision.size > 0
    ? [...featuredByDivision.values()]
    : all.filter((c) => (c.rank ?? 999) <= 2);
  const cards = featured
    .sort((a, b) => {
      const aIdx = divisionSortIndex(a.weight_class || '');
      const bIdx = divisionSortIndex(b.weight_class || '');
      if (aIdx !== bIdx) return aIdx - bIdx;
      return (a.rank ?? 999) - (b.rank ?? 999);
    })
    .slice(0, 12);

  cachedRankedFighters = cards;
  const ttl = cards.length > 0 ? championsCacheTtlMilliseconds(env) : Math.min(championsCacheTtlMilliseconds(env), 10 * 60 * 1000);
  cacheExpiresAt = now + ttl;
  return cards;
}

export interface DivisionGroup {
  weight_class: string;
  fighters: RankedFighterRecord[];
}

export function groupRankedFightersByDivision(cards: RankedFighterRecord[]): DivisionGroup[] {
  const groups = new Map<string, RankedFighterRecord[]>();
  for (const card of cards) {
    const wc = card.weight_class || 'Unranked';
    if (!groups.has(wc)) groups.set(wc, []);
    groups.get(wc)!.push(card);
  }
  return [...groups.entries()]
    .map(([weight_class, fighters]) => ({
      weight_class,
      fighters: [...fighters].sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999)),
    }))
    .sort((a, b) => divisionSortIndex(a.weight_class) - divisionSortIndex(b.weight_class));
}
