"""
sufficiency.py — Structural sufficiency analysis of a compiled context.

The question this module answers: "given this compiled context package,
how likely is it that an LLM reading it has everything it needs to reason
about the change correctly?"

HONESTY CONTRACT (read this before trusting the score):

  The score is a STRUCTURAL PROXY, not a guarantee. True sufficiency is
  defined relative to a stochastic model and cannot be proven statically.
  What CAN be measured statically is the set of known predictors of
  insufficiency:

    1. Direct-neighbor closure — a caller/callee of a changed symbol that
       is NOT in context is the single strongest structural predictor of
       a wrong or hallucinated patch.
    2. High-score retention — symbols the ranker itself scored as relevant
       but the token budget cut. The ranker is telling us the context is
       incomplete by its own standard.
    3. Local graph confidence — unresolved edges out of the changed
       symbols mean the graph may be blind to real dependencies (dynamic
       dispatch, externals, broken files).
    4. Parse holes — files with SyntaxErrors are invisible to the graph.

  The score only becomes CALIBRATED CONFIDENCE after `diffcontext verify
  --cases/--from-history --calibrate` maps score buckets to empirically
  observed recall on your repo. Until then treat it as a ranked warning
  system, not a probability.
"""

from dataclasses import dataclass, field
from typing import List, Set

from ..models import RepositoryIndex, ImpactResult, ContextPackage

# A symbol the ranker scored at/above this is "relevant by the ranker's
# own standard" — dropping it is evidence of insufficiency. 50 sits between
# the direct-neighbor base scores (85-90) and the expanded-dep floor (30).
HIGH_SCORE_THRESHOLD = 50.0

# Component weights. Direct closure dominates because a missing direct
# neighbor is the failure mode observed most often in retrieval evals
# (see benchmarks/EVAL_V2_REPORT.md).
W_CLOSURE   = 0.45
W_RETENTION = 0.30
W_CONFIDENCE = 0.15
W_PARSE     = 0.10

VERDICT_SUFFICIENT_MIN = 80.0
VERDICT_DEGRADED_MIN   = 55.0

# Evidence saturation: how many observations a component needs before it is
# fully trusted. A component computed from zero observations (no direct
# neighbors, no ranked-relevant symbols, no outgoing edges) says NOTHING —
# the old formula scored it as a perfect 1.0, which is why sparse-graph
# repos (TypeScript especially) reported a constant 100. With no evidence
# the score now shrinks toward the maximum-uncertainty midpoint (50), and
# the report says so instead of feigning confidence.
EVIDENCE_SAT_CLOSURE    = 3     # direct neighbors
EVIDENCE_SAT_RETENTION  = 3     # ranker-relevant symbols
EVIDENCE_SAT_CONFIDENCE = 3     # outgoing edges from changed symbols
LOW_EVIDENCE_MAX        = 0.4   # below this, emit a low-evidence finding
MIDPOINT                = 50.0  # "don't know" score


@dataclass
class SufficiencyFinding:
    """One concrete, actionable deficiency in the compiled context."""
    severity: str          # "critical" | "warning" | "info"
    kind: str              # machine-readable slug, e.g. "missing-direct-neighbor"
    message: str           # human/LLM-readable, includes remediation
    symbols: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "symbols": self.symbols,
        }


