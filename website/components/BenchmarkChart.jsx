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

const data = [
  { budget: '1k', Grep: 28, BM25: 35, Graph: 38, Hybrid: 45 },
  { budget: '2k', Grep: 32, BM25: 48, Graph: 52, Hybrid: 62 },
  { budget: '4k', Grep: 35, BM25: 58, Graph: 66, Hybrid: 76 },
  { budget: '8k', Grep: 37, BM25: 68, Graph: 78, Hybrid: 88 },
  { budget: '16k', Grep: 38, BM25: 74, Graph: 83, Hybrid: 94 },
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
        <p className="text-sm text-slate-500 dark:text-slate-400">Hybrid retrieval achieves ~2× the recall of grep across all context sizes.</p>
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
            <Line type="monotone" dataKey="Grep" stroke="#ef4444" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
            <Line type="monotone" dataKey="BM25" stroke="#f59e0b" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
            <Line type="monotone" dataKey="Graph" stroke="#3b82f6" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
            <Line type="monotone" dataKey="Hybrid" stroke="#10b981" strokeWidth={4} dot={{ r: 5 }} activeDot={{ r: 7 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
