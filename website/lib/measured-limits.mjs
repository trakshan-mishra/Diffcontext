/**
 * The measured limits of DiffContext — the single source for both renderings.
 *
 * These four claims are the ones a reader most often walks away without, because
 * every one of them qualifies a headline number that is easier to repeat. They
 * are rendered in two places, both generated from this file:
 *
 *   - components/MeasuredLimits.jsx -> the "Measured limits" section on /docs
 *   - scripts/build-llms.mjs        -> the same block in /llms.txt
 *
 * Keep the text here and nowhere else. `text` is inline markdown: `**bold**` and
 * `` `code` `` are the only markup either renderer understands.
 */

export const MEASURED_LIMITS_INTRO =
  'Claims on this site are measured against real commit history, and the measured limits are published alongside the wins. Four that a summary should not omit:'

export const MEASURED_LIMITS = [
  {
    id: 'precision',
    text:
      'Recall is the strength (hybrid ~0.70 mean vs 0.56 call-graph-only); **precision is under 0.1** at the default top-k, so retrieved context is a wide net of supporting code, not a curated shortlist. `--cutoff gap` trades recall for precision, and the size of that trade depends on the benchmark: roughly 4× precision for ~30% relative recall on the co-change benchmark, 2.2× for ~14% on ContextBench.',
    href: '/docs/benchmarks',
  },
  {
    id: 'verify-proxy',
    text:
      'The `verify` sufficiency score is a **structural proxy, not a probability**, and it has **zero discriminating power on TypeScript** today.',
    href: '/docs/verify',
  },
  {
    id: 'calibration',
    text:
      'Calibration is a ranking signal (r≈0.29 measured), not a confidence guarantee.',
    href: '/docs/verify',
  },
  {
    id: 'downstream-pass1',
    text:
      'Context roughly quadruples pass@1 (5.5% → 25.8%, McNemar p < 0.0001) — but the three context variants are statistically indistinguishable from each other (p = 0.36–0.81) at n=128.',
    href: '/docs/benchmarks',
  },
]
