import React from 'react';
import { GitCommit, FileCode2, Network, Scissors, FileJson, ArrowRight } from 'lucide-react';

const steps = [
  { icon: <GitCommit className="w-6 h-6 text-blue-500" />, title: "git change", desc: "" },
  { icon: <FileCode2 className="w-6 h-6 text-indigo-500" />, title: "changed functions", desc: "" },
  { icon: <Network className="w-6 h-6 text-purple-500" />, title: "hybrid retrieval", desc: "graph ∪ BM25 ∪ file" },
  { icon: <Scissors className="w-6 h-6 text-pink-500" />, title: "token budget", desc: "top-k + tokens" },
  { icon: <FileJson className="w-6 h-6 text-emerald-500" />, title: "LLM-ready context", desc: "" }
];

export default function Pipeline() {
  return (
    <div className="w-full my-12 overflow-x-auto pb-4">
      <div className="min-w-[800px] flex items-center justify-between px-4 bg-slate-50 dark:bg-slate-900/50 rounded-2xl border border-slate-100 dark:border-slate-800 p-8">
        {steps.map((step, index) => (
          <React.Fragment key={index}>
            <div className="flex flex-col items-center text-center max-w-[140px]">
              <div className="w-16 h-16 rounded-2xl bg-white dark:bg-slate-800 shadow-sm border border-slate-200 dark:border-slate-700 flex items-center justify-center mb-4 transition-transform hover:scale-105">
                {step.icon}
              </div>
              <div className="font-semibold text-sm text-slate-900 dark:text-slate-100 mb-1">
                {step.title}
              </div>
              {step.desc && (
                <div className="text-xs text-slate-500 dark:text-slate-400">
                  {step.desc}
                </div>
              )}
            </div>
            
            {index < steps.length - 1 && (
              <div className="flex-1 flex justify-center text-slate-300 dark:text-slate-600 px-2">
                <ArrowRight className="w-5 h-5" />
              </div>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}
