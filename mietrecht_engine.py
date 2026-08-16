"""
mietrecht_engine — deterministic core for Swiss rent-adjustment claims.

Computes the net rent change a tenant can claim (or a landlord can demand)
under the "relative method" used in Swiss tenancy law:

    net_change = reference_rate_component            (Art. 269a lit. b OR, Art. 13 VMWG)
               + inflation_passthrough               (Art. 269a lit. e OR, Art. 16 VMWG)
               + general_cost_increases              (Art. 269a lit. b OR — must be substantiated,
                                                      BGer 4A_252/2023: no unproven flat rates)
               + value_adding_investments            (Art. 269a lit. a OR / Art. 14 VMWG, optional)

All components are expressed as fractions of the current net rent and combined
additively (standard practice of MV/HEV calculators). The reference-rate
component follows the official Überwälzungssätze convention: step percentages
add up in the increase direction, and reductions are the exact inverse
(e.g. basis 1.75% -> current 1.25% = -6/106 = -5.66%).

Scope / honesty notes
---------------------
* Applies to non-indexed, non-staggered, non-turnover leases only. Indexed,
  staggered (Staffelmiete) and turnover-rent contracts are excluded by law.
* Everything legally contested is a parameter with a documented default.
* This encodes methodology, not legal advice. Individual cases need review
  before a formal demand (Herabsetzungsbegehren) is sent.

Stdlib only. Python >= 3.10.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Official hypothecary reference rate (hypothekarischer Referenzzinssatz)
# Source: BWO, https://www.bwo.admin.ch/de/referenzzinssatz
# Entries are (effective_date, rate_percent) — every CHANGE since the uniform
# federal rate was introduced on 10.09.2008. The BWO announces on the first
# working day of Mar/Jun/Sep/Dec; the new rate applies from the following day.
#
# Effective days verified against the BWO series as republished by
# mietrechtspraxis|mp (mietrecht.ch, "Hypothekarzins und Überwälzungssätze")
# and the BWO Merkblatt reproduced by third parties; 02.06.2017 additionally
# confirmed by the BWO announcement of 01.06.2017.
# ---------------------------------------------------------------------------
REFERENCE_RATE_SERIES: list[tuple[date, float]] = [
    (date(2008, 9, 10), 3.50),  # introduction of the uniform federal rate
    (date(2009, 6, 3), 3.25),
    (date(2009, 9, 2), 3.00),
    (date(2010, 12, 2), 2.75),
    (date(2011, 12, 2), 2.50),
    (date(2012, 6, 2), 2.25),
    (date(2013, 9, 3), 2.00),
    (date(2015, 6, 2), 1.75),
    (date(2017, 6, 2), 1.50),
    (date(2020, 3, 3), 1.25),   # historic low
    (date(2023, 6, 2), 1.50),   # first increase since introduction
    (date(2023, 12, 2), 1.75),
    (date(2025, 3, 4), 1.50),
    (date(2025, 9, 2), 1.25),
]

# Last BWO announcement reflected in the series / next scheduled announcement.
SERIES_AS_OF: date = date(2026, 6, 1)
NEXT_ANNOUNCEMENT: date = date(2026, 9, 1)


def current_reference_rate() -> float:
    """Latest rate in the embedded series."""
    return REFERENCE_RATE_SERIES[-1][1]


def rate_on(d: date) -> float:
    """Reference rate in force on date ``d`` (statutory fallback when a lease
    does not state its basis rate: the rate at the time of contracting /
    last rent fixing applies)."""
    first = REFERENCE_RATE_SERIES[0][0]
    if d < first:
        raise ValueError(
            f"Reference-rate series starts {first}; extend REFERENCE_RATE_SERIES "
            "from the official BWO table for earlier basis dates."
        )
    rate = REFERENCE_RATE_SERIES[0][1]
    for eff, r in REFERENCE_RATE_SERIES:
        if eff <= d:
            rate = r
        else:
            break
    return rate


# ---------------------------------------------------------------------------
# Component 1: reference-rate change (Art. 13 VMWG)
# ---------------------------------------------------------------------------

def _step_pct(upper_rate: float) -> float:
    """Per-0.25-point step percentage, keyed to the rate level (Art. 13 VMWG):
    3% below 5%, 2.5% between 5% and 6%, 2% above 6%.

    Bracket-edge convention: the step spanning (upper_rate - 0.25, upper_rate]
    is classified by its upper endpoint. Irrelevant in the current rate regime
    (everything < 5%); revisit if rates ever approach 5% again.
    """
    if upper_rate <= 5.0:
        return 0.03
    if upper_rate <= 6.0:
        return 0.025
    return 0.02


def _validate_quarter(rate: float, name: str) -> None:
    if abs(rate * 4 - round(rate * 4)) > 1e-9:
        raise ValueError(f"{name}={rate}: reference rates are quarter-point values.")


def reference_rate_change_pct(basis_rate: float, current_rate: float) -> float:
    """Fractional net-rent change implied by moving basis_rate -> current_rate.

    Official convention (Überwälzungssätze table, Art. 13 VMWG as published
    by BWO / mietrechtspraxis|mp): step percentages are summed ADDITIVELY in
    the increase direction (3% per quarter point below a 5% rate level), and
    a decrease is the exact inverse of the corresponding increase,
    sum / (1 + sum):
        1.50 -> 1.25  =>  -0.029126   (-2.91%  = 0.03/1.03)
        1.75 -> 1.25  =>  -0.056604   (-5.66%  = 0.06/1.06)
        2.00 -> 1.25  =>  -0.082569   (-8.26%  = 0.09/1.09)
        3.50 -> 1.25  =>  -0.212598   (-21.26% = 0.27/1.27)
        1.25 -> 1.75  =>  +0.060000   (+6.00%)
    Negative = tenant reduction claim; positive = landlord increase headroom.
    """
    _validate_quarter(basis_rate, "basis_rate")
    _validate_quarter(current_rate, "current_rate")
    lo, hi = sorted((basis_rate, current_rate))
    increase_sum, r = 0.0, lo
    while r < hi - 1e-9:                    # walk lo -> hi in quarter steps
        upper = round(r + 0.25, 2)
        increase_sum += _step_pct(upper)
        r = upper
    if current_rate > basis_rate:           # rate rose: rent may rise
        return increase_sum
    return -increase_sum / (1.0 + increase_sum)  # rate fell: rent must fall


# ---------------------------------------------------------------------------
# Component 2: inflation pass-through (Teuerung, Art. 16 VMWG)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InflationOffset:
    """40% of the LIK (CPI) change since the last rent fixing.

    Both index values MUST be on the same base (BFS publishes conversion
    factors between base years, currently Dec 2020 = 100). Symmetric: if the
    index fell, this becomes a further reduction in the tenant's favour.
    """
    lik_at_basis: float
    lik_current: float
    passthrough: float = 0.40  # Art. 269a lit. e OR

    def pct(self) -> float:
        if self.lik_at_basis <= 0:
            raise ValueError("lik_at_basis must be positive.")
        return self.passthrough * (self.lik_current / self.lik_at_basis - 1.0)


# ---------------------------------------------------------------------------
# Component 3: general cost increases (allgemeine Kostensteigerungen)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostIncreaseOffset:
    """Operating/maintenance cost increases the landlord may net against the
    claim — ONLY insofar as substantiated (BGer 4A_252/2023 of 24.10.2023:
    unproven flat rates are inadmissible; some conciliation boards previously
    tolerated ~0.25–0.5%/year).

    Default is 0.0 (post-BGer stance). Use pct_per_year=0.005 as a
    conservative "landlord will fight" scenario.
    """
    pct_per_year: float = 0.0
    years: float = 0.0

    def pct(self) -> float:
        return self.pct_per_year * self.years


# ---------------------------------------------------------------------------
# Claim computation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimInput:
    net_rent_monthly: float                     # current NET rent (excl. Nebenkosten)
    basis_rate: float                           # rate underlying current rent
    current_rate: Optional[float] = None        # default: latest embedded rate
    inflation: Optional[InflationOffset] = None
    cost_increase: Optional[CostIncreaseOffset] = None
    value_adding_pct: float = 0.0               # Mehrleistungen, if invoked (fraction)
    round_to: float = 1.0                       # landlords often round to CHF 1 or 5


@dataclass(frozen=True)
class ClaimResult:
    old_net_rent: float
    new_net_rent: float
    ref_pct: float
    inflation_pct: float
    cost_pct: float
    value_adding_pct: float
    net_pct: float

    @property
    def monthly_delta(self) -> float:
        return round(self.new_net_rent - self.old_net_rent, 2)

    @property
    def annual_delta(self) -> float:
        return round(12 * self.monthly_delta, 2)

    @property
    def has_reduction_claim(self) -> bool:
        return self.monthly_delta < 0

    def summary_de(self) -> str:
        lines = [
            f"Nettomietzins aktuell:            CHF {self.old_net_rent:>10,.2f} /Mt.",
            f"  Referenzzinssatz-Komponente:    {self.ref_pct:+8.4%}",
            f"  Teuerung (40%-Überwälzung):     {self.inflation_pct:+8.4%}",
            f"  Allg. Kostensteigerungen:       {self.cost_pct:+8.4%}",
            f"  Mehrleistungen:                 {self.value_adding_pct:+8.4%}",
            f"  Netto-Anpassung:                {self.net_pct:+8.4%}",
            f"Nettomietzins neu:                CHF {self.new_net_rent:>10,.2f} /Mt.",
            f"Differenz:                        CHF {self.monthly_delta:>10,.2f} /Mt. "
            f"({self.annual_delta:+,.2f} /Jahr)",
        ]
        return "\n".join(lines)


def _round_to(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def compute_claim(inp: ClaimInput) -> ClaimResult:
    """Combine all components additively (standard calculator practice) and
    apply once to the current net rent.

    Note: an alternative practice compounds the components multiplicatively;
    at current magnitudes the difference is < 0.1 percentage points. The
    additive convention matches the MV/HEV calculators and is what landlords
    and conciliation boards expect to see.
    """
    if inp.net_rent_monthly <= 0:
        raise ValueError("net_rent_monthly must be positive.")
    current = inp.current_rate if inp.current_rate is not None else current_reference_rate()

    ref = reference_rate_change_pct(inp.basis_rate, current)
    infl = inp.inflation.pct() if inp.inflation else 0.0
    cost = inp.cost_increase.pct() if inp.cost_increase else 0.0
    net = ref + infl + cost + inp.value_adding_pct

    new_rent = _round_to(inp.net_rent_monthly * (1.0 + net), inp.round_to)
    return ClaimResult(
        old_net_rent=round(inp.net_rent_monthly, 2),
        new_net_rent=new_rent,
        ref_pct=ref,
        inflation_pct=infl,
        cost_pct=cost,
        value_adding_pct=inp.value_adding_pct,
        net_pct=net,
    )


# ---------------------------------------------------------------------------
# Timing: when can the reduction take effect, and when must the demand be sent?
# ---------------------------------------------------------------------------

def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(d: date, months: int) -> tuple[int, int]:
    total = d.year * 12 + (d.month - 1) + months
    y, m0 = divmod(total, 12)
    return y, m0 + 1


@dataclass(frozen=True)
class ClaimWindow:
    effective_date: date        # next termination date the claim can target
    latest_send_date: date      # demand must REACH the landlord before the
                                # notice period starts — i.e. on/before this date
    def days_left(self, today: date) -> int:
        return (self.latest_send_date - today).days


def next_claim_window(
    today: date,
    termination_months: Sequence[int] = (3, 6, 9, 12),
    notice_months: int = 6,
    safety_days: int = 5,
) -> ClaimWindow:
    """Earliest termination date for which a reduction demand can still be
    filed (Art. 270a OR: claim takes effect at the next termination date;
    the demand must arrive before the notice period preceding it begins).

    Defaults reflect common commercial leases: quarterly month-end
    termination dates, 6-month notice. Always read the actual lease.
    ``safety_days`` buffers postal delivery — the demand must *arrive*, not
    merely be sent, in time.
    """
    if notice_months < 0:
        raise ValueError("notice_months must be >= 0.")
    months_sorted = sorted(set(termination_months))
    y, m = today.year, 1
    for _ in range(0, 48):  # scan up to 4 years ahead
        for tm in months_sorted:
            eff = _month_end(y, tm) if (y, tm) >= (today.year, today.month) or y > today.year else None
            if eff is None or eff <= today:
                continue
            sy, sm = _add_months(eff, -notice_months)
            latest_send = _month_end(sy, sm)
            if latest_send >= today + timedelta(days=safety_days):
                return ClaimWindow(effective_date=eff, latest_send_date=latest_send)
        y += 1
    raise RuntimeError("No claim window found within 4 years — check inputs.")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Workshop example from the business case. LIK values ILLUSTRATIVE —
    # fetch official Landesindex der Konsumentenpreise values from BFS.
    inp = ClaimInput(
        net_rent_monthly=4500.0,
        basis_rate=1.75,                      # last fixed during the Dec-2023 wave
        inflation=InflationOffset(lik_at_basis=106.0, lik_current=108.1),
        cost_increase=CostIncreaseOffset(pct_per_year=0.0, years=2.5),
    )
    res = compute_claim(inp)
    print(res.summary_de())
    print()
    win = next_claim_window(today=date.today())
    print(f"Nächstmöglicher Wirksamkeitstermin: {win.effective_date}")
    print(f"Begehren muss eintreffen bis:       {win.latest_send_date} "
          f"({win.days_left(date.today())} Tage verbleibend)")
