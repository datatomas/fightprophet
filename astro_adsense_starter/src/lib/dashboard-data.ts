import { parquetReadObjects } from 'hyparquet';
import { compressors } from 'hyparquet-compressors';
import type { RuntimeEnv } from './country-master';
import { getCountryMasterIndex } from './country-master';
import { getAllRankedFighterCards, type RankedFighterRecord } from './champions';

const DEFAULT_CONTAINER = 'fightprophet-dashboard';
const DEFAULT_PREFIX = 'mma/diamond';
const DEFAULT_CACHE_TTL_SECONDS = 43200;
const AZURE_BLOB_API_VERSION = '2023-11-03';

const FOLDER_UPCOMING = 'dashboard_upcoming_cards';
const FOLDER_UPCOMING_CATBOOST = 'dashboard_upcoming_cards_catboost';
const FOLDER_UPCOMING_ENSEMBLE = 'dashboard_upcoming_cards_ensemble';
const FOLDER_UPCOMING_LOGREG = 'dashboard_upcoming_cards_logreg';
const FOLDER_EVENTS = 'dashboard_upcoming_events';
const FOLDER_HISTORICAL = 'dashboard_hist_historical_all';
const FOLDER_HISTORICAL_CATBOOST = 'dashboard_hist_historical_all_catboost';
const FOLDER_HISTORICAL_ENSEMBLE = 'dashboard_hist_historical_all_ensemble';
const FOLDER_HISTORICAL_LOGREG = 'dashboard_hist_historical_all_logreg';
const FOLDER_STATS = 'dashboard_model_stats';
const FOLDER_STATS_CATBOOST = 'dashboard_model_stats_catboost';
const FOLDER_STATS_ENSEMBLE = 'dashboard_model_stats_ensemble';
const FOLDER_STATS_LOGREG = 'dashboard_model_stats_logreg';
const FOLDER_FIGHTER_PROFILES = 'dashboard_fighter_profiles';
const FOLDER_FIGHTER_HISTORY = 'dashboard_fighter_history';

export interface EventSummary {
  event_name: string;
  event_date: string;
  location: string;
  event_phase: 'Upcoming' | 'Past';
  fight_count: number;
  unique_fighters: number;
  model_correct_count: number | null;
  model_total_count: number | null;
}

export interface EventFightRow {
  event_name: string;
  event_date: string;
  location: string;
  event_phase: 'Upcoming' | 'Past';
  fighter_name_display: string;
  opponent_name_display: string;
  winner_name_display: string;
  result: string;
  signal_strength: string;
  model_prob: number | null;
  market_prob: number | null;
  edge: number | null;
  model_correct: boolean | null;
  // betting intelligence (computed in backend betting_math.py)
  bet_on_name: string;
  odds_source: string;
  odds_sportsbook: string;
  bet_market_prob: number | null;
  market_hold: number | null;
  bet_fair_prob: number | null;
  bet_fair_odds: number | null;
  expected_value: number | null;
  expected_roi: number | null;
  edge_vs_fair: number | null;
  realized_roi: number | null;
}

export interface EventsHistoryData {
  events: EventSummary[];
  fights: EventFightRow[];
  debug: {
    fetchedEvents: boolean;
    fetchedUpcoming: boolean;
    fetchedHistorical: boolean;
    eventCount: number;
    fightCount: number;
  };
}

export interface FighterProfile {
  fighter_id: string;
  name: string;
  country: string;
  country_iso2: string;
  country_flag: string;
  dob: string;
  stance: string;
  reach: string;
  weight: string;
  height: string;
  age: number | null;
  fighter_status: string;
  current_belt_weight_classes: string;
  is_current_champion: boolean;
  first_fight_date: string;
  last_fight_date: string;
  total_fights: number | null;
  wins: number | null;
  losses: number | null;
  draws: number | null;
  no_contests: number | null;
  win_rate: number | null;
  finish_rate: number | null;
  ko_wins: number | null;
  sub_wins: number | null;
  dec_wins: number | null;
  bonuses_won_count: number | null;
  win_streak: number | null;
  loss_streak: number | null;
  longest_win_streak: number | null;
  longest_loss_streak: number | null;
  title_fights: number | null;
  weight_classes_fought: number | null;
  slpm: number | null;
  sapm: number | null;
  str_acc: number | null;
  str_def: number | null;
  td_avg: number | null;
  td_acc: number | null;
  td_def: number | null;
  sub_avg: number | null;
  ko_rate_win_shrunk: number | null;
  sub_rate_win_shrunk: number | null;
  finish_rate_win_shrunk: number | null;
  wins_method_known_count: number | null;
  search_text: string;
}

export interface FighterHistoryRow {
  fighter_id: string;
  fighter_name_display: string;
  opponent_name_display: string;
  opponent_country: string;
  event_date: string;
  event_name: string;
  weight_class: string;
  is_title_fight: boolean;
  result: string;
  winner_name_display: string;
  method: string;
  method_category: string;
  round: number | null;
  time: string;
  kd_for: number | null;
  str_for: number | null;
  td_for: number | null;
  sub_for: number | null;
  kd_against: number | null;
  str_against: number | null;
  td_against: number | null;
  sub_against: number | null;
}

export interface FighterCardsData {
  profiles: FighterProfile[];
  history: Record<string, FighterHistoryRow[]>;
  debug: {
    fetchedProfiles: boolean;
    fetchedHistory: boolean;
    profileCount: number;
    historyCount: number;
  };
}

export interface UpcomingFightRow {
  event_name: string;
  event_date: string;
  location: string;
  fighter_name_display: string;
  opponent_name_display: string;
  weight_class: string;
  fighter_country: string;
  opponent_country: string;
  fighter_flag: string;
  opponent_flag: string;
  fighter_is_champion: boolean;
  opponent_is_champion: boolean;
  fighter_status: string;
  opponent_status: string;
  fighter_wins: number | null;
  fighter_losses: number | null;
  opponent_wins: number | null;
  opponent_losses: number | null;
  fighter_finish_rate: number | null;
  opponent_finish_rate: number | null;
  fighter_sub_rate: number | null;
  opponent_sub_rate: number | null;
  fighter_win_streak: number | null;
  fighter_loss_streak: number | null;
  opponent_win_streak: number | null;
  opponent_loss_streak: number | null;
  model_prob: number | null;
  market_prob: number | null;
  edge: number | null;
  signal_strength: string;
  recommended_bet: boolean;
  fighter_odds: number | null;
  opponent_odds: number | null;
  bet_on_name: string;
  // betting intelligence (computed in backend betting_math.py)
  odds_source: string;
  odds_sportsbook: string;
  bet_market_prob: number | null;
  market_hold: number | null;
  bet_fair_prob: number | null;
  bet_fair_odds: number | null;
  bet_side_model_prob: number | null;
  expected_value: number | null;
  expected_roi: number | null;
  edge_vs_fair: number | null;
}

