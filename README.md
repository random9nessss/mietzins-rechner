# mietrecht-engine

Deterministic core for Swiss rent-adjustment claims (Mietzinssenkung /
Mietzinserhöhung nach der relativen Methode). Pure Python, stdlib only,
fully unit-tested against the published conversion values.

This is the calculation layer of a lease-cost-recovery product for SMB
tenants. Extraction (reading leases and adjustment notices), letter
generation, and the Nebenkosten audit are separate layers on top.

## What it computes

```
net_change = Δ reference rate      (Art. 269a lit. b OR, Art. 13 VMWG)
           + Δ inflation × 40%     (Art. 269a lit. e OR, Art. 16 VMWG)
           + general cost increases (substantiated only — BGer 4A_252/2023)
           + value-adding investments (optional)
new_net_rent = old_net_rent × (1 + net_change)
```

plus the **claim window**: the next termination date the claim can target and
the date by which the demand must *reach* the landlord (Art. 270a OR).

## Anchor values (encoded in tests)

| Basis → current | Net-rent change |
|---|---|
| 1.50% → 1.25% | −2.91% |
| 1.75% → 1.25% | −5.66% |
| 2.00% → 1.25% | −8.26% |
| 3.50% → 1.25% | −21.26% |
| 1.25% → 1.75% | +6.00% |

Official Überwälzungssätze convention: per-step percentages (3% below a 5%
rate level, 2.5% between 5–6%, 2% above) are summed **additively** in the
increase direction; a reduction is the exact inverse, `sum / (1 + sum)`.
Matches the BWO / mietrechtspraxis|mp conversion table row for row.

## Deliberate design decisions

1. **Everything legally contested is a parameter, not a constant.**
   - `CostIncreaseOffset` defaults to **0.0**: since BGer 4A_252/2023,
     flat-rate cost surcharges without proof are inadmissible. Use
     `pct_per_year=0.005` to model the conservative pre-ruling practice as a
     "landlord fights back" scenario. The gap between the two scenarios is
     the audit argument you present to the client.
   - Inflation pass-through fixed at 40% by law but exposed for
     what-if analysis.
2. **Additive combination** of components (MV/HEV calculator convention);
   the multiplicative alternative differs by <0.1 pp at current magnitudes —
   documented in `compute_claim`.
3. **Reference-rate table**: complete verified series 10.09.2008 → today
   (BWO via mietrechtspraxis|mp). `rate_on()` deliberately raises for dates
   before 10.09.2008 (pre-federal-rate era: cantonal mortgage rates applied)
   instead of guessing.
4. **`safety_days` in the claim window**: the demand must *arrive* before the
   notice period starts; the buffer models postal delivery. Registered mail
   is assumed for the eventual letter layer.

## Scope limits (by law, not by laziness)

- Excluded lease types: indexed (indexierte Miete), staggered
  (Staffelmiete), turnover rent (Umsatzmiete). Detecting these is the
  extraction layer's first classification task.
- The claim takes effect only at the next termination date; fixed-term
  leases without termination options must wait for expiry/renewal.
- The landlord may respond within 30 days; escalation to the conciliation
  board (Schlichtungsbehörde) has its own deadlines — state machine planned,
  not yet in this module.

## Data sources to wire up

- Reference rate: BWO, https://www.bwo.admin.ch/de/referenzzinssatz
  (quarterly: announced first working day of Mar/Jun/Sep/Dec).
- CPI (LIK): BFS Landesindex der Konsumentenpreise. **Both index values must
  share the same base year** (currently Dec 2020 = 100); BFS publishes
  conversion factors. Demo values in `__main__` are illustrative only.

## Usage

```python
from datetime import date
from mietrecht_engine import (
    ClaimInput, InflationOffset, CostIncreaseOffset,
    compute_claim, next_claim_window,
)

res = compute_claim(ClaimInput(
    net_rent_monthly=4500.0,
    basis_rate=1.75,                      # from lease / last adjustment notice
    inflation=InflationOffset(lik_at_basis=106.0, lik_current=108.1),
))
print(res.summary_de())

win = next_claim_window(today=date.today(), notice_months=6)
print(win.effective_date, win.latest_send_date)
```

Run tests: `python3 test_mietrecht_engine.py` (or `pytest`).

## Roadmap

1. Extraction layer: basis rate + basis date + lease-type classification from
   lease PDFs and adjustment notices (the actual moat).
2. Letter generator: Herabsetzungsbegehren with the component table as
   annex, registered-mail workflow.
3. Nebenkosten audit module: line items vs. lease clauses (the evergreen
   revenue engine).
4. Deadline state machine: 30-day landlord response, conciliation-board
   windows, portfolio-wide monitoring.
5. Rate-direction neutrality: same engine already computes increase headroom
   — the 2027 "defense" product if rates rise.

## Disclaimer

Encodes standard published methodology; it is not legal advice. Individual
demands should be reviewed (lawyer partnership planned for escalations)
before sending.
