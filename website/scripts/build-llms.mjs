#!/usr/bin/env node
/**
 * Build /llms.txt, /llms-full.txt and /llms/<slug>.md from content/*.mdx.
 *
 * Why this exists: the docs site is a Nextra static export, so a page's text
 * lives in an RSC payload rather than in the served HTML. Plain HTTP
 * retrievers (ChatGPT browsing, LLM crawlers, `curl`) fetch /docs/... and find
 * no readable prose. These files are the plain-text mirror, generated from the
 * same MDX the site renders so the two cannot drift.
 *
 * Run: `node scripts/build-llms.mjs` (also wired as the npm `prebuild` hook).
 */
import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { MEASURED_LIMITS, MEASURED_LIMITS_INTRO } from '../lib/measured-limits.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WEBSITE = path.resolve(HERE, '..')
const REPO = path.resolve(WEBSITE, '..')
const CONTENT = path.join(WEBSITE, 'content')
const PUBLIC = path.join(WEBSITE, 'public')
const OUT_MD = path.join(PUBLIC, 'llms')

const SITE = 'https://diffcontext-docs.pages.dev'
const GITHUB = 'https://github.com/trakshan-mishra/Diffcontext'

/**
 * Hard-wrap `text` for the plain-text mirror.
 *
 * `first` prefixes the opening line, `rest` every continuation line, so a
 * bullet wraps under its own text rather than under the marker.
 */
function wrap(text, { width = 78, first = '', rest = '' } = {}) {
  const lines = []
  let line = first
  let started = false

  for (const word of text.split(/\s+/).filter(Boolean)) {
    if (!started) {
      line += word
      started = true
    } else if (`${line} ${word}`.length > width) {
      lines.push(line)
      line = rest + word
    } else {
      line += ` ${word}`
    }
  }
  if (started) lines.push(line)

  return lines
}

/** The measured-limits block as markdown lines, from lib/measured-limits.mjs. */
function measuredLimitLines() {
  return [
    ...wrap(MEASURED_LIMITS_INTRO),
    '',
    ...MEASURED_LIMITS.flatMap((limit) => wrap(limit.text, { first: '- ', rest: '  ' })),
  ]
}

// ---------------------------------------------------------------------------
// JSX components have no text in the MDX source, so their rendered content is
// spelled out here. Each entry is transcribed from the component that renders
// it on the page — keep them in sync by hand; there are four. The fifth,
// MeasuredLimits, is generated from the same module the component renders, so
// the limits published here and on the page cannot disagree.
// ---------------------------------------------------------------------------
const COMPONENT_TEXT = {
  // components/Hero.jsx
  Hero: [
    '# DiffContext',
    '',
    '**Repository-aware Context Compiler for LLM Coding Agents.**',
    '',
    'Parse once. Retrieve precisely. Fit any context window. Find the code that',
    "matters for a change, and fit it into an LLM's context window — automatically.",
  ].join('\n'),

  // components/Metrics.jsx
  Metrics: [
    'At a glance: **423** real commits benchmarked · **189** automated tests ·',
    '**Python** production ready · **TypeScript** prototype support.',
  ].join('\n'),

  // components/Pipeline.jsx
  Pipeline: [
    '```',
    'git change ──► changed functions ──► hybrid retrieval ──► token budget ──► LLM-ready context',
    '                                     graph ∪ BM25 ∪ file      top-k + tokens',
    '```',
  ].join('\n'),

  // components/BenchmarkChart.jsx renders a "Retrieval Recall vs Token Budget"
  // line chart of the numbers below; the mirror carries them as a table.
  BenchmarkChart: [
    '**Retrieval recall vs token budget.** Measured head-to-head against',
    "grep-packing on 30 real co-change queries from black's history — recall of",
    'the true co-change partners inside the packed window:',
    '',
    '| Token budget | grep-packing | DiffContext |',
    '|---|---|---|',
    '| 1,000 | 0.083 | 0.122 |',
    '| 2,000 | 0.145 | 0.282 |',
    '| 4,000 | 0.215 | 0.408 |',
    '| 8,000 | 0.215 (plateau) | 0.576 |',
    '',
    `Full table, methodology and caveats: ${SITE}/docs/benchmarks`,
  ].join('\n'),

  // components/MeasuredLimits.jsx — generated, not transcribed.
  MeasuredLimits: measuredLimitLines().join('\n'),
}

// ---------------------------------------------------------------------------
// MDX → markdown
// ---------------------------------------------------------------------------

