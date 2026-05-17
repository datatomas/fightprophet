import { parquetReadObjects } from 'hyparquet';
import { compressors } from 'hyparquet-compressors';
import type { RuntimeEnv } from './country-master';
import { getCountryMasterIndex } from './country-master';

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
  columns: string[],
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
    const fileRows = await parquetReadObjects({ file, columns, compressors });
    rows.push(...fileRows as Record<string, any>[]);
  }

  return rows;
}

async function readLatestDashboardRows(
  env: RuntimeEnv | undefined,
  folder: string,
  columns: string[],
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
  ];

  const [eventsRows, upcomingPrimary, historicalPrimary] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_EVENTS, eventColumns),
    readLatestDashboardRows(env, FOLDER_UPCOMING, fightColumns),
    readLatestDashboardRows(env, FOLDER_HISTORICAL, fightColumns),
  ]);

  const [upcomingFallback, historicalFallback] = await Promise.all([
    upcomingPrimary.length > 0 ? Promise.resolve([]) : readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, fightColumns),
    historicalPrimary.length > 0 ? Promise.resolve([]) : readLatestDashboardRows(env, FOLDER_HISTORICAL_CATBOOST, fightColumns),
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
      const currentBelts = cleanStr(row.current_belt_weight_classes) || cleanStr(row.belt);
      const profile: FighterProfile = {
        fighter_id: cleanStr(row.fighter_id),
        name,
        country,
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
  ];

  const [primaryRows, countryIndex] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_UPCOMING, columns),
    getCountryMasterIndex(env),
  ]);
  const rows =
    primaryRows.length > 0
      ? primaryRows
      : await readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, columns);

  const fights: UpcomingFightRow[] = rows
    .map((row): UpcomingFightRow | null => {
      const eventName = cleanStr(row.event_name);
      const fighterName = cleanStr(row.fighter_name_display);
      if (!eventName || !fighterName) return null;
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
      };
    })
    .filter(Boolean) as UpcomingFightRow[];

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
  ];

  const [catboostRows, ensembleRows, logregRows, countryIndex] = await Promise.all([
    readLatestDashboardRows(env, FOLDER_UPCOMING_CATBOOST, columns),
    readLatestDashboardRows(env, FOLDER_UPCOMING_ENSEMBLE, columns),
    readLatestDashboardRows(env, FOLDER_UPCOMING_LOGREG, columns),
    getCountryMasterIndex(env),
  ]);

  function normalizeRows(rows: Record<string, any>[], fallbackRows: Record<string, any>[]): UpcomingFightRow[] {
    const source = rows.length > 0 ? rows : fallbackRows;
    return source
      .map((row): UpcomingFightRow | null => {
        const eventName = cleanStr(row.event_name);
        const fighterName = cleanStr(row.fighter_name_display);
        if (!eventName || !fighterName) return null;
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
        };
      })
      .filter(Boolean) as UpcomingFightRow[];
  }

  const primaryRows = await readLatestDashboardRows(env, FOLDER_UPCOMING, columns);
  cachedAllModels = {
    catboost: normalizeRows(catboostRows, primaryRows),
    ensemble: normalizeRows(ensembleRows, catboostRows),
    logreg: normalizeRows(logregRows, catboostRows),
  };
  cacheExpiresAt = now + cacheTtlMilliseconds(env);
  return cachedAllModels;
}
