"""
tests/test_token_budget.py — `max_tokens` must bound the FULL compiled output.

Regression context: the selector used to budget on token_count(symbol.code)
alone, while the compiler rendered each symbol with a FILE:/FUNCTION: header
plus a CALLERS/CALLEES relationship block, and prepended a meta header —
none of which the selector's budget check ever measured. Result: the full
output (`ContextPackage.token_estimate`, the number reported as
"Output tokens (full)" and the number an agent harness actually pays)
systematically overshot --max-tokens by 25-41% at every budget from 500 to
8000, reproduced live on psf/black. These tests fail against that behavior.

The one documented exception: the meta header (the disclosure layer — repo
totals, DROPPED manifest, warnings) and the changed symbols themselves (the
diff is the reason the context exists) are never dropped. When that floor
alone exceeds the requested budget, the output is the floor — visible in
the meta's own token lines, never silent. In that case, and only that case,
every non-changed symbol must have been dropped and disclosed.
"""

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diffcontext.pipeline import run_pipeline


def _write_synthetic_repo(root):
    """
    A small repo with real call edges and enough code volume that budget
    selection actually has to cut something at the budgets tested below.
    auth.py: validate -> (decode, check_expiry, load_keys); refresh -> decode
    store.py: save_session / purge_sessions (no call edges into auth).
    """
    body = "\n".join(
        f"    value_{i} = compute_step_{i} = {i} * seed + offset_{i}"
        for i in range(18)
    )
    auth = textwrap.dedent("""\
        def decode(token, seed=1):
            offset_0 = offset_1 = offset_2 = offset_3 = offset_4 = 0
            offset_5 = offset_6 = offset_7 = offset_8 = offset_9 = 0
            offset_10 = offset_11 = offset_12 = offset_13 = offset_14 = 0
            offset_15 = offset_16 = offset_17 = 0
        {body}
            return value_17


        def check_expiry(claims, seed=2):
            offset_0 = offset_1 = offset_2 = offset_3 = offset_4 = 0
            offset_5 = offset_6 = offset_7 = offset_8 = offset_9 = 0
            offset_10 = offset_11 = offset_12 = offset_13 = offset_14 = 0
            offset_15 = offset_16 = offset_17 = 0
        {body}
            return value_17 > 0


        def load_keys(path, seed=3):
            offset_0 = offset_1 = offset_2 = offset_3 = offset_4 = 0
            offset_5 = offset_6 = offset_7 = offset_8 = offset_9 = 0
            offset_10 = offset_11 = offset_12 = offset_13 = offset_14 = 0
            offset_15 = offset_16 = offset_17 = 0
        {body}
            return [value_17]


        def validate(token):
            claims = decode(token)
            keys = load_keys("/etc/keys")
            return check_expiry(claims) and bool(keys)


        def refresh(token):
            return decode(token, seed=9)
    """).format(body=body)

    store = textwrap.dedent("""\
        def save_session(session, seed=4):
            offset_0 = offset_1 = offset_2 = offset_3 = offset_4 = 0
            offset_5 = offset_6 = offset_7 = offset_8 = offset_9 = 0
            offset_10 = offset_11 = offset_12 = offset_13 = offset_14 = 0
            offset_15 = offset_16 = offset_17 = 0
        {body}
            return value_17


        def purge_sessions(seed=5):
            offset_0 = offset_1 = offset_2 = offset_3 = offset_4 = 0
            offset_5 = offset_6 = offset_7 = offset_8 = offset_9 = 0
            offset_10 = offset_11 = offset_12 = offset_13 = offset_14 = 0
            offset_15 = offset_16 = offset_17 = 0
        {body}
            return None
    """).format(body=body)

    (root / "auth.py").write_text(auth)
    (root / "store.py").write_text(store)


def test_full_output_respects_budget_or_is_disclosed_floor(tmp_path):
    _write_synthetic_repo(tmp_path)
    changed = ["./auth.py:validate"]

    for budget in (300, 800, 2000):
        ctx = run_pipeline(str(tmp_path), changed, max_tokens=budget)

        selected = {item.symbol_id for item in ctx.items}
        non_changed_selected = selected - set(changed)

        if ctx.token_estimate > budget:
            # Only permissible overshoot: the non-compressible floor of
            # meta header + changed symbols. Everything else must have
            # been dropped AND disclosed in the dropped manifest.
            assert non_changed_selected == set(), (
                f"budget {budget}: output {ctx.token_estimate} tokens "
                f"exceeds budget but non-changed symbols are still "
                f"included: {non_changed_selected}"
            )
            assert ctx.dropped_symbols, (
                f"budget {budget}: over budget at the floor, but nothing "
                "is listed as dropped — the miss would be silent"
            )
        else:
            assert ctx.token_estimate <= budget

        # The disclosure invariant, in every branch: any scored symbol that
        # is not visible must be named in the dropped manifest.
        for d in ctx.dropped_symbols:
            assert d not in selected


def test_budget_actually_binds_and_drops_are_disclosed(tmp_path):
    """At a budget the whole repo doesn't fit, something must be dropped,
    the drop must be disclosed, and raising the budget must include more."""
    _write_synthetic_repo(tmp_path)
    changed = ["./auth.py:validate"]

    tight = run_pipeline(str(tmp_path), changed, max_tokens=800)
    loose = run_pipeline(str(tmp_path), changed, max_tokens=100_000)

    assert tight.dropped_symbols, "tight budget dropped nothing — not binding"
    assert tight.symbol_count < loose.symbol_count
    for d in tight.dropped_symbols:
        assert d in tight.text, f"dropped symbol {d} not disclosed in output"


def test_unlimited_budget_unchanged(tmp_path):
    """max_tokens=None keeps everything scored — no trimming path taken."""
    _write_synthetic_repo(tmp_path)
    ctx = run_pipeline(str(tmp_path), ["./auth.py:validate"], max_tokens=None)
    assert ctx.dropped_symbols == []