export interface UpcomingPredictionsData {
  fights: UpcomingFightRow[];
  debug: {
    fetched: boolean;
    fightCount: number;
  };
}

export interface AllModelsPredictionsData {
  catboost: UpcomingFightRow[];
  ensemble: UpcomingFightRow[];
  logreg: UpcomingFightRow[];
}

let cachedEvents: EventsHistoryData | null = null;
let cachedFighters: FighterCardsData | null = null;
let cachedPredictions: UpcomingPredictionsData | null = null;
let cacheExpiresAt = 0;

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
  const raw = envValue(env, 'DASHBOARD_CACHE_TTL_SECONDS', String(DEFAULT_CACHE_TTL_SECONDS));
  const parsed = Number.parseInt(raw, 10);
  const seconds = Number.isFinite(parsed) ? Math.max(60, parsed) : DEFAULT_CACHE_TTL_SECONDS;
  return seconds * 1000;
}

function resolveBase(env: RuntimeEnv | undefined): string {
  const explicit = envValue(env, 'PARQUET_BASE_URI') || envValue(env, 'AZURE_PARQUET_BASE_URI');
  if (explicit) return explicit.replace(/\/+$/, '');
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

function isAzure(base: string): boolean {
  return base.startsWith('az://') || base.startsWith('azure://');
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
  if (!account || !accountKey || !container || !blobPath) return null;

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
  if (!headers || !url) return null;
  const response = await fetch(url, { headers });
  if (!response.ok) return null;
  const payload = await response.json();
  return payload && typeof payload === 'object' ? payload as Record<string, any> : null;
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
  if (!account || !accountKey || !container || !prefix) return [];

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

    const search = new URLSearchParams({ restype: 'container', comp: 'list', prefix });
    if (marker) search.set('marker', marker);
    const url = `https://${account}.blob.core.windows.net/${container}?${search.toString()}`;
    const response = await fetch(url, {
      headers: {
        Authorization: `SharedKey ${account}:${signature}`,
        'x-ms-date': requestDate,
        'x-ms-version': AZURE_BLOB_API_VERSION,
      },
    });
    if (!response.ok) return [...collected];

    const xml = await response.text();
    for (const match of xml.matchAll(/<Name>([^<]+)<\/Name>/g)) {
      const name = String(match[1] || '').trim();
      if (name) collected.add(name);
    }
    marker = xml.match(/<NextMarker>([^<]*)<\/NextMarker>/)?.[1]?.trim() || '';
  } while (marker);

  return [...collected];
}

async function expandAzureParquetBlobPaths(env: RuntimeEnv | undefined, remotePath: string): Promise<string[]> {
  const blobPath = azureBlobPathFromRemotePath(env, remotePath);
  if (!blobPath.includes('*')) return [blobPath];
  const prefix = blobPath.split('*')[0] || '';
  const listed = await listAzureBlobPaths(env, prefix);
  return listed.filter((path) => path.endsWith('.parquet')).sort();
}

async function readParquetRowsAzure(
  env: RuntimeEnv | undefined,
  remotePath: string,
  columns?: string[],
): Promise<Record<string, any>[]> {
  const blobPaths = await expandAzureParquetBlobPaths(env, remotePath);
  const rows: Record<string, any>[] = [];

  for (const blobPath of blobPaths) {
    const headers = await azureRequestHeaders(env, blobPath);
    const url = azureBlobUrl(env, blobPath);
    if (!headers || !url) continue;
    const response = await fetch(url, { headers });
    if (!response.ok) continue;
    const file = await response.arrayBuffer();
    // columns=undefined reads all columns; columns=[] or array reads specified columns
    const opts: Parameters<typeof parquetReadObjects>[0] = { file, compressors };
    if (columns && columns.length > 0) opts.columns = columns;
    let fileRows;
    try {
      fileRows = await parquetReadObjects(opts);
    } catch {
      // A requested column may not exist yet on an older export (e.g. new
      // betting-math columns before the ETL re-runs). Fall back to reading all
      // columns so the page never breaks during a schema transition.
      fileRows = await parquetReadObjects({ file, compressors });
    }
    rows.push(...fileRows as Record<string, any>[]);
  }

  return rows;
}

async function readLatestDashboardRows(
  env: RuntimeEnv | undefined,
  folder: string,
  columns?: string[],
): Promise<Record<string, any>[]> {
  const base = resolveBase(env);
  if (!isAzure(base)) return [];
  const path = await resolveLatestAzurePath(env, applyPrefix(folder, resolvePrefix(env)));
  if (!path) return [];
  return readParquetRowsAzure(env, path, columns);
}

function cleanStr(value: unknown): string {
  const text = value == null ? '' : String(value).trim();
  return ['', 'nan', 'nat', 'none', 'null', 'undefined'].includes(text.toLowerCase()) ? '' : text;
}

function coerceDate(value: unknown): string {
  if (value == null || value === '') return '';
  if (value instanceof Date && Number.isFinite(value.getTime())) return value.toISOString().slice(0, 10);
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toISOString().slice(0, 10);
}

