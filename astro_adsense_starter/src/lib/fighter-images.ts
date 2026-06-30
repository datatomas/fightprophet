import type { RuntimeEnv } from './country-master';

const DEFAULT_CONTAINER = 'ufc';
const DEFAULT_PREFIX = 'assets/fighter-images';

export const fighterImageFiles: Record<string, string> = {
  '0d8011111be000b2': '0d8011111be000b2-sean-strickland.svg',
  '17e97649403ba428': '17e97649403ba428-joshua-van.svg',
  '275aca31f61ba28c': '275aca31f61ba28c-islam=makhachev.svg',
  '399afbabc02376b5': '399afbabc02376b5-tom-aspinall.svg',
  '9014c02eff8b3d62': '9014c02eff8b3d62-carlos-ulberg.svg',
  '9e8f6c728eb01124': '9e8f6c728eb01124-justin-gaetjhe.svg',
  'd661ce4da776fc20': 'd661ce4da776fc20-petr-yan.svg',
  'e1248941344b3288': 'e1248941344b3288-alexander-volkanovski.svg',
};

const fighterImageNameAliases: Record<string, string> = {
  'sean strickland': '0d8011111be000b2',
  'joshua van': '17e97649403ba428',
  'islam makhachev': '275aca31f61ba28c',
  'tom aspinall': '399afbabc02376b5',
  'carlos ulberg': '9014c02eff8b3d62',
  'justin gaethje': '9e8f6c728eb01124',
  'petr yan': 'd661ce4da776fc20',
  'alexander volkanovski': 'e1248941344b3288',
};

function envValue(env: RuntimeEnv | undefined, key: string, fallback = ''): string {
  const value = env?.[key];
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value).trim();
  if (typeof import.meta !== 'undefined' && typeof import.meta.env?.[key] === 'string') {
    return String(import.meta.env[key]).trim();
  }
  if (typeof process !== 'undefined' && process?.env && typeof process.env[key] === 'string') {
    return String(process.env[key]).trim();
  }
  return fallback;
}

export function getFighterImagesBaseUrl(env?: RuntimeEnv): string {
  const explicit = envValue(env, 'PUBLIC_FIGHTER_IMAGES_BASE_URL') || envValue(env, 'FIGHTER_IMAGES_BASE_URL');
  if (explicit) return explicit.replace(/\/+$/, '');

  const account = envValue(env, 'PUBLIC_AZURE_STORAGE_ACCOUNT') || envValue(env, 'AZURE_STORAGE_ACCOUNT');
  if (!account) return '';

  const container =
    envValue(env, 'PUBLIC_FIGHTER_IMAGES_CONTAINER')
    || envValue(env, 'FIGHTER_IMAGES_CONTAINER')
    || DEFAULT_CONTAINER;

  return `https://${account}.blob.core.windows.net/${container}`;
}

export function fighterImageUrl(fighterId: string | undefined, baseUrl: string): string {
  const id = String(fighterId || '').trim().toLowerCase();
  const filename = fighterImageFiles[id];
  if (!filename || !baseUrl) return '';
  return `${baseUrl.replace(/\/+$/, '')}/${DEFAULT_PREFIX}/${encodeURIComponent(filename)}`;
}

export function normalizeFighterImageName(name: string | undefined): string {
  return String(name || '')
    .replace(/\([^)]*\)/g, ' ')
    .replace(/[^a-z0-9]+/gi, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase();
}

export function fighterImageUrlForFighter(fighterId: string | undefined, name: string | undefined, baseUrl: string): string {
  const byId = fighterImageUrl(fighterId, baseUrl);
  if (byId) return byId;
  const fallbackId = fighterImageNameAliases[normalizeFighterImageName(name)];
  return fighterImageUrl(fallbackId, baseUrl);
}

export function fighterImageAssetMap(baseUrl: string): Record<string, string> {
  const entries = Object.fromEntries(
    Object.keys(fighterImageFiles).map((id) => [id, fighterImageUrl(id, baseUrl)]),
  );
  for (const [name, id] of Object.entries(fighterImageNameAliases)) {
    entries[name] = fighterImageUrl(id, baseUrl);
  }
  return entries;
}