@dataclass
class SufficiencyReport:
    """Structural sufficiency verdict for one compiled context package."""
    score: float                     # 0-100 structural proxy (see module docstring)
    verdict: str                     # SUFFICIENT | DEGRADED | INSUFFICIENT
    direct_closure: float            # fraction of direct neighbors in context
    high_score_retention: float      # fraction of ranker-relevant symbols kept
    local_graph_confidence: float    # resolved fraction of edges out of changed symbols
    parse_health: float              # 1.0 = no broken files
    findings: List[SufficiencyFinding] = field(default_factory=list)
    missing_direct: List[str] = field(default_factory=list)
    dropped_high_score: List[str] = field(default_factory=list)
    calibrated: bool = False         # True only when produced by a calibration run
    evidence: float = 1.0            # [0,1] how much observation backs the score
    score_legacy: float = 0.0        # pre-evidence-shrinkage formula (A/B measure)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "verdict": self.verdict,
            "components": {
                "direct_closure": round(self.direct_closure, 3),
                "high_score_retention": round(self.high_score_retention, 3),
                "local_graph_confidence": round(self.local_graph_confidence, 3),
                "parse_health": round(self.parse_health, 3),
            },
            "missing_direct": self.missing_direct,
            "dropped_high_score": self.dropped_high_score,
            "findings": [f.to_dict() for f in self.findings],
            "calibrated": self.calibrated,
            "evidence": round(self.evidence, 3),
            "score_legacy": round(self.score_legacy, 1),
        }

    def render(self) -> str:
        """Human-readable report block."""
        mark = {"SUFFICIENT": "✓", "DEGRADED": "⚠", "INSUFFICIENT": "✗"}[self.verdict]
        lines = [
            "=== DIFFCONTEXT SUFFICIENCY REPORT ===",
            f"Verdict : {mark} {self.verdict}  (structural score: {self.score:.0f}/100)",
            f"  direct-neighbor closure : {self.direct_closure * 100:.0f}%"
            f"  ({len(self.missing_direct)} missing)",
            f"  high-score retention    : {self.high_score_retention * 100:.0f}%"
            f"  ({len(self.dropped_high_score)} relevant symbols cut by budget)",
            f"  local graph confidence  : {self.local_graph_confidence * 100:.0f}%",
            f"  parse health            : {self.parse_health * 100:.0f}%",
            f"  evidence behind score   : {self.evidence * 100:.0f}%",
        ]
        if self.findings:
            lines.append("")
            lines.append("FINDINGS:")
            for f in self.findings:
                icon = {"critical": "✗", "warning": "⚠", "info": "·"}[f.severity]
                lines.append(f"  {icon} [{f.kind}] {f.message}")
                for s in f.symbols[:8]:
                    lines.append(f"      - {s}")
                if len(f.symbols) > 8:
                    lines.append(f"      ... and {len(f.symbols) - 8} more")
        lines.append("")
        if not self.calibrated:
            lines.append(
                "NOTE: this score is a structural proxy, not a probability. Run\n"
                "`diffcontext verify --from-history 30 --calibrate` to map scores\n"
                "to measured recall on this repo's own history."
            )
        lines.append("=== END SUFFICIENCY REPORT ===")
        return "\n".join(lines)