function dateValue(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function coerceNum(value: unknown): number | null {
  if (value == null || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function coerceInt(value: unknown): number | null {
  const num = coerceNum(value);
  return num == null ? null : Math.trunc(num);
}

function coerceBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value === 1;
  return ['1', 'true', 't', 'yes', 'y'].includes(cleanStr(value).toLowerCase());
}

function phaseForDate(date: string): 'Upcoming' | 'Past' {
  if (!date) return 'Past';
  return dateValue(date) >= Date.now() - 24 * 60 * 60 * 1000 ? 'Upcoming' : 'Past';
}

function flagEmojiFor(index: Record<string, { canonical_name: string; iso2?: string }>, value: unknown): string {
  const raw = cleanStr(value);
  if (!raw) return '';
  const code = (index[raw.toUpperCase()] as { iso2?: string } | undefined)?.iso2 ?? (raw.length === 2 ? raw.toUpperCase() : '');
  if (!/^[A-Z]{2}$/.test(code)) return '';
  return String.fromCodePoint(...[...code].map((ch) => ch.charCodeAt(0) + 127397));
}

function countryName(index: Record<string, { canonical_name: string }>, value: unknown): string {
  const raw = cleanStr(value);
  if (!raw) return '';
  return index[raw.toUpperCase()]?.canonical_name ?? raw;
}

function eventKey(name: string): string {
  return name.trim().toLowerCase();
}

function fighterProfileKey(value: unknown): string {
  return cleanStr(value)
    .toLowerCase()
    .replace(/\([^)]*\)/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ');
}

function fighterProfileIndex(profiles: FighterProfile[]): Map<string, FighterProfile> {
  const index = new Map<string, FighterProfile>();
  for (const profile of profiles) {
    const keys = [
      cleanStr(profile.fighter_id) ? `id:${cleanStr(profile.fighter_id).toLowerCase()}` : '',
      fighterProfileKey(profile.name),
    ].filter(Boolean);
    for (const key of keys) {
      if (!index.has(key)) index.set(key, profile);
    }
  }
  return index;
}

function fillUpcomingSideFromProfile(
  fight: UpcomingFightRow,
  profile: FighterProfile | undefined,
  side: 'fighter' | 'opponent',
): void {
  if (!profile) return;
  const prefix = side === 'fighter' ? 'fighter' : 'opponent';
  const countryKey = `${prefix}_country` as 'fighter_country' | 'opponent_country';
  const isChampionKey = `${prefix}_is_champion` as 'fighter_is_champion' | 'opponent_is_champion';
  const statusKey = `${prefix}_status` as 'fighter_status' | 'opponent_status';
  const winsKey = `${prefix}_wins` as 'fighter_wins' | 'opponent_wins';
  const lossesKey = `${prefix}_losses` as 'fighter_losses' | 'opponent_losses';
  const finishKey = `${prefix}_finish_rate` as 'fighter_finish_rate' | 'opponent_finish_rate';
  const subKey = `${prefix}_sub_rate` as 'fighter_sub_rate' | 'opponent_sub_rate';
  const winStreakKey = `${prefix}_win_streak` as 'fighter_win_streak' | 'opponent_win_streak';
  const lossStreakKey = `${prefix}_loss_streak` as 'fighter_loss_streak' | 'opponent_loss_streak';

  fight[countryKey] ||= profile.country;
  fight[isChampionKey] ||= profile.is_current_champion;
  fight[statusKey] ||= profile.fighter_status;
  fight[winsKey] ??= profile.wins;
  fight[lossesKey] ??= profile.losses;
  fight[finishKey] ??= profile.finish_rate_win_shrunk ?? profile.finish_rate;
  fight[subKey] ??= profile.sub_rate_win_shrunk;
  fight[winStreakKey] ??= profile.win_streak;
  fight[lossStreakKey] ??= profile.loss_streak;
}

function fillUpcomingFightsFromProfiles(
  fights: UpcomingFightRow[],
  profiles: FighterProfile[],
  countryIndex: Awaited<ReturnType<typeof getCountryMasterIndex>>,
): UpcomingFightRow[] {
  if (!profiles.length || !fights.length) return fights;
  const profilesByKey = fighterProfileIndex(profiles);
  return fights.map((fight) => {
    const fighterProfile = profilesByKey.get(fighterProfileKey(fight.fighter_name_display));
    const opponentProfile = profilesByKey.get(fighterProfileKey(fight.opponent_name_display));
    fillUpcomingSideFromProfile(fight, fighterProfile, 'fighter');
    fillUpcomingSideFromProfile(fight, opponentProfile, 'opponent');
    fight.fighter_flag ||= flagEmojiFor(countryIndex.byAlias, fight.fighter_country);
    fight.opponent_flag ||= flagEmojiFor(countryIndex.byAlias, fight.opponent_country);
    return fight;
  });
}

function rankedFighterIndex(records: RankedFighterRecord[]): Map<string, RankedFighterRecord> {
  const index = new Map<string, RankedFighterRecord>();
  for (const record of records) {
    const keys = [
      cleanStr(record.fighter_id) ? `id:${cleanStr(record.fighter_id).toLowerCase()}` : '',
      fighterProfileKey(record.name),
    ].filter(Boolean);
    for (const key of keys) {
      if (!index.has(key)) index.set(key, record);
    }
  }
  return index;
}

function fillUpcomingSideFromRanking(
  fight: UpcomingFightRow,
  record: RankedFighterRecord | undefined,
  side: 'fighter' | 'opponent',
  countryIndex: Awaited<ReturnType<typeof getCountryMasterIndex>>,
): void {
  if (!record) return;
  const prefix = side === 'fighter' ? 'fighter' : 'opponent';
  const countryKey = `${prefix}_country` as 'fighter_country' | 'opponent_country';
  const flagKey = `${prefix}_flag` as 'fighter_flag' | 'opponent_flag';
  const isChampionKey = `${prefix}_is_champion` as 'fighter_is_champion' | 'opponent_is_champion';
  const statusKey = `${prefix}_status` as 'fighter_status' | 'opponent_status';
  const winsKey = `${prefix}_wins` as 'fighter_wins' | 'opponent_wins';
  const lossesKey = `${prefix}_losses` as 'fighter_losses' | 'opponent_losses';
  const finishKey = `${prefix}_finish_rate` as 'fighter_finish_rate' | 'opponent_finish_rate';
  const subKey = `${prefix}_sub_rate` as 'fighter_sub_rate' | 'opponent_sub_rate';
  const winStreakKey = `${prefix}_win_streak` as 'fighter_win_streak' | 'opponent_win_streak';
  const lossStreakKey = `${prefix}_loss_streak` as 'fighter_loss_streak' | 'opponent_loss_streak';

  fight[countryKey] ||= countryName(countryIndex.byAlias, record.country || record.country_iso2);
  fight[flagKey] ||= flagEmojiFor(countryIndex.byAlias, record.country_iso2 || record.country);
  fight[isChampionKey] ||= !!record.is_champion;
  fight[statusKey] ||= record.fighter_status || '';
  fight[winsKey] ??= record.wins ?? null;
  fight[lossesKey] ??= record.losses ?? null;
  fight[finishKey] ??= record.finish_rate ?? null;
  fight[subKey] ??= record.sub_rate ?? null;
  fight[winStreakKey] ??= record.win_streak ?? null;
  fight[lossStreakKey] ??= record.loss_streak ?? null;
}

function fillUpcomingFightsFromRankings(
  fights: UpcomingFightRow[],
  records: RankedFighterRecord[],
  countryIndex: Awaited<ReturnType<typeof getCountryMasterIndex>>,
): UpcomingFightRow[] {
  if (!records.length || !fights.length) return fights;
  const recordsByKey = rankedFighterIndex(records);
  return fights.map((fight) => {
    fillUpcomingSideFromRanking(fight, recordsByKey.get(fighterProfileKey(fight.fighter_name_display)), 'fighter', countryIndex);
    fillUpcomingSideFromRanking(fight, recordsByKey.get(fighterProfileKey(fight.opponent_name_display)), 'opponent', countryIndex);
    return fight;
  });
}

function normalizeFightRow(row: Record<string, any>, phaseHint?: 'Upcoming' | 'Past'): EventFightRow | null {
  const eventName = cleanStr(row.event_name);
  if (!eventName) return null;
  const eventDate = coerceDate(row.event_date);
  return {
    event_name: eventName,
    event_date: eventDate,
    location: cleanStr(row.location),
    event_phase: phaseHint ?? phaseForDate(eventDate),
    fighter_name_display: cleanStr(row.fighter_name_display),
    opponent_name_display: cleanStr(row.opponent_name_display),
    winner_name_display: cleanStr(row.winner_name_display),
    result: cleanStr(row.result),
    signal_strength: cleanStr(row.signal_strength),
    model_prob: coerceNum(row.model_prob),
    market_prob: coerceNum(row.market_prob),
    edge: coerceNum(row.edge),
    model_correct: row.model_correct == null ? null : coerceBool(row.model_correct),
    bet_on_name: cleanStr(row.bet_on_name),
    ...mapBettingFields(row),
    realized_roi: coerceNum(row.realized_roi),
  };
}

export async function getEventsHistoryData(env?: RuntimeEnv): Promise<EventsHistoryData> {
  const now = Date.now();
  if (cachedEvents && now < cacheExpiresAt) return cachedEvents;

  const eventColumns = ['event_name', 'event_date', 'location'];
  const fightColumns = [
    'event_name',
    'event_date',
    'location',
    'fighter_name_display',
    'opponent_name_display',
    'winner_name_display',
    'result',
    'signal_strength',
    'model_prob',
    'market_prob',
    'edge',
    'model_correct',
    'bet_on_name',
    ...BETTING_COLUMNS,
  ];
  // realized_roi is only present on the (settled) historical exports.
  const histColumns = [...fightColumns, 'realized_roi'];

  const [eventsRows, upcomingPrimary, historicalPrimary] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_EVENTS, eventColumns),
    readLatestDashboardRows(env, FOLDER_UPCOMING, fightColumns),
    readLatestDashboardRows(env, FOLDER_HISTORICAL, histColumns),
  ]);

  const [upcomingFallback, historicalFallback] = await Promise.all([
    upcomingPrimary.length > 0 ? Promise.resolve([]) : readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, fightColumns),
    historicalPrimary.length > 0 ? Promise.resolve([]) : readLatestDashboardRows(env, FOLDER_HISTORICAL_CATBOOST, histColumns),
  ]);

  const upcomingRows = upcomingPrimary.length > 0 ? upcomingPrimary : upcomingFallback;
  const historicalRows = historicalPrimary.length > 0 ? historicalPrimary : historicalFallback;
  const fights = [
    ...upcomingRows.map((row) => normalizeFightRow(row, 'Upcoming')),
    ...historicalRows.map((row) => normalizeFightRow(row, 'Past')),
  ].filter(Boolean) as EventFightRow[];

  const summariesByEvent = new Map<string, EventSummary>();
  const ensureSummary = (name: string, eventDate: string, location: string, phase?: 'Upcoming' | 'Past') => {
    const key = eventKey(name);
    const existing = summariesByEvent.get(key);
    if (!existing) {
      summariesByEvent.set(key, {
        event_name: name,
        event_date: eventDate,
        location,
        event_phase: phase ?? phaseForDate(eventDate),
        fight_count: 0,
        unique_fighters: 0,
        model_correct_count: null,
        model_total_count: null,
      });
      return;
    }
    if (dateValue(eventDate) > dateValue(existing.event_date)) existing.event_date = eventDate;
    if (!existing.location && location) existing.location = location;
    existing.event_phase = phaseForDate(existing.event_date);
  };

  for (const row of eventsRows) {
    const name = cleanStr(row.event_name);
    if (!name) continue;
    const eventDate = coerceDate(row.event_date);
    ensureSummary(name, eventDate, cleanStr(row.location));
  }

  const fightersByEvent = new Map<string, Set<string>>();
  const correctByEvent = new Map<string, { correct: number; total: number }>();
  for (const row of fights) {
    ensureSummary(row.event_name, row.event_date, row.location, row.event_phase);
    const key = eventKey(row.event_name);
    const summary = summariesByEvent.get(key);
    if (summary) summary.fight_count += 1;
    if (!fightersByEvent.has(key)) fightersByEvent.set(key, new Set());
    for (const fighter of [row.fighter_name_display, row.opponent_name_display]) {
      if (fighter) fightersByEvent.get(key)!.add(fighter);
    }
    if (row.model_correct != null) {
      const current = correctByEvent.get(key) ?? { correct: 0, total: 0 };
      current.total += 1;
      if (row.model_correct) current.correct += 1;
      correctByEvent.set(key, current);
    }
  }

  for (const [key, summary] of summariesByEvent.entries()) {
    summary.unique_fighters = fightersByEvent.get(key)?.size ?? 0;
    const model = correctByEvent.get(key);
    if (model) {
      summary.model_correct_count = model.correct;
      summary.model_total_count = model.total;
    }
  }

  const events = [...summariesByEvent.values()]
    .sort((a, b) => dateValue(b.event_date) - dateValue(a.event_date) || a.event_name.localeCompare(b.event_name));

  cachedEvents = {
    events,
    fights: fights.sort((a, b) => dateValue(b.event_date) - dateValue(a.event_date) || a.event_name.localeCompare(b.event_name)),
    debug: {
      fetchedEvents: eventsRows.length > 0,
      fetchedUpcoming: upcomingRows.length > 0,
      fetchedHistorical: historicalRows.length > 0,
      eventCount: events.length,
      fightCount: fights.length,
    },
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedEvents;
}

