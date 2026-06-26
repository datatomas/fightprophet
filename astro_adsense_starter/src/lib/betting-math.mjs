// Client-side mirror of the canonical backend betting math
// (src/ml_kuda_sports_lab/etl/gold/betting_math.py). Used ONLY by the
// interactive calculators on the Betting Education page — a static Cloudflare
// Pages site can't call Python at runtime. Per-fight values shown elsewhere are
// always the backend-baked numbers, never recomputed here.
//
// Kept in lock-step with the Python module by betting-math.test.mjs, which
// asserts the same known-example fixtures used by test_betting_math.py.
// Change a formula here -> change it in the Python module too.

const isNum = (x) => typeof x === 'number' && Number.isFinite(x);
const num = (x) => {
  const v = typeof x === 'number' ? x : Number(x);
  return Number.isFinite(v) ? v : NaN;
};

export function impliedProbFromAmerican(odds) {
  const o = num(odds);
  if (!isNum(o) || o === 0) return NaN;
  return o > 0 ? 100 / (o + 100) : Math.abs(o) / (Math.abs(o) + 100);
}

export function americanToDecimal(odds) {
  const o = num(odds);
  if (!isNum(o) || o === 0) return NaN;
  return o > 0 ? 1 + o / 100 : 1 + 100 / Math.abs(o);
}

export function decimalToAmerican(dec) {
  const d = num(dec);
  if (!isNum(d) || d <= 1) return NaN;
  return d >= 2 ? (d - 1) * 100 : -100 / (d - 1);
}

export function fairAmericanOdds(p) {
  const pr = num(p);
  if (!isNum(pr) || pr <= 0 || pr >= 1) return NaN;
  if (pr === 0.5) return 100;
  return pr < 0.5 ? (100 * (1 - pr)) / pr : (-100 * pr) / (1 - pr);
}

export function marketHold(fighterProb, opponentProb) {
  const pf = num(fighterProb);
  const po = num(opponentProb);
  if (!(isNum(pf) && isNum(po))) return NaN;
  return pf + po - 1;
}

export function holdLabel(hold) {
  const h = num(hold);
  if (!isNum(h)) return null;
  if (h < 0.03) return 'low';
  if (h <= 0.06) return 'medium';
  return 'high';
}

export function devigFairProb(sideProb, fighterProb, opponentProb) {
  const s = num(sideProb);
  const pf = num(fighterProb);
  const po = num(opponentProb);
  const tot = pf + po;
  if (!(isNum(s) && isNum(pf) && isNum(po)) || tot <= 0) return NaN;
  return s / tot;
}

function profitPer100(odds) {
  return odds > 0 ? odds : 10000 / Math.abs(odds);
}

export function expectedValue(modelProb, americanOdds, stake = 100) {
  const p = num(modelProb);
  const o = num(americanOdds);
  if (!(isNum(p) && isNum(o)) || o === 0) return NaN;
  const unit = stake / 100;
  const profit = profitPer100(o) * unit;
  return p * profit - (1 - p) * stake;
}

export function expectedRoi(modelProb, americanOdds, stake = 100) {
  const ev = expectedValue(modelProb, americanOdds, stake);
  if (!isNum(ev) || stake === 0) return NaN;
  return ev / stake;
}

export function edge(modelProb, fairProb) {
  const p = num(modelProb);
  const f = num(fairProb);
  if (!(isNum(p) && isNum(f))) return NaN;
  return p - f;
}

// --- Parlays ---
export function parlayProbability(probs) {
  if (!probs.length) return NaN;
  let out = 1;
  for (const p of probs) {
    const v = num(p);
    if (!isNum(v)) return NaN;
    out *= v;
  }
  return out;
}

export function parlayDecimalPayout(americanLegs) {
  if (!americanLegs.length) return NaN;
  let out = 1;
  for (const o of americanLegs) {
    const d = americanToDecimal(o);
    if (!isNum(d)) return NaN;
    out *= d;
  }
  return out;
}

export function parlayAmericanOdds(americanLegs) {
  return decimalToAmerican(parlayDecimalPayout(americanLegs));
}

export function parlayFairOdds(probs) {
  return fairAmericanOdds(parlayProbability(probs));
}

export function parlayExpectedValue(probs, americanLegs, stake = 100) {
  const p = parlayProbability(probs);
  const dec = parlayDecimalPayout(americanLegs);
  if (!(isNum(p) && isNum(dec))) return NaN;
  const profit = (dec - 1) * stake;
  return p * profit - (1 - p) * stake;
}

export function parlayRoi(probs, americanLegs, stake = 100) {
  const ev = parlayExpectedValue(probs, americanLegs, stake);
  if (!isNum(ev) || stake === 0) return NaN;
  return ev / stake;
}
