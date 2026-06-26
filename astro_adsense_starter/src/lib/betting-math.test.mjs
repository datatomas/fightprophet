// Parity test: the client betting math must match the canonical Python module
// (betting_math.py) on the SAME known-example fixtures as test_betting_math.py.
// Run: node src/lib/betting-math.test.mjs
import assert from 'node:assert/strict';
import * as bm from './betting-math.mjs';

const close = (a, b, tol = 1e-6) => Number.isFinite(a) && Math.abs(a - b) < tol;

const tests = {
  implied_positive: () => assert.ok(close(bm.impliedProbFromAmerican(150), 0.4)),
  implied_negative: () => assert.ok(close(bm.impliedProbFromAmerican(-200), 200 / 300)),
  implied_invalid: () => assert.ok(Number.isNaN(bm.impliedProbFromAmerican(0))),
  two_way_hold: () => {
    const p = bm.impliedProbFromAmerican(-110);
    assert.ok(close(bm.marketHold(p, p), 2 * (110 / 210) - 1));
  },
  hold_missing_side: () => {
    const p = bm.impliedProbFromAmerican(-110);
    assert.ok(Number.isNaN(bm.marketHold(p, NaN)));
  },
  devig_fair: () => {
    const p = bm.impliedProbFromAmerican(-110);
    assert.ok(close(bm.devigFairProb(p, p, p), 0.5));
  },
  fair_american: () => {
    assert.ok(close(bm.fairAmericanOdds(0.5), 100));
    assert.ok(close(bm.fairAmericanOdds(0.4), 150));
    assert.ok(close(bm.fairAmericanOdds(0.6), -150));
  },
  expected_value: () => assert.ok(close(bm.expectedValue(0.55, -110), 5.0, 1e-3)),
  expected_roi: () => assert.ok(close(bm.expectedRoi(0.55, -110), 0.05, 1e-5)),
  edge: () => assert.ok(close(bm.edge(0.55, 0.5), 0.05)),
  parlay_probability: () => {
    assert.ok(close(bm.parlayProbability([0.5, 0.5]), 0.25));
    assert.ok(close(bm.parlayProbability([0.6, 0.5, 0.4]), 0.12));
  },
  parlay_fair_odds: () => assert.ok(close(bm.parlayFairOdds([0.5, 0.5]), 300)),
  parlay_payout: () => {
    assert.ok(close(bm.parlayDecimalPayout([100, 100]), 4));
    assert.ok(close(bm.parlayAmericanOdds([100, 100]), 300));
  },
  parlay_ev_roi: () => {
    assert.ok(close(bm.parlayExpectedValue([0.5, 0.5], [100, 100]), 0.0));
    assert.ok(close(bm.parlayRoi([0.5, 0.5], [100, 100]), 0.0));
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log(`ok  ${name}`);
  } catch (e) {
    failed += 1;
    console.error(`FAIL ${name}: ${e.message}`);
  }
}
if (failed) {
  console.error(`\n${failed} parity test(s) failed.`);
  process.exit(1);
}
console.log(`\nAll ${Object.keys(tests).length} TS↔Python parity tests passed.`);