export async function getFighterCardsData(env?: RuntimeEnv): Promise<FighterCardsData> {
  const now = Date.now();
  if (cachedFighters && now < cacheExpiresAt) return cachedFighters;

  const profileColumns = [
    'fighter_id',
    'fighter_name_display',
    'fighter_name',
    'fighter_name_plain',
    'dob',
    'stance',
    'reach',
    'weight',
    'height',
    'belt',
    'current_belts_count',
    'is_current_champion',
    'current_belt_weight_classes',
    'age',
    'fighter_status',
    'country',
    'slpm',
    'str_acc',
    'sapm',
    'str_def',
    'td_avg',
    'td_acc',
    'td_def',
    'sub_avg',
    'ko_rate_win_shrunk',
    'sub_rate_win_shrunk',
    'finish_rate_win_shrunk',
    'wins_method_known_count',
    'first_fight_date',
    'last_fight_date',
    'total_fights',
    'wins',
    'losses',
    'draws',
    'no_contests',
    'bonuses_won_count',
    'ko_wins',
    'sub_wins',
    'dec_wins',
    'win_streak',
    'loss_streak',
    'longest_win_streak',
    'longest_loss_streak',
    'title_fights',
    'weight_classes_fought',
    'win_rate',
    'finish_rate',
  ];
  const historyColumns = [
    'fighter_id',
    'fighter_name_display',
    'opponent_name_display',
    'opponent_country',
    'event_date',
    'event_name',
    'weight_class',
    'is_title_fight',
    'result',
    'winner_name_display',
    'method',
    'method_category',
    'round',
    'time',
    'kd_for',
    'str_for',
    'td_for',
    'sub_for',
    'kd_against',
    'str_against',
    'td_against',
    'sub_against',
  ];

  const [profileRows, historyRows, countryIndex] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_FIGHTER_PROFILES, profileColumns),
    readLatestDashboardRows(env, FOLDER_FIGHTER_HISTORY, historyColumns),
    getCountryMasterIndex(env),
  ]);

  const profiles: FighterProfile[] = profileRows
    .map((row) => {
      const name = cleanStr(row.fighter_name_display) || cleanStr(row.fighter_name) || cleanStr(row.fighter_name_plain);
      const country = countryName(countryIndex.byAlias, row.country);
      const rawCountry = String(row.country ?? '').trim().toUpperCase();
      const countryEntry = countryIndex.byAlias[rawCountry];
      const countryIso2 = countryEntry?.iso2 ?? (rawCountry.length === 2 ? rawCountry : '');
      const countryFlag = flagEmojiFor(countryIndex.byAlias, row.country);
      const currentBelts = cleanStr(row.current_belt_weight_classes) || cleanStr(row.belt);
      const profile: FighterProfile = {
        fighter_id: cleanStr(row.fighter_id),
        name,
        country,
        country_iso2: countryIso2,
        country_flag: countryFlag,
        dob: coerceDate(row.dob),
        stance: cleanStr(row.stance),
        reach: cleanStr(row.reach),
        weight: cleanStr(row.weight),
        height: cleanStr(row.height),
        age: coerceInt(row.age),
        fighter_status: cleanStr(row.fighter_status) || 'active',
        current_belt_weight_classes: currentBelts,
        is_current_champion: coerceBool(row.is_current_champion) || (coerceInt(row.current_belts_count) ?? 0) > 0,
        first_fight_date: coerceDate(row.first_fight_date),
        last_fight_date: coerceDate(row.last_fight_date),
        total_fights: coerceInt(row.total_fights),
        wins: coerceInt(row.wins),
        losses: coerceInt(row.losses),
        draws: coerceInt(row.draws),
        no_contests: coerceInt(row.no_contests),
        win_rate: coerceNum(row.win_rate),
        finish_rate: coerceNum(row.finish_rate),
        ko_wins: coerceInt(row.ko_wins),
        sub_wins: coerceInt(row.sub_wins),
        dec_wins: coerceInt(row.dec_wins),
        bonuses_won_count: coerceInt(row.bonuses_won_count),
        win_streak: coerceInt(row.win_streak),
        loss_streak: coerceInt(row.loss_streak),
        longest_win_streak: coerceInt(row.longest_win_streak),
        longest_loss_streak: coerceInt(row.longest_loss_streak),
        title_fights: coerceInt(row.title_fights),
        weight_classes_fought: coerceInt(row.weight_classes_fought),
        slpm: coerceNum(row.slpm),
        sapm: coerceNum(row.sapm),
        str_acc: coerceNum(row.str_acc),
        str_def: coerceNum(row.str_def),
        td_avg: coerceNum(row.td_avg),
        td_acc: coerceNum(row.td_acc),
        td_def: coerceNum(row.td_def),
        sub_avg: coerceNum(row.sub_avg),
        ko_rate_win_shrunk: coerceNum(row.ko_rate_win_shrunk),
        sub_rate_win_shrunk: coerceNum(row.sub_rate_win_shrunk),
        finish_rate_win_shrunk: coerceNum(row.finish_rate_win_shrunk),
        wins_method_known_count: coerceInt(row.wins_method_known_count),
        search_text: '',
      };
      profile.search_text = `${profile.name} ${profile.country} ${profile.fighter_status} ${profile.current_belt_weight_classes}`.toLowerCase();
      return profile;
    })
    .filter((profile) => !!profile.name)
    .sort((a, b) => {
      if (a.is_current_champion !== b.is_current_champion) return a.is_current_champion ? -1 : 1;
      return (b.wins ?? 0) - (a.wins ?? 0) || a.name.localeCompare(b.name);
    });

  const history: Record<string, FighterHistoryRow[]> = {};
  for (const row of historyRows) {
    const fighterId = cleanStr(row.fighter_id);
    if (!fighterId) continue;
    const item: FighterHistoryRow = {
      fighter_id: fighterId,
      fighter_name_display: cleanStr(row.fighter_name_display),
      opponent_name_display: cleanStr(row.opponent_name_display),
      opponent_country: countryName(countryIndex.byAlias, row.opponent_country),
      event_date: coerceDate(row.event_date),
      event_name: cleanStr(row.event_name),
      weight_class: cleanStr(row.weight_class),
      is_title_fight: coerceBool(row.is_title_fight),
      result: cleanStr(row.result),
      winner_name_display: cleanStr(row.winner_name_display),
      method: cleanStr(row.method),
      method_category: cleanStr(row.method_category),
      round: coerceInt(row.round),
      time: cleanStr(row.time),
      kd_for: coerceInt(row.kd_for),
      str_for: coerceInt(row.str_for),
      td_for: coerceInt(row.td_for),
      sub_for: coerceInt(row.sub_for),
      kd_against: coerceInt(row.kd_against),
      str_against: coerceInt(row.str_against),
      td_against: coerceInt(row.td_against),
      sub_against: coerceInt(row.sub_against),
    };
    if (!history[fighterId]) history[fighterId] = [];
    history[fighterId].push(item);
  }

  for (const key of Object.keys(history)) {
    history[key].sort((a, b) => dateValue(b.event_date) - dateValue(a.event_date));
    history[key] = history[key].slice(0, 10);
  }

  cachedFighters = {
    profiles,
    history,
    debug: {
      fetchedProfiles: profileRows.length > 0,
      fetchedHistory: historyRows.length > 0,
      profileCount: profiles.length,
      historyCount: Object.values(history).reduce((acc, rows) => acc + rows.length, 0),
    },
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedFighters;
}