/** Split `---\ntitle: ...\n---` frontmatter off the body. */
function splitFrontmatter(src) {
  const m = src.match(/^---\n([\s\S]*?)\n---\n/)
  if (!m) return { meta: {}, body: src }
  const meta = {}
  for (const line of m[1].split('\n')) {
    const kv = line.match(/^(\w+):\s*(.*)$/)
    // Strip surrounding quotes only when the value is quoted end to end — the
    // verify page's description ends in a quoted question, not a quoted value.
    if (kv) meta[kv[1]] = kv[2].trim().replace(/^(['"])([\s\S]*)\1$/, '$2')
  }
  return { meta, body: src.slice(m[0].length) }
}

/** Remove the common leading indentation of a block lifted out of a component. */
function dedent(block) {
  const lines = block.split('\n')
  const indents = lines
    .filter((l) => l.trim() !== '')
    .map((l) => l.match(/^[ \t]*/)[0].length)
  const cut = indents.length ? Math.min(...indents) : 0
  return lines.map((l) => l.slice(cut)).join('\n')
}

const CALLOUT_LABEL = {
  info: 'Note',
  warning: 'Warning',
  error: 'Warning',
  important: 'Important',
  default: 'Note',
}

function transform(body) {
  let out = body

  // `import { Callout } from 'nextra/components'` etc.
  out = out.replace(/^import .*$/gm, '')

  // Self-closing content components (Hero, Metrics, Pipeline, BenchmarkChart).
  out = out.replace(/<(\w+)\s*\/>/g, (match, name) =>
    name in COMPONENT_TEXT ? COMPONENT_TEXT[name] : match
  )

  // <Cards><Cards.Card title="X" href="/docs/y" /></Cards>
  out = out.replace(/<Cards>([\s\S]*?)<\/Cards>/g, (_, inner) => {
    const links = [...inner.matchAll(/<Cards\.Card\s+title="([^"]*)"\s+href="([^"]*)"/g)]
    return links.map(([, title, href]) => `- [${title.replace(/\s*→\s*$/, '')}](${href})`).join('\n')
  })

  // <Tabs items={['a', 'b']}><Tabs.Tab>…</Tabs.Tab>…</Tabs>
  out = out.replace(/<Tabs\s+items=\{\[([\s\S]*?)\]\}>([\s\S]*?)<\/Tabs>/g, (_, itemsSrc, inner) => {
    const labels = [...itemsSrc.matchAll(/'([^']*)'|"([^"]*)"/g)].map((m) => m[1] ?? m[2])
    const tabs = [...inner.matchAll(/<Tabs\.Tab>([\s\S]*?)<\/Tabs\.Tab>/g)].map((m) => dedent(m[1]).trim())
    return tabs.map((tab, i) => `**${labels[i] ?? `Option ${i + 1}`}:**\n\n${tab}`).join('\n\n')
  })

  // <Steps> only affects numbering styling; the ### headings inside carry it.
  out = out.replace(/^\s*<\/?Steps>\s*$/gm, '')

  // <Callout type="warning" emoji="⚠️">…</Callout> → blockquote
  out = out.replace(/<Callout([^>]*)>([\s\S]*?)<\/Callout>/g, (_, attrs, inner) => {
    const type = (attrs.match(/type="(\w+)"/) || [, 'default'])[1]
    const label = CALLOUT_LABEL[type] ?? CALLOUT_LABEL.default
    const text = dedent(inner).trim()
    return [`> **${label}:**`, ...text.split('\n').map((l) => (l ? `> ${l}` : '>'))].join('\n')
  })

  // Site-relative links → absolute, so the mirror is useful standalone.
  out = out.replace(/\]\(\/docs/g, `](${SITE}/docs`)

  return out.replace(/\n{3,}/g, '\n\n').trim() + '\n'
}

// ---------------------------------------------------------------------------
// Page set — order and titles come from content/_meta.js; every .mdx file must
// appear there or the build fails rather than silently dropping a page.
// ---------------------------------------------------------------------------
function readPageOrder() {
  const src = fs.readFileSync(path.join(CONTENT, '_meta.js'), 'utf8')
  const entries = [...src.matchAll(/^\s{2}'?([\w-]+)'?:\s*\{/gm)].map((m) => m[1])
  const onDisk = fs
    .readdirSync(CONTENT)
    .filter((f) => f.endsWith('.mdx'))
    .map((f) => f.replace(/\.mdx$/, ''))
  const missing = onDisk.filter((slug) => !entries.includes(slug))
  if (missing.length) {
    throw new Error(`content/_meta.js does not list: ${missing.join(', ')}`)
  }
  return entries.filter((slug) => onDisk.includes(slug))
}

function provenance() {
  const sha =
    process.env.CF_PAGES_COMMIT_SHA?.slice(0, 7) ||
    (() => {
      try {
        return execSync('git rev-parse --short HEAD', { cwd: REPO, stdio: ['ignore', 'pipe', 'ignore'] })
          .toString()
          .trim()
      } catch {
        return null
      }
    })()
  const date = new Date().toISOString().slice(0, 10)
  return sha ? `Generated ${date} from commit ${sha}.` : `Generated ${date}.`
}

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------
const slugs = readPageOrder()
const pages = slugs.map((slug) => {
  const { meta, body } = splitFrontmatter(fs.readFileSync(path.join(CONTENT, `${slug}.mdx`), 'utf8'))
  return {
    slug,
    url: slug === 'index' ? `${SITE}/docs` : `${SITE}/docs/${slug}`,
    title: meta.title ?? slug,
    description: meta.description ?? '',
    markdown: transform(body),
  }
})

// Two extra sections that are not website pages: the verified status block
// (hand-maintained next to this script) and the repository's own roadmap.
function optional(file, label) {
  try {
    return fs.readFileSync(file, 'utf8').trim() + '\n'
  } catch {
    console.warn(`[llms] skipped ${label}: ${file} not readable from this build root`)
    return null
  }
}

const status = optional(path.join(HERE, 'llms-status.md'), 'status block')
const roadmap = optional(path.join(REPO, 'docs', 'ROADMAP.md'), 'roadmap')

const extras = []
if (status) extras.push({ slug: 'status', title: 'Project status', markdown: status })
if (roadmap) {
  extras.push({
    slug: 'roadmap',
    title: 'Roadmap (later stages)',
    markdown:
      `# Roadmap (later stages)\n\nVerbatim copy of \`docs/ROADMAP.md\` in the repository.\n\n---\n\n` +
      roadmap,
  })
}

fs.mkdirSync(OUT_MD, { recursive: true })
for (const page of [...pages, ...extras]) {
  fs.writeFileSync(path.join(OUT_MD, `${page.slug}.md`), page.markdown)
}

const header = [
  '# DiffContext — full documentation',
  '',
  `> ${provenance()} Source of truth: ${GITHUB}. Rendered docs: ${SITE}/docs`,
  '',
  'This file is the complete text of the DiffContext documentation, in one',
  'plain-text file, for LLMs and other automated readers. Nothing here is',
  'summarised or paraphrased: each section is the source of a rendered docs page.',
  '',
].join('\n')

const full =
  header +
  [...pages, ...extras]
    .map((page) => `\n\n---\n\n<!-- ${page.title}${page.url ? ` · ${page.url}` : ''} -->\n\n${page.markdown}`)
    .join('')

fs.writeFileSync(path.join(PUBLIC, 'llms-full.txt'), full)

const index = [
  '# DiffContext',
  '',
  '> Repository-aware context compiler for LLM coding agents: it finds the code',
  "> that matters for a change and fits it into a model's context window, leading",
  '> with a meta header that discloses what was dropped. Python is fully',
  '> supported; TypeScript/JavaScript (ESM) is a working prototype.',
  '',
  provenance(),
  '',
  ...measuredLimitLines(),
  '',
  '## Documentation',
  '',
  ...pages.map(
    (p) => `- [${p.title}](${SITE}/llms/${p.slug}.md): ${p.description || p.title} (rendered: ${p.url})`
  ),
  '',
  '## Status and plans',
  '',
  ...(status ? [`- [Project status](${SITE}/llms/status.md): what is shipped, verified against the source tree, and how to install and use it today`] : []),
  ...(roadmap ? [`- [Roadmap](${SITE}/llms/roadmap.md): the later stages, each with the measurement that motivates it`] : []),
  '',
  '## Source',
  '',
  `- [GitHub repository](${GITHUB}): code, tests, benchmark harness and reports`,
  `- [Benchmark methodology report](${GITHUB}/blob/main/benchmarks/EVAL_V2_REPORT.md): distinct-commit sampling, baselines, bootstrap CIs, failure taxonomy`,
  `- [2026-07 rigor report](${GITHUB}/blob/main/benchmarks/RIGOR_REPORT_2026-07.md): the self-audit that retracted three published claims; newer than the docs pages where they disagree`,
  '',
  '## Optional',
  '',
  `- [llms-full.txt](${SITE}/llms-full.txt): every page above concatenated into one file`,
  '',
].join('\n')

fs.writeFileSync(path.join(PUBLIC, 'llms.txt'), index)

console.log(
  `[llms] wrote public/llms.txt, public/llms-full.txt (${(full.length / 1024).toFixed(1)} KB) and ` +
    `${pages.length + extras.length} files in public/llms/`
)
