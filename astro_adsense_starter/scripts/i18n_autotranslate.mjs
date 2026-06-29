#!/usr/bin/env node
/**
 * Auto-fill UI translations from the English source.
 *
 * English is the single source of truth: src/lib/i18n/en.json. This fills any
 * MISSING/empty keys in es/pt/ru/ka.json by machine-translating them, then the
 * routed Astro pages render them (SEO-friendly /es//pt//ru//ka/ URLs are kept).
 *
 * - Idempotent: only translates keys missing or empty in a locale, so any hand
 *   edits / prior translations are preserved. Set I18N_FORCE=1 to retranslate all.
 * - Uses the OpenAI API (cheap gpt-4o-mini by default) because it covers Georgian,
 *   which DeepL does not. For 60-odd UI strings the cost is a fraction of a cent.
 *
 * Usage:
 *   OPENAI_API_KEY=sk-... node scripts/i18n_autotranslate.mjs
 *   # also auto-loads /home/ares/.config/ml_kuda_sports_lab/pipeline.env
 *   # optional: I18N_MODEL=gpt-4o-mini  I18N_FORCE=1
 *
 * Workflow: add new strings to en.json → run this → review the JSON diffs → commit.
 */
import { readFileSync, writeFileSync } from 'node:fs';

const DIR = new URL('../src/lib/i18n/', import.meta.url);
const ENV_FILES = [
  new URL('../.env.local', import.meta.url),
  new URL('../.env', import.meta.url),
  '/home/ares/.config/ml_kuda_sports_lab/pipeline.env',
];
const LANGS = { es: 'Spanish', pt: 'Brazilian Portuguese', ru: 'Russian', ka: 'Georgian' };

function loadEnvFiles() {
  for (const file of ENV_FILES) {
    let text = '';
    try {
      text = readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const rawLine of text.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith('#')) continue;
      const match = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
      if (!match) continue;
      const [, key, rawValue] = match;
      if (process.env[key]) continue;
      let value = rawValue.trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      process.env[key] = value;
    }
  }
}

loadEnvFiles();

const MODEL = process.env.I18N_MODEL || 'gpt-4o-mini';
const FORCE = process.env.I18N_FORCE === '1';
const KEY = process.env.OPENAI_API_KEY;

if (!KEY) {
  console.error('ERROR: set OPENAI_API_KEY (or adapt translateBatch for DeepL/Google).');
  process.exit(1);
}

const read = (l) => JSON.parse(readFileSync(new URL(`${l}.json`, DIR), 'utf8'));
const en = read('en');

async function translateBatch(targetName, pairs) {
  const system =
    `You are a professional UI localizer for Fight Prophet, an MMA/UFC analytics site. ` +
    `Translate the given UI strings from English into ${targetName}. ` +
    `Return ONLY a JSON object with the SAME keys and translated values — no prose, no code fences. ` +
    `Keep these UNCHANGED: brand/proper nouns (Fight Prophet, UFC, MMA), metric abbreviations ` +
    `(EV, ROI, Edge, hold), code/formula snippets, and %/$ symbols. Keep it concise, UI-appropriate.`;
  const resp = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${KEY}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      temperature: 0,
      response_format: { type: 'json_object' },
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: JSON.stringify(pairs, null, 2) },
      ],
    }),
  });
  if (!resp.ok) throw new Error(`OpenAI API ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  const text = (data.choices?.[0]?.message?.content || '').trim();
  return JSON.parse(text);
}

let total = 0;
for (const [lang, name] of Object.entries(LANGS)) {
  const cur = read(lang);
  const missing = {};
  for (const k of Object.keys(en)) {
    if (FORCE || !cur[k]) missing[k] = en[k];
  }
  if (Object.keys(missing).length === 0) {
    console.log(`${lang}: up to date (${Object.keys(en).length} keys)`);
    continue;
  }
  console.log(`${lang}: translating ${Object.keys(missing).length} key(s) via ${MODEL}…`);
  const out = await translateBatch(name, missing);
  // rebuild in en key order; keep existing values unless FORCE
  const merged = {};
  for (const k of Object.keys(en)) merged[k] = (!FORCE && cur[k]) || out[k] || en[k];
  writeFileSync(new URL(`${lang}.json`, DIR), JSON.stringify(merged, null, 2) + '\n');
  total += Object.keys(missing).length;
  console.log(`${lang}: wrote ${Object.keys(out).length} translation(s).`);
}
console.log(`\nDone (${total} strings). Review the JSON diffs, then commit.`);