// Betting-intelligence columns + mapper, shared by every prediction/event read
// so the frontend never re-derives betting math (all baked by betting_math.py).
const BETTING_COLUMNS = [
  'odds_source', 'odds_sportsbook',
  'bet_market_prob', 'market_hold', 'bet_fair_prob', 'bet_fair_odds',
  'bet_side_model_prob', 'expected_value', 'expected_roi', 'edge_vs_fair',
];

function mapBettingFields(row: Record<string, any>) {
  return {
    odds_source: cleanStr(row.odds_source),
    odds_sportsbook: cleanStr(row.odds_sportsbook),
    bet_market_prob: coerceNum(row.bet_market_prob),
    market_hold: coerceNum(row.market_hold),
    bet_fair_prob: coerceNum(row.bet_fair_prob),
    bet_fair_odds: coerceNum(row.bet_fair_odds),
    bet_side_model_prob: coerceNum(row.bet_side_model_prob),
    expected_value: coerceNum(row.expected_value),
    expected_roi: coerceNum(row.expected_roi),
    edge_vs_fair: coerceNum(row.edge_vs_fair),
  };
}

export async function getUpcomingPredictionsData(env?: RuntimeEnv): Promise<UpcomingPredictionsData> {
  const now = Date.now();
  if (cachedPredictions && now < cacheExpiresAt) return cachedPredictions;

  const columns = [
    'event_name', 'event_date', 'location',
    'fighter_name_display', 'opponent_name_display',
    'weight_class',
    'fighter_country', 'opponent_country',
    'fighter_is_champion', 'opponent_is_champion',
    'fighter_status', 'opponent_status',
    'fighter_wins', 'fighter_losses',
    'opponent_wins', 'opponent_losses',
    'fighter_finish_rate', 'opponent_finish_rate',
    'fighter_sub_rate', 'opponent_sub_rate',
    'fighter_win_streak', 'fighter_loss_streak',
    'opponent_win_streak', 'opponent_loss_streak',
    'model_prob', 'market_prob', 'edge',
    'signal_strength', 'recommended_bet',
    'fighter_odds', 'opponent_odds',
    'bet_on_name',
    ...BETTING_COLUMNS,
  ];

  const [primaryRows, countryIndex, fighterData, rankedCards] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_UPCOMING, columns),
    getCountryMasterIndex(env),
    getFighterCardsData(env),
    getAllRankedFighterCards(env),
  ]);
  const rows =
    primaryRows.length > 0
      ? primaryRows
      : await readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, columns);

  const seenLegacy = new Set<string>();
  const fights: UpcomingFightRow[] = rows
    .map((row): UpcomingFightRow | null => {
      const eventName = cleanStr(row.event_name);
      const fighterName = cleanStr(row.fighter_name_display);
      if (!eventName || !fighterName) return null;
      const opponentName = cleanStr(row.opponent_name_display);
      const dedupeKey = `${eventName}::${fighterName}::${opponentName}`;
      if (seenLegacy.has(dedupeKey)) return null;
      seenLegacy.add(dedupeKey);
      const fighterCountry = countryName(countryIndex.byAlias, row.fighter_country);
      const opponentCountry = countryName(countryIndex.byAlias, row.opponent_country);
      return {
        event_name: eventName,
        event_date: coerceDate(row.event_date),
        location: cleanStr(row.location),
        fighter_name_display: fighterName,
        opponent_name_display: opponentName,
        weight_class: cleanStr(row.weight_class),
        fighter_country: fighterCountry,
        opponent_country: opponentCountry,
        fighter_flag: flagEmojiFor(countryIndex.byAlias, row.fighter_country),
        opponent_flag: flagEmojiFor(countryIndex.byAlias, row.opponent_country),
        fighter_is_champion: coerceBool(row.fighter_is_champion),
        opponent_is_champion: coerceBool(row.opponent_is_champion),
        fighter_status: cleanStr(row.fighter_status),
        opponent_status: cleanStr(row.opponent_status),
        fighter_wins: coerceInt(row.fighter_wins),
        fighter_losses: coerceInt(row.fighter_losses),
        opponent_wins: coerceInt(row.opponent_wins),
        opponent_losses: coerceInt(row.opponent_losses),
        fighter_finish_rate: coerceNum(row.fighter_finish_rate),
        opponent_finish_rate: coerceNum(row.opponent_finish_rate),
        fighter_sub_rate: coerceNum(row.fighter_sub_rate),
        opponent_sub_rate: coerceNum(row.opponent_sub_rate),
        fighter_win_streak: coerceInt(row.fighter_win_streak),
        fighter_loss_streak: coerceInt(row.fighter_loss_streak),
        opponent_win_streak: coerceInt(row.opponent_win_streak),
        opponent_loss_streak: coerceInt(row.opponent_loss_streak),
        model_prob: coerceNum(row.model_prob),
        market_prob: coerceNum(row.market_prob),
        edge: coerceNum(row.edge),
        signal_strength: cleanStr(row.signal_strength),
        recommended_bet: coerceBool(row.recommended_bet),
        fighter_odds: coerceNum(row.fighter_odds),
        opponent_odds: coerceNum(row.opponent_odds),
        bet_on_name: cleanStr(row.bet_on_name),
        ...mapBettingFields(row),
      };
    })
    .filter(Boolean) as UpcomingFightRow[];
  fillUpcomingFightsFromProfiles(fights, fighterData.profiles, countryIndex);
  fillUpcomingFightsFromRankings(fights, rankedCards, countryIndex);

  cachedPredictions = {
    fights,
    debug: { fetched: rows.length > 0, fightCount: fights.length },
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedPredictions;
}

