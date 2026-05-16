# Illiquid Alternative Asset Fund Model

Cash-flow projection engine for a portfolio of illiquid alternative asset funds, based on the Takahashi–Alexander (Yale, 2001) approach to modeling private capital commitments, drawdowns, distributions, and NAV over time.

## What it does

The model reads fund-level assumptions from Excel, projects **monthly** cash flows for each fund from an **as-of date** through each fund’s termination, rolls those up to **calendar-year annual** metrics, and aggregates funds into a **portfolio** view. Results are written to a four-sheet Excel workbook.

Key behaviors:

- **Life-cycle phases** — Each month is labeled *Investment*, *Reinvestment*, or *Harvest* from invest/reinvest end dates.
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
