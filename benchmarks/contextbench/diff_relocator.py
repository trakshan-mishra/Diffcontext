#!/usr/bin/env python3
"""
diff_relocator.py — a malformed-diff fix that was TESTED and REJECTED.

Hypothesis (Phase 1): the dominant apply error is that the model emits correct
diff CONTENT but WRONG hunk line numbers, so searching the whole target file
for the hunk's leading context and rewriting the `@@` header would recover them.

Result (validate_relocator.py, offline on 18 captured GLM 5.2 apply-error diffs,
two independent samples: 12 systematic + 6 sporadic): **0/18 recovered (0%)**.

Root cause (confirmed by direct file comparison): the model's diffs are NOT
"correct content + wrong line numbers". The leading context lines are partly
HALLUCINATED — they do not exist in the target file (e.g. a fabricated comment
line, a fabricated `kwargs['widget'] = forms.RadioSelect(` line). Where the
leading context does match, the model's deleted/changed lines do not align with
the real code at that location (the change is placed in the wrong structural
neighborhood). So there is nothing to relocate *to*.

Conclusion: apply-side patch normalization has a ~0% ceiling on this dataset.
The apply errors are a symptom of the model's incorrect mental model of the
code — a CONTEXT problem, not a patch-format problem. The lever is context
quality / localization (Phase 2 + Phase 3), not normalization. Supporting
evidence: in the sporadic re-run, context cut apply errors from 5 (none) ->
1 (diffcontext) -> 0 (diffcontext_gap).

This module + validate_relocator.py are retained as a documented negative
result. The apply_patch() cascade hardening (recount / ignore-whitespace / fuzz
/ trailing-newline) in run_glm_pass1.py IS retained — it recovers the separate
hunk-line-COUNT-mismatch cases (8 via --recount, 4 via fuzz in the systematic
re-run), which is a real, smaller win.
"""

import os
import re
from typing import List, Optional, Tuple


def _parse_diff(diff_text: str) -> List[dict]:
    """Parse a unified diff into a list of file sections, each with hunks.

    Returns [{"path": "rel/path", "head": [raw header lines], "hunks": [
        {"header": "@@ ... @@", "raw_body": [lines], "ctx_lines": [str],
         "old_count": int, "new_count": int} ]}].
    """
    lines = diff_text.splitlines()
    files: List[dict] = []
    cur_file: Optional[dict] = None
    cur_hunk: Optional[dict] = None
    i = 0
    # A file section begins at a `--- a/...` line (optionally preceded by
    # `diff --git`/`index` lines). A hunk begins at `@@`.
    pending_header: List[str] = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            # collect the file header: preceding diff --git/index lines + ---/+++
            head = list(pending_header)
            head.append(line)
            m = re.match(r"--- (?:a/)?(.+?)(?:\s|$)", line)
            path = m.group(1) if m else ""
            i += 1
            if i < len(lines) and lines[i].startswith("+++ "):
                head.append(lines[i])
                # prefer +++ path if --- path is /dev/null
                m2 = re.match(r"\+\+\+ (?:b/)?(.+?)(?:\s|$)", lines[i])
                if m2 and path in ("", "/dev/null"):
                    path = m2.group(1)
                i += 1
            cur_file = {"path": path, "head": head, "hunks": []}
            files.append(cur_file)
            cur_hunk = None
            pending_header = []
            continue
        if line.startswith("@@"):
            if cur_file is None:
                # hunk with no file header — skip (can't locate without a path)
                i += 1
                continue
            # parse header
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if not m:
                cur_hunk = None
                i += 1
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            cur_hunk = {"header": line, "old_start": old_start, "old_count": old_count,
                        "new_start": new_start, "new_count": new_count,
                        "section": m.group(5) or "",
                        "body": []}
            cur_file["hunks"].append(cur_hunk)
            i += 1
            continue
        if line.startswith("diff --git") or line.startswith("Index ") or \
           line.startswith("index "):
            # pending header for the NEXT file section
            pending_header.append(line)
            i += 1
            continue
        if cur_hunk is not None:
            cur_hunk["body"].append(line)
        else:
            # leading non-diff text (e.g. stray prose) — ignore
            pass
        i += 1
    # Compute ctx lines + real counts per hunk
    for f in files:
        for h in f["hunks"]:
            ctx, old_n, new_n = [], 0, 0
            lead = []          # context lines BEFORE the first +/- line
            in_lead = True
            for bl in h["body"]:
                if bl.startswith("\\"):
                    continue  # "\ No newline at end of file"
                if bl.startswith(" "):
                    ctx.append(bl[1:])
                    old_n += 1
                    new_n += 1
                    if in_lead:
                        lead.append(bl[1:])
                elif bl.startswith("-"):
                    old_n += 1
                    in_lead = False
                elif bl.startswith("+"):
                    new_n += 1
                    in_lead = False
                elif bl == "":
                    # bare empty line inside a hunk is treated as context
                    ctx.append("")
                    old_n += 1
                    new_n += 1
                    if in_lead:
                        lead.append("")
            h["ctx_lines"] = ctx
            # Fingerprint for relocation: leading context (contiguous in the
            # original file, since it precedes any change). If the hunk starts
            # with a change (no leading context), fall back to the first run of
            # context lines anywhere in the body (the trailing context).
            h["lead_ctx"] = lead if lead else ctx
            h["real_old_count"] = old_n
            h["real_new_count"] = new_n
    return files