let cachedAllModels: AllModelsPredictionsData | null = null;

export async function getAllModelsPredictionsData(env?: RuntimeEnv): Promise<AllModelsPredictionsData> {
  const now = Date.now();
  if (cachedAllModels && now < cacheExpiresAt) return cachedAllModels;

  const columns = [
    'event_name', 'event_date', 'location',
    'fighter_name_display', 'opponent_name_display',
    'weight_class',
    'fighter_country', 'opponent_country',
    'fighter_is_champion', 'opponent_is_champion',
    'fighter_status', 'opponent_status',
    'fighter_wins', 'fighter_losses',
    'opponent_wins', 'opponent_losses',
    'fighter_finish_rate', 'opponent_finish_rate',
    'fighter_sub_rate', 'opponent_sub_rate',
    'fighter_win_streak', 'fighter_loss_streak',
    'opponent_win_streak', 'opponent_loss_streak',
    'model_prob', 'market_prob', 'edge',
    'signal_strength', 'recommended_bet',
    'fighter_odds', 'opponent_odds',
    'bet_on_name',
    ...BETTING_COLUMNS,
  ];

  const [catboostRows, ensembleRows, logregRows, countryIndex, fighterData, rankedCards] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, columns),
    readLatestDashboardRows(env, FOLDER_UPCOMING_ENSEMBLE, columns),
    readLatestDashboardRows(env, FOLDER_UPCOMING_LOGREG, columns),
    getCountryMasterIndex(env),
    getFighterCardsData(env),
    getAllRankedFighterCards(env),
  ]);

  function normalizeRows(rows: Record<string, any>[], fallbackRows: Record<string, any>[]): UpcomingFightRow[] {
    const source = rows.length > 0 ? rows : fallbackRows;
    // The upstream parquet currently emits each matchup many times (~90× duplicates with identical
    // payloads). Dedupe by (event, fighter, opponent) so the inline JSON doesn't ship 5K+ rows.
    const seen = new Set<string>();
    return source
      .map((row): UpcomingFightRow | null => {
        const eventName = cleanStr(row.event_name);
        const fighterName = cleanStr(row.fighter_name_display);
        if (!eventName || !fighterName) return null;
        const opponentName = cleanStr(row.opponent_name_display);
        const dedupeKey = `${eventName}::${fighterName}::${opponentName}`;
        if (seen.has(dedupeKey)) return null;
        seen.add(dedupeKey);
        const fighterCountry = countryName(countryIndex.byAlias, row.fighter_country);
        const opponentCountry = countryName(countryIndex.byAlias, row.opponent_country);
        return {
          event_name: eventName,
          event_date: coerceDate(row.event_date),
          location: cleanStr(row.location),
          fighter_name_display: fighterName,
          opponent_name_display: cleanStr(row.opponent_name_display),
          weight_class: cleanStr(row.weight_class),
          fighter_country: fighterCountry,
          opponent_country: opponentCountry,
          fighter_flag: flagEmojiFor(countryIndex.byAlias, row.fighter_country),
          opponent_flag: flagEmojiFor(countryIndex.byAlias, row.opponent_country),
          fighter_is_champion: coerceBool(row.fighter_is_champion),
          opponent_is_champion: coerceBool(row.opponent_is_champion),
          fighter_status: cleanStr(row.fighter_status),
          opponent_status: cleanStr(row.opponent_status),
          fighter_wins: coerceInt(row.fighter_wins),
          fighter_losses: coerceInt(row.fighter_losses),
          opponent_wins: coerceInt(row.opponent_wins),
          opponent_losses: coerceInt(row.opponent_losses),
          fighter_finish_rate: coerceNum(row.fighter_finish_rate),
          opponent_finish_rate: coerceNum(row.opponent_finish_rate),
          fighter_sub_rate: coerceNum(row.fighter_sub_rate),
          opponent_sub_rate: coerceNum(row.opponent_sub_rate),
          fighter_win_streak: coerceInt(row.fighter_win_streak),
          fighter_loss_streak: coerceInt(row.fighter_loss_streak),
          opponent_win_streak: coerceInt(row.opponent_win_streak),
          opponent_loss_streak: coerceInt(row.opponent_loss_streak),
          model_prob: coerceNum(row.model_prob),
          market_prob: coerceNum(row.market_prob),
          edge: coerceNum(row.edge),
          signal_strength: cleanStr(row.signal_strength),
          recommended_bet: coerceBool(row.recommended_bet),
          fighter_odds: coerceNum(row.fighter_odds),
          opponent_odds: coerceNum(row.opponent_odds),
          bet_on_name: cleanStr(row.bet_on_name),
          ...mapBettingFields(row),
        };
      })
      .filter(Boolean) as UpcomingFightRow[];
  }

  const primaryRows = await readLatestDashboardRows(env, FOLDER_UPCOMING, columns);
  const catboost = normalizeRows(catboostRows, primaryRows);
  const ensemble = normalizeRows(ensembleRows, catboostRows);
  const logreg = normalizeRows(logregRows, catboostRows);
  fillUpcomingFightsFromProfiles(catboost, fighterData.profiles, countryIndex);
  fillUpcomingFightsFromProfiles(ensemble, fighterData.profiles, countryIndex);
  fillUpcomingFightsFromProfiles(logreg, fighterData.profiles, countryIndex);
  fillUpcomingFightsFromRankings(catboost, rankedCards, countryIndex);
  fillUpcomingFightsFromRankings(ensemble, rankedCards, countryIndex);
  fillUpcomingFightsFromRankings(logreg, rankedCards, countryIndex);
  cachedAllModels = {
    catboost,
    ensemble,
    logreg,
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedAllModels;
}

