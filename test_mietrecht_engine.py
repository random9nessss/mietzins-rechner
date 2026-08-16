"""
Tests for mietrecht_engine. Pytest-compatible (plain asserts); also runnable
without pytest:  python3 test_mietrecht_engine.py

Anchor values come from the official Überwälzungssätze table (BWO /
mietrechtspraxis|mp): one 0.25 step below 5% = -2.91% (0.03/1.03);
1.75 -> 1.25 = -5.66% (0.06/1.06); 2.00 -> 1.25 = -8.26%; 3.50 -> 1.25
= -21.26%; increases are additive (1.25 -> 1.75 = +6.00%).
"""

from datetime import date

from mietrecht_engine import (
    ClaimInput,
    CostIncreaseOffset,
    InflationOffset,
    ClaimWindow,
    compute_claim,
    current_reference_rate,
    next_claim_window,
    rate_on,
    reference_rate_change_pct,
)

TOL = 1e-6


def approx(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) <= tol


# --- reference-rate component: anchored to published tables -----------------

def test_single_step_down_matches_published_2_91():
    assert approx(reference_rate_change_pct(1.50, 1.25), -(0.03 / 1.03))  # -2.9126%


def test_two_steps_down_matches_published_5_66():
    val = reference_rate_change_pct(1.75, 1.25)
    assert approx(val, -(0.06 / 1.06))           # -5.6604%
    assert round(val * 100, 2) == -5.66


def test_three_steps_down_matches_published_8_26():
    val = reference_rate_change_pct(2.00, 1.25)
    assert round(val * 100, 2) == -8.26


def test_max_span_down_matches_published_21_26():
    val = reference_rate_change_pct(3.50, 1.25)
    assert round(val * 100, 2) == -21.26


def test_two_steps_up_matches_published_6_00():
    val = reference_rate_change_pct(1.25, 1.75)
    assert round(val * 100, 2) == 6.00


def test_round_trip_is_neutral():
    down = 1 + reference_rate_change_pct(1.75, 1.25)
    up = 1 + reference_rate_change_pct(1.25, 1.75)
    assert approx(down * up, 1.0)


def test_no_change_is_zero():
    assert reference_rate_change_pct(1.25, 1.25) == 0.0


def test_non_quarter_rate_rejected():
    try:
        reference_rate_change_pct(1.30, 1.25)
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-quarter rate")


# --- rate table -------------------------------------------------------------

def test_current_rate_is_1_25():
    assert current_reference_rate() == 1.25


def test_rate_on_lookup():
    assert rate_on(date(2024, 6, 1)) == 1.75
    assert rate_on(date(2025, 10, 1)) == 1.25
    assert rate_on(date(2016, 1, 1)) == 1.75
    assert rate_on(date(2008, 9, 10)) == 3.50


def test_rate_on_before_series_raises():
    try:
        rate_on(date(2008, 9, 9))
    except ValueError:
        return
    raise AssertionError("expected ValueError before series start")


# --- offsets ----------------------------------------------------------------

def test_inflation_passthrough_40_percent():
    off = InflationOffset(lik_at_basis=100.0, lik_current=102.0)
    assert approx(off.pct(), 0.40 * 0.02)


def test_inflation_symmetric_when_index_falls():
    off = InflationOffset(lik_at_basis=102.0, lik_current=100.0)
    assert off.pct() < 0


def test_cost_increase_default_is_zero():
    assert CostIncreaseOffset(years=3.0).pct() == 0.0


# --- full claim: the workshop example --------------------------------------

def test_workshop_worked_example():
    inp = ClaimInput(
        net_rent_monthly=4500.0,
        basis_rate=1.75,
        current_rate=1.25,
        inflation=InflationOffset(lik_at_basis=106.0, lik_current=108.1),
    )
    res = compute_claim(inp)
    # components
    assert round(res.ref_pct * 100, 2) == -5.66
    assert approx(res.inflation_pct, 0.40 * (108.1 / 106.0 - 1.0))
    # net ≈ -4.87% -> new rent CHF 4,281 (rounded to CHF 1)
    assert res.new_net_rent == 4281.0
    assert res.monthly_delta == -219.0
    assert res.annual_delta == -2628.0
    assert res.has_reduction_claim


def test_offsets_can_extinguish_claim():
    inp = ClaimInput(
        net_rent_monthly=3000.0,
        basis_rate=1.50,
        current_rate=1.25,
        inflation=InflationOffset(lik_at_basis=100.0, lik_current=105.0),  # +2.0% offset
        cost_increase=CostIncreaseOffset(pct_per_year=0.005, years=2.0),   # +1.0% offset
    )
    res = compute_claim(inp)
    # -2.91% + 2.0% + 1.0% = +0.09% -> no reduction claim
    assert not res.has_reduction_claim


def test_rounding_to_5_francs():
    inp = ClaimInput(
        net_rent_monthly=4500.0, basis_rate=1.75, current_rate=1.25, round_to=5.0
    )
    res = compute_claim(inp)
    assert res.new_net_rent % 5 == 0


# --- claim window timing ----------------------------------------------------

def test_next_claim_window_quarterly_six_month_notice():
    win = next_claim_window(
        today=date(2026, 8, 14), termination_months=(3, 6, 9, 12), notice_months=6
    )
    assert win == ClaimWindow(
        effective_date=date(2027, 3, 31), latest_send_date=date(2026, 9, 30)
    )
    assert win.days_left(date(2026, 8, 14)) == 47


def test_window_respects_safety_buffer():
    # Five days before the send deadline the window must roll to the next date.
    win = next_claim_window(
        today=date(2026, 9, 28), termination_months=(3, 6, 9, 12),
        notice_months=6, safety_days=5,
    )
    assert win.effective_date == date(2027, 6, 30)
    assert win.latest_send_date == date(2026, 12, 31)


def test_monthly_termination_short_notice():
    win = next_claim_window(
        today=date(2026, 8, 14),
        termination_months=tuple(range(1, 13)),
        notice_months=3,
    )
    assert win.effective_date == date(2026, 11, 30)
    assert win.latest_send_date == date(2026, 8, 31)
    assert win.days_left(date(2026, 8, 14)) == 17


# --- minimal runner ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
