# Illiquid Alternative Asset Fund Model

Cash-flow projection engine for a portfolio of illiquid alternative asset funds, based on the Takahashi–Alexander (Yale, 2001) approach to modeling private capital commitments, drawdowns, distributions, and NAV over time.

## What it does

The model reads fund-level assumptions from Excel, projects **monthly** cash flows for each fund from an **as-of date** through each fund’s termination, rolls those up to **calendar-year annual** metrics, and aggregates funds into a **portfolio** view. Results are written to a four-sheet Excel workbook.

Key behaviors:

- **Life-cycle phases** — Each month is labeled *Invest*, *Reinvest*, or *Harvest* from invest/reinvest end dates.
- **Capital calls** — Remaining effective unfunded is drawn linearly through the investment end date.
- **Harvest** — Return of capital, gain on sale, and final liquidation follow a schedule from reinvestment end through termination (including immediate liquidation when termination is on or before the as-of date).
- **Income & fees** — Asset return, management fee (on commitment or invested capital), carry above a hurdle, and optional distributions.
- **Multi-currency** — Funds may be USD or GBP; portfolio reporting converts GBP to USD via spot FX (yfinance) as of the projection date.
- **Annual roll-up** — Mirrors `ModelTemplate.xlsx` (cells I17:Q37): period 0 is a reference snapshot only; flows sum operating months; year-end snapshots use the last operating month in each calendar year (including mid-year terminations).

## Project layout

| Path | Purpose |
|------|---------|
| `fund_engine.py` | Core engine: `Fund`, `Portfolio`, validation, FX, annual aggregation, Excel export |
| `inputs/FundAssumptions.xlsx` | Fund assumptions (one row per fund) |
| `outputs/CashFlowOutputs.xlsx` | Generated output (four tabs) |
| `ModelTemplate.xlsx` | Reference workbook for regression tests |
| `tests/` | `pytest` suite (golden checks vs ModelTemplate and assumptions file) |

## Inputs

**`inputs/FundAssumptions.xlsx`** — one row per fund, including:

- Commitment, funded amount, stated NAV, unfunded (optional)
- Investment / reinvestment / termination dates
- Return, distribution target, % drawn, management fee, carry, currency
- Optional metadata: volatility, asset class, sub-asset class, geography

Rows are validated on load (date ordering, rate bounds, currency, funded vs commitment, etc.).

Default **as-of date**: `2025-12-31` (`DEFAULT_AS_OF_DATE` in `fund_engine.py`).

## Outputs

Running the engine produces **`outputs/CashFlowOutputs.xlsx`** with four sheets:

| Sheet | Contents |
|-------|----------|
| **Funds (Monthly)** | Per-fund monthly cash flows in **native currency**, plus metadata and `period_type` |
| **Funds (Annual)** | Calendar-year roll-up per fund (with `operating_months`, returns, flows) |
| **Portfolio (Monthly)** | Sum of all funds in **USD** by month |
| **Portfolio (Annual)** | USD annual totals plus tagged slices (`TOTAL`, and by `period_type`, volatility, asset class, sub-asset class, geography) |

## How projection works

1. **Period 0 (as-of)** — Snapshot of stated NAV and commitment balances; most flow fields are empty (`NaN`).
2. **Operating months** — For each subsequent month-end through `end_date` (or termination): capital calls, ROC/gain on sale, income, fees, carry, dividend, NAV = ending capital + unrealized G/L.
3. **Edge cases** — Already-terminated zero-NAV funds get a single as-of row; funds terminating on/before as-of get period 0 plus one liquidation month.

Portfolio aggregation converts each fund’s monthly table to USD, then sums numeric columns by `(period, start_date, end_date)`.

Annual aggregation groups operating months (`period > 0`) into calendar years, takes commitment/NAV snapshots from the last month in each year, and computes `period_return` and `dividend_return` from average NAV.

## Monthly output fields (`NUMERIC_OUTPUT_COLUMNS`)

These columns appear on each fund’s monthly rows (Funds Monthly tab). Unless noted, formulas apply to **operating months** (`period > 0`). **Period 0** is an as-of snapshot; **liquidation** is a one-month wind-down when termination is on or before the as-of date.

