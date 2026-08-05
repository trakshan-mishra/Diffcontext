"use client";

import React, { useState, useEffect } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer
} from 'recharts';

// Measured, not illustrative: recall of the true co-change partners inside the
// packed window, 30 real co-change queries from black's history
// (benchmarks/budget_head2head.py; table on /docs/benchmarks). Percent of the
// recall figures 0.083/0.145/0.215/0.215 and 0.122/0.282/0.408/0.576. Only
// these four budgets and these two arms have been measured — do not add a
// series or a budget here without a benchmark run behind it.
const data = [
  { budget: '1k', 'grep-packing': 8.3, DiffContext: 12.2 },
  { budget: '2k', 'grep-packing': 14.5, DiffContext: 28.2 },
  { budget: '4k', 'grep-packing': 21.5, DiffContext: 40.8 },
  { budget: '8k', 'grep-packing': 21.5, DiffContext: 57.6 },
];

export default function BenchmarkChart() {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return <div className="w-full h-[400px] bg-slate-50 dark:bg-slate-900/50 rounded-xl animate-pulse"></div>;
  }

  return (
    <div className="w-full my-8 p-6 bg-white dark:bg-slate-950 rounded-2xl border border-slate-200 dark:border-slate-800 shadow-sm">
      <div className="mb-6">
        <h3 className="text-lg font-bold text-slate-900 dark:text-white">Retrieval Recall vs Token Budget</h3>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Recall of the true co-change partners inside the packed window, on 30 real
          queries from black&apos;s history. grep <strong>plateaus</strong> past 4k tokens —
          name-matching cannot find partners that never mention the name — while
          graph+BM25 retrieval keeps climbing, to 2.7× at 8k.
        </p>
      </div>
      <div className="h-[400px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" opacity={0.5} />
            <XAxis dataKey="budget" stroke="#64748b" tick={{ fill: '#64748b' }} tickLine={false} axisLine={false} />
            <YAxis stroke="#64748b" tick={{ fill: '#64748b' }} tickLine={false} axisLine={false} tickFormatter={(value) => `${value}%`} />
            <Tooltip
              contentStyle={{ backgroundColor: '#1e293b', border: 'none', borderRadius: '8px', color: '#f8fafc' }}
              itemStyle={{ color: '#f8fafc' }}
              labelStyle={{ color: '#94a3b8', marginBottom: '4px' }}
            />
            <Legend wrapperStyle={{ paddingTop: '20px' }} />
            <Line type="monotone" dataKey="grep-packing" stroke="#ef4444" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
            <Line type="monotone" dataKey="DiffContext" stroke="#10b981" strokeWidth={4} dot={{ r: 5 }} activeDot={{ r: 7 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