// ── Fight Lab ─────────────────────────────────────────────────────────────────

export interface FightLabRow {
  event_date: string;
  event_name: string;
  fighter_name_display: string;
  fighter_country: string;
  fighter_flag: string;
  opponent_name_display: string;
  opponent_country: string;
  opponent_flag: string;
  bet_on_name: string;
  winner_name_display: string;
  model_correct: boolean | null;
  model_prob: number | null;
  market_prob: number | null;
  edge: number | null;
  signal_strength: string;
  weight_class: string;
}

export interface ModelStats {
  accuracy: number | null;
  total_fights: number | null;
  correct_picks: number | null;
  wrong_picks: number | null;
  f1: number | null;
  auc: number | null;
  brier: number | null;
  log_loss: number | null;
  events_covered: number | null;
}

export interface FightLabModelData {
  rows: FightLabRow[];
  stats: ModelStats;
}

export interface FightLabData {
  catboost: FightLabModelData;
  ensemble: FightLabModelData;
  logreg: FightLabModelData;
}

let cachedFightLab: FightLabData | null = null;

function normalizeFightLabRow(
  row: Record<string, any>,
  countryIndex: Record<string, { canonical_name: string; iso2?: string }>,
): FightLabRow | null {
  const fighterName = cleanStr(row.fighter_name_display);
  if (!fighterName) return null;
  const fighterCountry = countryName(countryIndex, row.fighter_country);
  const opponentCountry = countryName(countryIndex, row.opponent_country);
  return {
    event_date: coerceDate(row.event_date),
    event_name: cleanStr(row.event_name),
    fighter_name_display: fighterName,
    fighter_country: fighterCountry,
    fighter_flag: flagEmojiFor(countryIndex, row.fighter_country),
    opponent_name_display: cleanStr(row.opponent_name_display),
    opponent_country: opponentCountry,
    opponent_flag: flagEmojiFor(countryIndex, row.opponent_country),
    bet_on_name: cleanStr(row.bet_on_name),
    winner_name_display: cleanStr(row.winner_name_display),
    model_correct: row.model_correct == null ? null : coerceBool(row.model_correct),
    model_prob: coerceNum(row.model_prob),
    market_prob: coerceNum(row.market_prob),
    edge: coerceNum(row.edge),
    signal_strength: cleanStr(row.signal_strength),
    weight_class: cleanStr(row.weight_class),
  };
}

function firstNum(row: Record<string, any>, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = coerceNum(row[k]);
    if (v != null) return v;
  }
  return null;
}
function firstInt(row: Record<string, any>, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = coerceInt(row[k]);
    if (v != null) return v;
  }
  return null;
}

function normalizeModelStats(rows: Record<string, any>[]): ModelStats {
  const row = rows[0] ?? {};
  return {
    accuracy:       firstNum(row, 'accuracy', 'acc', 'model_accuracy'),
    total_fights:   firstInt(row, 'total_fights', 'total_picks', 'n_fights', 'count', 'total'),
    correct_picks:  firstInt(row, 'correct_picks', 'model_correct_count', 'n_correct', 'correct'),
    wrong_picks:    firstInt(row, 'wrong_picks', 'model_wrong_count', 'n_wrong', 'wrong'),
    f1:             firstNum(row, 'f1', 'f1_score', 'f1score', 'f_1'),
    auc:            firstNum(row, 'auc', 'roc_auc', 'auc_score', 'auroc'),
    brier:          firstNum(row, 'brier', 'brier_score', 'brier_loss'),
    log_loss:       firstNum(row, 'log_loss', 'logloss', 'cross_entropy'),
    events_covered: firstInt(row, 'events_covered', 'event_count', 'n_events', 'events'),
  };
}

function mergeModelStats(primary: ModelStats, fallback: Partial<ModelStats>): ModelStats {
  return {
    accuracy: primary.accuracy ?? fallback.accuracy ?? null,
    total_fights: primary.total_fights ?? fallback.total_fights ?? null,
    correct_picks: primary.correct_picks ?? fallback.correct_picks ?? null,
    wrong_picks: primary.wrong_picks ?? fallback.wrong_picks ?? null,
    f1: primary.f1 ?? fallback.f1 ?? null,
    auc: primary.auc ?? fallback.auc ?? null,
    brier: primary.brier ?? fallback.brier ?? null,
    log_loss: primary.log_loss ?? fallback.log_loss ?? null,
    events_covered: primary.events_covered ?? fallback.events_covered ?? null,
  };
}

