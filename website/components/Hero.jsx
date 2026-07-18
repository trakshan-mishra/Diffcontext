import React from 'react';
import Link from 'next/link';
import { ArrowRight } from 'lucide-react';

export default function Hero() {
  return (
    <div className="flex flex-col items-center text-center pt-16 pb-12 sm:pt-24 sm:pb-16 px-4">
      <h1 className="text-4xl sm:text-6xl font-extrabold tracking-tight text-slate-900 dark:text-white mb-6">
        Repository-aware Context Compiler <br className="hidden sm:block" />
        <span className="text-transparent bg-clip-text bg-gradient-to-r from-blue-600 to-indigo-500">
          for LLM Coding Agents
        </span>
      </h1>
      
      <p className="max-w-2xl text-lg sm:text-xl text-slate-600 dark:text-slate-400 mb-10 leading-relaxed font-medium">
        Parse once. Retrieve precisely. Fit any context window. <br/>
        <span className="font-normal">Find the code that matters for a change, and fit it into an LLM's context window — automatically.</span>
      </p>

      <div className="flex flex-col sm:flex-row gap-4 w-full sm:w-auto">
        <Link href="/docs/quick-start" className="flex items-center justify-center gap-2 px-8 py-3.5 text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-full transition-colors shadow-sm w-full sm:w-auto">
          Get Started
          <ArrowRight className="w-4 h-4" />
        </Link>
        <a href="https://github.com/trakshan-mishra/Diffcontext" target="_blank" rel="noopener noreferrer" className="flex items-center justify-center gap-2 px-8 py-3.5 text-sm font-semibold text-slate-700 bg-slate-100 hover:bg-slate-200 dark:text-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 rounded-full transition-colors w-full sm:w-auto">
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="currentColor">
            <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
          </svg>
          GitHub
        </a>
      </div>
    </div>
  );
}
