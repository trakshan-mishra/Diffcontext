import React from 'react';

const metrics = [
  { value: '423', label: 'Real commits benchmarked' },
  { value: '159', label: 'Automated tests' },
  { value: 'Python', label: 'Production ready' },
  { value: 'TypeScript', label: 'Prototype support' },
];

export default function Metrics() {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 sm:gap-8 my-12">
      {metrics.map((metric, i) => (
        <div key={i} className="flex flex-col items-center justify-center p-6 bg-slate-50 dark:bg-slate-900 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
          <div className="text-3xl sm:text-4xl font-bold text-slate-900 dark:text-white mb-2">
            {metric.value}
          </div>
          <div className="text-sm text-slate-600 dark:text-slate-400 font-medium">
            {metric.label}
          </div>
        </div>
      ))}
    </div>
  );
}