function computeFightLabMetrics(rows: FightLabRow[]): Partial<ModelStats> {
  const invalidWinners = new Set(['', 'nan', 'nat', 'none', 'draw', 'no contest']);
  const pickCorrectCount = rows.filter((row) => row.model_correct === true).length;
  const pickWrongCount = rows.filter((row) => row.model_correct === false).length;
  const pickAccuracy = rows.length > 0 && pickCorrectCount + pickWrongCount > 0
    ? pickCorrectCount / rows.length
    : null;
  const scored = rows
    .map((row) => {
      const fighterName = row.fighter_name_display.trim();
      const winnerName = row.winner_name_display.trim();
      const winnerKey = winnerName.toLowerCase();
      const probRaw = row.model_prob;
      if (!fighterName || invalidWinners.has(winnerKey) || probRaw == null || Number.isNaN(Number(probRaw))) {
        return null;
      }
      const probability = Math.max(0, Math.min(1, Number(probRaw)));
      const actual = winnerName === fighterName ? 1 : 0;
      const predicted = probability >= 0.5 ? 1 : 0;
      return { probability, actual, predicted };
    })
    .filter(Boolean) as Array<{ probability: number; actual: number; predicted: number }>;

  const n = scored.length;
  if (!n) {
    return {
      accuracy: pickAccuracy,
      total_fights: rows.length,
      correct_picks: pickCorrectCount,
      wrong_picks: pickWrongCount,
      f1: null,
      auc: null,
      brier: null,
      log_loss: null,
    };
  }

  let correct = 0;
  let tp = 0;
  let fp = 0;
  let fn = 0;
  let brierSum = 0;
  let logLossSum = 0;
  const eps = 1e-6;

  for (const item of scored) {
    if (item.predicted === item.actual) correct += 1;
    if (item.predicted === 1 && item.actual === 1) tp += 1;
    if (item.predicted === 1 && item.actual === 0) fp += 1;
    if (item.predicted === 0 && item.actual === 1) fn += 1;
    brierSum += (item.probability - item.actual) ** 2;
    const clipped = Math.max(eps, Math.min(1 - eps, item.probability));
    logLossSum += -(item.actual * Math.log(clipped) + (1 - item.actual) * Math.log(1 - clipped));
  }

  const precision = tp + fp > 0 ? tp / (tp + fp) : null;
  const recall = tp + fn > 0 ? tp / (tp + fn) : null;
  const f1 = precision != null && recall != null && precision + recall > 0
    ? (2 * precision * recall) / (precision + recall)
    : null;

  const positives = scored.filter((item) => item.actual === 1).length;
  const negatives = scored.length - positives;
  let auc: number | null = null;
  if (positives > 0 && negatives > 0) {
    const sorted = [...scored].sort((a, b) => a.probability - b.probability);
    let rankSumPositive = 0;
    let idx = 0;
    while (idx < sorted.length) {
      let end = idx + 1;
      while (end < sorted.length && sorted[end].probability === sorted[idx].probability) end += 1;
      const averageRank = (idx + 1 + end) / 2;
      for (let i = idx; i < end; i += 1) {
        if (sorted[i].actual === 1) rankSumPositive += averageRank;
      }
      idx = end;
    }
    auc = (rankSumPositive - (positives * (positives + 1)) / 2) / (positives * negatives);
    auc = Math.max(0, Math.min(1, auc));
  }

  return {
    accuracy: pickAccuracy ?? correct / n,
    total_fights: rows.length,
    correct_picks: pickCorrectCount || correct,
    wrong_picks: pickWrongCount || (n - correct),
    f1,
    auc,
    brier: brierSum / n,
    log_loss: logLossSum / n,
    events_covered: new Set(rows.map((row) => row.event_name).filter(Boolean)).size,
  };
}

export async function getFightLabData(env?: RuntimeEnv): Promise<FightLabData> {
  const now = Date.now();
  if (cachedFightLab && now < cacheExpiresAt) return cachedFightLab;

  const histCols = [
    'event_date', 'event_name',
    'fighter_name_display', 'fighter_country',
    'opponent_name_display', 'opponent_country',
    'bet_on_name', 'winner_name_display',
    'model_correct', 'model_prob', 'market_prob', 'edge',
    'signal_strength', 'weight_class',
  ];
  const statsCols = [
    'accuracy', 'total_fights', 'correct_picks', 'wrong_picks',
    'f1', 'f1_score', 'auc', 'roc_auc', 'brier', 'brier_score',
    'log_loss', 'events_covered', 'event_count',
    'model_correct_count', 'model_wrong_count',
  ];

  const [cbRows, ensRows, lrRows, cbStats, ensStats, lrStats, countryIndex] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_HISTORICAL_CATBOOST, histCols),
    readLatestDashboardRows(env, FOLDER_HISTORICAL_ENSEMBLE, histCols),
    readLatestDashboardRows(env, FOLDER_HISTORICAL_LOGREG, histCols),
    readLatestDashboardRows(env, FOLDER_STATS_CATBOOST),  // read all columns — schema varies by export
    readLatestDashboardRows(env, FOLDER_STATS_ENSEMBLE),
    readLatestDashboardRows(env, FOLDER_STATS_LOGREG),
    getCountryMasterIndex(env),
  ]);

  const fallbackRows = cbRows.length > 0
    ? cbRows
    : await readLatestDashboardRows(env, FOLDER_HISTORICAL, histCols);
  const fallbackStats = cbStats.length > 0
    ? cbStats
    : await readLatestDashboardRows(env, FOLDER_STATS, statsCols);

  function buildModel(
    rows: Record<string, any>[],
    stats: Record<string, any>[],
    fallbackR: Record<string, any>[],
    fallbackS: Record<string, any>[],
  ): FightLabModelData {
    const src = rows.length > 0 ? rows : fallbackR;
    const labRows = src
      .map((r) => normalizeFightLabRow(r, countryIndex.byAlias))
      .filter(Boolean) as FightLabRow[];
    labRows.sort((a, b) => dateValue(b.event_date) - dateValue(a.event_date));
    const statsFromRows = computeFightLabMetrics(labRows);
    return {
      rows: labRows,
      stats: mergeModelStats(normalizeModelStats(stats.length > 0 ? stats : fallbackS), statsFromRows),
    };
  }

  cachedFightLab = {
    catboost: buildModel(cbRows, cbStats, fallbackRows, fallbackStats),
    ensemble: buildModel(ensRows, ensStats, fallbackRows, fallbackStats),
    logreg:   buildModel(lrRows, lrStats, fallbackRows, fallbackStats),
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedFightLab;
}
