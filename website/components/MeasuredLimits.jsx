import React from 'react';
import { MEASURED_LIMITS, MEASURED_LIMITS_INTRO } from '../lib/measured-limits.mjs';

// The limit text is inline markdown, so that scripts/build-llms.mjs can emit the
// same strings verbatim. Only `**bold**` and `` `code` `` are supported.
function inline(text) {
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  const out = [];
  let last = 0;
  let match;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[1] !== undefined) {
      out.push(
        <strong key={match.index} className="font-semibold text-slate-900 dark:text-white">
          {match[1]}
        </strong>
      );
    } else {
      out.push(
        // nowrap: `--cutoff gap` broke mid-flag at narrow widths, reading as a
        // different flag entirely.
        <code key={match.index} className="text-[0.9em] whitespace-nowrap">
          {match[2]}
        </code>
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));

  return out;
}

export default function MeasuredLimits() {
  return (
    <div className="my-8 p-6 bg-slate-50 dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800">
      <p className="text-sm text-slate-600 dark:text-slate-400 mb-4">
        {MEASURED_LIMITS_INTRO}
      </p>
      <ul className="space-y-3 list-disc pl-5 marker:text-slate-400 dark:marker:text-slate-600">
        {MEASURED_LIMITS.map((limit) => (
          <li key={limit.id} className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
            {inline(limit.text)}
            {limit.href ? (
              <>
                {' '}
                <a href={limit.href} className="whitespace-nowrap underline underline-offset-2 text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white">
                  evidence →
                </a>
              </>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