| Field | How it is calculated |
|-------|----------------------|
| **total_commitment** | Total commitment while the fund is “active” (from initial commitment month through termination); otherwise `0`. |
| **effective_commitment** | `total_commitment × % of capital drawn` (`pct_drawn`). |
| **remaining_effective_unfunded** | During the investment phase (`period_end ≤ investment end date`): `max(0, effective_commitment − ending_capital)`. After investment ends: `0`. Feeds the next month’s capital-call calculation. |
| **legal_unfunded** | Before reinvestment ends (`period_end < reinvestment end date`): `max(0, total_commitment − ending_capital)`. After reinvestment ends: `0`. |
| **beginning_capital_account** | Prior month’s `ending_capital`. Period 0: not applicable (`NaN`). |
| **capital_called** | Positive drawdown during the investment phase only. Remaining effective unfunded at the start of the month is spread evenly over the months from `period_start` through `investment end date`. No calls after investment ends, if reinvestment already ended before projection start, or if funded amount already exceeds effective commitment. |
| **roc** (return of capital) | Negative cash return of invested capital during **harvest** (`period_end > reinvestment end date`). The base amount is `beginning_capital_account + capital_called`, returned on the harvest schedule (evenly by month until termination, or fully in the termination month). |
| **ending_capital** | `beginning_capital_account + capital_called + roc`. Period 0: `funded_amount` if still in investment phase; otherwise `min(stated NAV, total commitment)`. Liquidation month: `0`. |
| **beginning_unrealized_gl** | Opening unrealized gain/loss. Period 1: `max(stated NAV − funded amount, 0)`. Later months: prior `ending_unrealized_gl`. Period 0: `NaN`. |
| **asset_income** | `max(prior month NAV × monthly_return, 0)` while `period_end ≤ termination date`; otherwise `0`. `monthly_return = (1 + annual_return)^(1/12) − 1`. Period 0: `NaN`. |
| **mgmt_fee_amt** | Negative expense. If **paid on committed** and still in investment phase: `−effective_commitment × (mgmt fee / 12)`. Otherwise, if prior `ending_capital > 0`: `−ending_capital × (mgmt fee / 12)`. Period 0: `NaN`. |
| **pre_carry_income** | `asset_income + mgmt_fee_amt`. Period 0: `NaN`. |
| **carry_amt** | Negative carried interest when annualized pre-carry return exceeds the hurdle: if `(pre_carry_income / prior ending_capital) × 12 > carry hurdle`, then `−pre_carry_income × carry rate`; otherwise `0`. Period 0: `NaN`. |
| **dividend** | Cash distribution (negative = paid to investors). Zero if distribution target is `0` or `period_end ≥ termination date`. Otherwise target is `prior NAV × (annual distribution target / 12)`, capped so the distribution does not exceed available net income after carry. Period 0: `NaN`. |
| **retained_income** | `pre_carry_income + carry_amt + dividend`. Drives unrealized G/L for the month. Period 0: `NaN`. |
| **period_gl** | Same as `retained_income` (period change in unrealized G/L before realization). Period 0: `NaN`. |
| **gain_on_sale** | Realization of unrealized G/L. If termination falls in this month: `−(beginning_unrealized_gl + period_gl)`. If unrealized base is negative: `0`. Otherwise, same harvest schedule as ROC on `beginning_unrealized_gl + period_gl`. Liquidation month: `max(0, prior NAV − funded amount)`. Period 0: `NaN`. |
| **ending_unrealized_gl** | `beginning_unrealized_gl + period_gl + gain_on_sale`. Period 0: `0`. Liquidation: `0`. |
| **nav** | `ending_capital + ending_unrealized_gl`. Period 0: **stated NAV** from assumptions. Liquidation: `0`. |
| **net_cf** | Net cash flow **to the investor** (positive = cash in). Operating months: `−(capital_called + roc + gain_on_sale + dividend)`. Period 0: `−stated NAV`. Liquidation: `roc + gain_on_sale`. |

**Harvest schedule (shared by ROC and gain on sale):** After reinvestment ends, outflows are spread evenly across months from the current `period_end` through `termination date`, except the termination month, which returns the full remaining balance.

**Initial unfunded for calls:** At load time, remaining effective unfunded is `max(0, effective commitment − funded amount)`, or `max(0, stated unfunded)` if provided. If funded amount already exceeds effective commitment, no further calls are modeled.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python.exe fund_engine.py
```

This loads `inputs/FundAssumptions.xlsx` and writes `outputs/CashFlowOutputs.xlsx`.

Programmatic use:

```python
from fund_engine import Fund, Portfolio, load_funds_from_assumptions, export_cash_flow_outputs

export_cash_flow_outputs()  # full pipeline

funds = load_funds_from_assumptions("inputs/FundAssumptions.xlsx")
df = funds[0].project("2025-12-31", "2030-06-30")
portfolio = Portfolio(funds).aggregate("2025-12-31")
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests compare monthly and annual output against `ModelTemplate.xlsx`, exercise validation and edge cases, and cover GBP→USD conversion (mocked where needed).

## Reference

Takahashi, D. E., and S. E. Alexander. *Illiquid Alternative Asset Fund Modeling*. Yale Investments Office, 2001.