def _read_file_lines(workdir: str, rel_path: str) -> Optional[List[str]]:
    p = os.path.join(workdir, rel_path)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _find_context(file_lines: List[str], ctx: List[str], near: int) -> Optional[int]:
    """Find the 1-indexed line where `ctx` appears as a consecutive window.
    If multiple matches, prefer the one closest to `near`. Returns None if no
    full-context match; if ctx is empty, returns None (can't relocate)."""
    if not ctx:
        return None
    n = len(file_lines)
    k = len(ctx)
    matches = []
    for start in range(n - k + 1):
        ok = True
        for j in range(k):
            if file_lines[start + j] != ctx[j]:
                ok = False
                break
        if ok:
            matches.append(start + 1)  # 1-indexed
    if not matches:
        # retry with a shorter fingerprint (first 2, then 1 line) — only if the
        # longer one failed, to avoid spurious single-line matches
        for kk in (2, 1):
            if k <= kk:
                continue
            ctx2 = ctx[:kk]
            for start in range(n - len(ctx2) + 1):
                if all(file_lines[start + j] == ctx2[j] for j in range(len(ctx2))):
                    matches.append(start + 1)
            if matches:
                break
        if not matches:
            return None
    # prefer closest to `near`
    best = min(matches, key=lambda m: abs(m - near))
    return best


def relocate_hunks(diff_text: str, workdir: str) -> Optional[str]:
    """Return a normalized diff with corrected hunk headers, or None if no hunk
    could be relocated. Diff content (body lines) is NEVER modified — only the
    `@@` headers are rewritten."""
    files = _parse_diff(diff_text)
    if not files:
        return None
    any_relocated = False
    out_parts: List[str] = []
    for f in files:
        out_parts.extend(f["head"])
        file_lines = _read_file_lines(workdir, f["path"])
        cum_net = 0  # net additions from prior hunks in this file
        for h in f["hunks"]:
            relocated = False
            if file_lines is not None and h["lead_ctx"]:
                near = h["old_start"] if h["old_start"] > 0 else 1
                loc = _find_context(file_lines, h["lead_ctx"], near)
                if loc is not None:
                    new_old_start = loc
                    new_new_start = loc + cum_net
                    oc = h["real_old_count"]
                    nc = h["real_new_count"]
                    # git apply rejects count=0 written as bare number; mirror
                    # the ",N" form only when N != 1, per unified-diff spec.
                    old_part = f"-{new_old_start}" if oc == 1 else f"-{new_old_start},{oc}"
                    new_part = f"+{new_new_start}" if nc == 1 else f"+{new_new_start},{nc}"
                    h["header"] = f"@@ {old_part} {new_part} @@{h['section']}"
                    cum_net += (nc - oc)
                    relocated = True
                    any_relocated = True
            else:
                # keep original counts for the cum_net offset
                cum_net += (h["real_new_count"] - h["real_old_count"])
            out_parts.append(h["header"])
            out_parts.extend(h["body"])
            if relocated:
                pass
    if not any_relocated:
        return None
    return "\n".join(out_parts) + ("\n" if diff_text.endswith("\n") else "")