def analyze_sufficiency(
    index: RepositoryIndex,
    impact: ImpactResult,
    package: ContextPackage,
    high_score_threshold: float = HIGH_SCORE_THRESHOLD,
) -> SufficiencyReport:
    """
    Compute the structural sufficiency of a compiled context package.

    Deterministic and offline: uses only the index, the impact scores, and
    what the selector actually kept — no LLM call.
    """
    changed = list(impact.changed)
    changed_set = set(changed)
    selected_set: Set[str] = {item.symbol_id for item in package.items}
    # package.items is empty when compile ran without a graph; fall back to
    # treating changed as selected so the report degrades gracefully.
    if not selected_set:
        selected_set = set(changed)

    reverse = index.reverse_graph
    findings: List[SufficiencyFinding] = []

    # ── 1. Direct-neighbor closure ────────────────────────────────────────
    direct_neighbors: Set[str] = set()
    for sym in changed:
        for callee in index.graph.get(sym, []):
            if callee in index.symbols and callee not in changed_set:
                direct_neighbors.add(callee)
        for caller in reverse.get(sym, set()):
            if caller in index.symbols and caller not in changed_set:
                direct_neighbors.add(caller)

    if direct_neighbors:
        present = direct_neighbors & selected_set
        direct_closure = len(present) / len(direct_neighbors)
    else:
        direct_closure = 1.0
    missing_direct = sorted(
        direct_neighbors - selected_set,
        key=lambda s: -impact.scores.get(s, 0.0),
    )

    if missing_direct:
        findings.append(SufficiencyFinding(
            severity="critical" if direct_closure < 0.7 else "warning",
            kind="missing-direct-neighbor",
            message=(
                f"{len(missing_direct)} direct caller(s)/callee(s) of the changed "
                f"symbols are NOT in context. An LLM cannot see how these interact "
                f"with the change. Remediation: raise --max-tokens or --top-k, or "
                f"pass them explicitly via --changed."
            ),
            symbols=missing_direct,
        ))

    # ── 2. High-score retention ───────────────────────────────────────────
    relevant = {
        s for s, sc in impact.scores.items()
        if sc >= high_score_threshold and s not in changed_set and s in index.symbols
    }
    if relevant:
        kept = relevant & selected_set
        retention = len(kept) / len(relevant)
    else:
        retention = 1.0
    dropped_high = sorted(
        relevant - selected_set,
        key=lambda s: -impact.scores.get(s, 0.0),
    )

    if dropped_high:
        findings.append(SufficiencyFinding(
            severity="critical" if retention < 0.5 else "warning",
            kind="high-score-dropped",
            message=(
                f"{len(dropped_high)} symbol(s) the ranker scored ≥{high_score_threshold:.0f} "
                f"were cut by the token budget — the ranker itself considers this "
                f"context incomplete. Remediation: raise --max-tokens."
            ),
            symbols=dropped_high,
        ))

    # ── 3. Local graph confidence (edges out of changed symbols) ─────────
    local_total = 0
    local_resolved = 0
    for sym in changed:
        for dep in index.graph.get(sym, []):
            local_total += 1
            if dep in index.symbols:
                local_resolved += 1
    local_confidence = (local_resolved / local_total) if local_total else 1.0

    if local_confidence < 0.7 and local_total >= 3:
        findings.append(SufficiencyFinding(
            severity="warning",
            kind="unresolved-local-edges",
            message=(
                f"Only {local_confidence * 100:.0f}% of calls made by the changed "
                f"symbols resolve to known code (externals, dynamic dispatch, or "
                f"broken files). The graph may be blind to real dependencies here."
            ),
        ))

    # ── 4. Parse health ───────────────────────────────────────────────────
    broken = list(package.skipped_files or index.broken_files)
    parse_health = max(0.0, 1.0 - 0.2 * len(broken))
    if broken:
        findings.append(SufficiencyFinding(
            severity="warning",
            kind="parse-holes",
            message=(
                f"{len(broken)} file(s) failed to parse; the graph has holes there "
                f"and this report cannot see dependencies through them."
            ),
            symbols=broken,
        ))

    # ── Composite score + verdict ─────────────────────────────────────────
    raw = (
        W_CLOSURE * direct_closure
        + W_RETENTION * retention
        + W_CONFIDENCE * local_confidence
        + W_PARSE * parse_health
    )
    score_legacy = 100.0 * raw

    # Evidence-aware shrinkage: a component backed by zero observations must
    # not count as a perfect 1.0. Each component's trust saturates after a
    # few observations; the composite shrinks toward the maximum-uncertainty
    # midpoint in proportion to the missing evidence. With rich evidence
    # (any well-connected Python symbol) this reduces to the legacy formula.
    evidence = (
        W_CLOSURE * min(1.0, len(direct_neighbors) / EVIDENCE_SAT_CLOSURE)
        + W_RETENTION * min(1.0, len(relevant) / EVIDENCE_SAT_RETENTION)
        + W_CONFIDENCE * min(1.0, local_total / EVIDENCE_SAT_CONFIDENCE)
        + W_PARSE * 1.0
    )
    score = evidence * score_legacy + (1.0 - evidence) * MIDPOINT

    if evidence < LOW_EVIDENCE_MAX:
        findings.append(SufficiencyFinding(
            severity="warning",
            kind="low-evidence",
            message=(
                f"Only {evidence * 100:.0f}% of the signals this score is built "
                f"from have any observations behind them (sparse or blind graph "
                f"around the changed symbols). The score is shrunk toward 50 "
                f"('unknown') accordingly — do not read it as confidence."
            ),
        ))

    if score >= VERDICT_SUFFICIENT_MIN:
        verdict = "SUFFICIENT"
    elif score >= VERDICT_DEGRADED_MIN:
        verdict = "DEGRADED"
    else:
        verdict = "INSUFFICIENT"

    return SufficiencyReport(
        score=score,
        verdict=verdict,
        direct_closure=direct_closure,
        high_score_retention=retention,
        local_graph_confidence=local_confidence,
        parse_health=parse_health,
        findings=findings,
        missing_direct=missing_direct,
        dropped_high_score=dropped_high,
        evidence=evidence,
        score_legacy=score_legacy,
    )
