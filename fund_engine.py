"""
Fund cash-flow projection engine for illiquid alternative asset funds.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta

DEFAULT_AS_OF_DATE = date(2025, 12, 31)
SUPPORTED_CURRENCIES = frozenset({"USD", "GBP"})
PORTFOLIO_REPORTING_CURRENCY = "USD"
_NAN = float("nan")

_FUND_ASSUMPTIONS_COLUMNS: dict[str, str] = {
    "Investment Name": "name",
    "Total Commitment": "commitment",
    "Stated Unfunded": "unfunded",
    "Funded Amount": "funded_amount",
    "Investment End Date": "invest_end_date",
    "Reinvestment End Date": "reinvest_end_date",
    "Termination Date": "termination_date",
    "Stated NAV": "stated_nav",
    "Ann. Asset Class Return (Conservative)": "annual_return",
    "Annual Fund Distribution Target": "dist_rate",
    "% of Capital Drawn": "pct_drawn",
    "Mgmt Fee": "mgmt_fee",
    "Paid on Committed?": "paid_on_committed",
    "Carry": "carry_rate",
    "Carry Hurdle": "carry_hurdle",
    "Currency": "currency",
    "Volatility": "volatility",
    "Asset Class": "asset_class",
    "Sub-Asset Class": "sub_asset_class",
    "Geography": "geography",
}

_FUND_METADATA_COLUMNS = (
    "volatility",
    "asset_class",
    "sub_asset_class",
    "geography",
)

_STRING_ASSUMPTION_FIELDS = frozenset(_FUND_METADATA_COLUMNS)

_REQUIRED_FUND_ASSUMPTIONS_COLUMNS = {
    excel_col: field
    for excel_col, field in _FUND_ASSUMPTIONS_COLUMNS.items()
    if field not in _STRING_ASSUMPTION_FIELDS
}

_RATE_FIELDS = (
    "annual_return",
    "dist_rate",
    "mgmt_fee",
    "carry_rate",
    "carry_hurdle",
)

_DATE_FIELDS = frozenset({"invest_end_date", "reinvest_end_date", "termination_date"})

NUMERIC_OUTPUT_COLUMNS = [
    "total_commitment",
    "effective_commitment",
    "remaining_effective_unfunded",
    "legal_unfunded",
    "beginning_capital_account",
    "capital_called",
    "roc",
    "ending_capital",
    "beginning_unrealized_gl",
    "period_gl",
    "gain_on_sale",
    "ending_unrealized_gl",
    "nav",
    "asset_income",
    "mgmt_fee_amt",
    "pre_carry_income",
    "carry_amt",
    "dividend",
    "retained_income",
    "net_cf",
]

ANNUAL_OUTPUT_COLUMNS = [
    "total_commitment",
    "effective_commitment",
    "remaining_effective_unfunded",
    "legal_unfunded",
    "beginning_nav",
    "capital_called",
    "distributions",
    "net_income",
    "ending_nav",
    "roc",
    "dividend",
    "gain_on_sale",
    "average_nav",
    "period_return",
    "dividend_return",
]

_ANNUAL_SNAPSHOT_COLUMNS = (
    "total_commitment",
    "effective_commitment",
    "remaining_effective_unfunded",
    "legal_unfunded",
)

# --- Date and table helpers ---


def month_end(value: date | datetime) -> date:
    """Return the last calendar day of the month containing *value*."""
    if isinstance(value, datetime):
        value = value.date()
    next_month = value.replace(day=1) + relativedelta(months=1)
    return next_month - relativedelta(days=1)


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in NUMERIC_OUTPUT_COLUMNS if c in df.columns]


def _pick_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return df[[c for c in columns if c in df.columns]]


def _convert_fund_to_usd(df: pd.DataFrame, fund: Fund, as_of: date) -> pd.DataFrame:
    """Scale numeric columns by spot FX so portfolio math is in USD."""
    if fund.currency == "USD":
        return df
    out = df.copy()
    rate = fx_rate_to_usd(fund.currency, as_of)
    out[_numeric_columns(out)] = out[_numeric_columns(out)] * rate
    return out


# --- Annual roll-up (monthly → calendar year) ---


def _first_calendar_year_end(as_of: date) -> date:
    """First reporting year-end after the as-of date (matches ModelTemplate)."""
    year = as_of.year + (1 if as_of.month == 12 else 0)
    return date(year, 12, 31)


def _calendar_year_ends(
    as_of: date,
    operating_end_dates: pd.Series,
) -> list[date]:
    """Dec-31 labels for each calendar year that has operating months."""
    if operating_end_dates.empty:
        return []
    dates = pd.to_datetime(operating_end_dates).dt.date
    activity_years = {d.year for d in dates}
    first_template_year = _first_calendar_year_end(as_of).year
    start_year = min(first_template_year, min(activity_years))
    end_year = max(dates).year
    return [date(y, 12, 31) for y in range(start_year, end_year + 1)]


def _split_reference_and_operating(
    monthly_df: pd.DataFrame,
) -> tuple[pd.Series | None, pd.DataFrame]:
    """Period 0 is the as-of reference row; operating months are period > 0."""
    if "period" not in monthly_df.columns:
        return None, monthly_df.sort_values("end_date").reset_index(drop=True)

    reference = monthly_df.loc[monthly_df["period"] == 0]
    operating = monthly_df.loc[monthly_df["period"] != 0].sort_values("end_date")
    ref_row = reference.iloc[-1] if not reference.empty else None
    return ref_row, operating.reset_index(drop=True)


def _year_snapshot(operating: pd.DataFrame, year_end: date) -> pd.Series | None:
    """Last operating month in the calendar year (Dec 31 when present)."""
    end_dates = pd.to_datetime(operating["end_date"]).dt.date
    year_start = date(year_end.year, 1, 1)
    in_year = operating.loc[(end_dates >= year_start) & (end_dates <= year_end)]
    if in_year.empty:
        return None
    return in_year.iloc[-1]


def _annual_return_ratio(numerator: float, average_nav: float) -> float:
    """Return ratio helper; 0 when average NAV is zero (ModelTemplate I38:I39)."""
    if average_nav == 0.0:
        return 0.0
    return numerator / average_nav


def aggregate_monthly_to_annual(
    monthly_df: pd.DataFrame,
    *,
    as_of_date: date | datetime,
    group_keys: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Roll monthly rows up to calendar years (period 0 is the as-of snapshot only)."""
    if monthly_df.empty:
        return pd.DataFrame()

    as_of = month_end(as_of_date)

    annual_rows: list[dict[str, Any]] = []
    grouped = (
        monthly_df.groupby(list(group_keys), sort=False)
        if group_keys
        else [((), monthly_df)]
    )

    for group_key, group_df in grouped:
        ref_row, operating = _split_reference_and_operating(group_df)
        if operating.empty:
            continue

        op_end_dates = pd.to_datetime(operating["end_date"]).dt.date
        year_ends = _calendar_year_ends(as_of, operating["end_date"])

        if group_keys:
            keys = group_key if isinstance(group_key, tuple) else (group_key,)
            group_identity = dict(zip(group_keys, keys))
        else:
            group_identity = {}

        ref_end = (
            pd.to_datetime(ref_row["end_date"]).date()
            if ref_row is not None
            else None
        )
        prev_year_end: date | None = None
        prev_ending_nav: float | None = None

        for year_end in year_ends:
            snap = _year_snapshot(operating, year_end)
            if snap is None:
                continue

            if prev_year_end is None:
                if ref_end is not None:
                    window_mask = (op_end_dates > ref_end) & (op_end_dates <= year_end)
                    beginning_nav = float(ref_row["nav"])
                else:
                    window_mask = op_end_dates <= year_end
                    beginning_nav = float(operating.iloc[0]["nav"])
            else:
                window_mask = (op_end_dates > prev_year_end) & (op_end_dates <= year_end)
                beginning_nav = float(prev_ending_nav)

            window = operating.loc[window_mask]
            operating_months = len(window)

            def _flow(col: str) -> float:
                return float(window[col].sum()) if not window.empty else 0.0

            dividend = _flow("dividend")
            roc = _flow("roc")
            gain_on_sale = _flow("gain_on_sale")
            ending_nav = float(snap["nav"])
            net_income = _flow("period_gl") - dividend
            average_nav = (beginning_nav + ending_nav) / 2.0

            row: dict[str, Any] = {
                **group_identity,
                "year": year_end.year,
                "operating_months": operating_months,
                "beginning_nav": beginning_nav,
                "capital_called": _flow("capital_called"),
                "roc": roc,
                "dividend": dividend,
                "gain_on_sale": gain_on_sale,
                "distributions": roc + dividend + gain_on_sale,
                "net_income": net_income,
                "ending_nav": ending_nav,
                "average_nav": average_nav,
                "period_return": _annual_return_ratio(net_income, average_nav),
                "dividend_return": _annual_return_ratio(abs(dividend), average_nav),
            }
            for col in _ANNUAL_SNAPSHOT_COLUMNS:
                row[col] = float(snap[col])
            annual_rows.append(row)
            prev_year_end = year_end
            prev_ending_nav = ending_nav

    if not annual_rows:
        return pd.DataFrame()

    result = pd.DataFrame(annual_rows)
    leading = [*group_keys, "year", "operating_months"]
    trailing = [c for c in ANNUAL_OUTPUT_COLUMNS if c in result.columns]
    return result[leading + trailing]


def _to_date(value: date | datetime | pd.Timestamp | None) -> date | None:
    """Coerce Excel/pandas date cells to plain date (or None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    return value


def _normalize_assumptions_columns(columns: pd.Index) -> pd.Index:
    """Flatten wrapped Excel headers so column names match our lookup keys."""
    return pd.Index([str(c).replace("\n", " ").strip() for c in columns])


def _coerce_paid_on_committed_strict(value: object, fund_name: str) -> bool:
    """Parse Paid on Committed? from Excel (TRUE/FALSE/1/0 only)."""
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ValueError(
            f"Fund {fund_name!r}: Paid on Committed? must be one of "
            "{{TRUE, FALSE, 1, 0}}; got empty value."
        )
    normalized = str(value).strip().upper()
    if normalized in {"TRUE", "1"}:
        return True
    if normalized in {"FALSE", "0"}:
        return False
    raise ValueError(
        f"Fund {fund_name!r}: Paid on Committed? must be one of "
        f"{{TRUE, FALSE, 1, 0}}; got {value!r}."
    )


def _parse_assumptions_row(row: pd.Series, as_of: date, path: Path) -> dict[str, Any]:
    """Build Fund constructor kwargs from one FundAssumptions.xlsx row."""
    name = row.get("Investment Name")
    if pd.isna(name) or str(name).strip() == "":
        raise ValueError(f"Missing Investment Name in {path}.")

    fund_name = str(name).strip()
    kwargs: dict[str, Any] = {"initial_commitment_month": as_of, "name": fund_name}

    for excel_col, field_name in _FUND_ASSUMPTIONS_COLUMNS.items():
        if field_name == "name":
            continue
        raw = row.get(excel_col)
        if field_name in _STRING_ASSUMPTION_FIELDS:
            kwargs[field_name] = str(raw).strip() if raw is not None and not pd.isna(raw) else ""
            continue
        if field_name == "currency":
            kwargs[field_name] = str(raw).strip() if not pd.isna(raw) else ""
        elif field_name == "paid_on_committed":
            kwargs[field_name] = _coerce_paid_on_committed_strict(raw, fund_name)
        elif field_name == "unfunded":
            kwargs[field_name] = None if pd.isna(raw) else float(raw)
        elif field_name in _DATE_FIELDS:
            parsed = _to_date(raw)
            if parsed is None:
                raise ValueError(
                    f"Fund {fund_name!r}: missing date for {excel_col!r} in {path}"
                )
            kwargs[field_name] = month_end(parsed)
        else:
            if pd.isna(raw):
                raise ValueError(
                    f"Fund {fund_name!r}: missing value for {excel_col!r} in {path}"
                )
            kwargs[field_name] = float(raw)

    return kwargs


def validate_fund_assumptions_row(
    data: dict[str, Any],
    *,
    validate_initial_commitment: bool = False,
) -> None:
    """Raise ValueError if parsed assumptions violate fund input rules."""
    name = data["name"]
    invest_end, reinvest_end, termination = (
        data["invest_end_date"],
        data["reinvest_end_date"],
        data["termination_date"],
    )

    if validate_initial_commitment:
        initial = month_end(data["initial_commitment_month"])
        if invest_end < initial:
            raise ValueError(
                f"Fund {name!r}: Investment End Date ({invest_end}) must be on or after "
                f"Initial Commitment Month ({initial})."
            )
    if reinvest_end < invest_end:
        raise ValueError(
            f"Fund {name!r}: Reinvestment End Date ({reinvest_end}) must be on or after "
            f"Investment End Date ({invest_end})."
        )
    if termination < reinvest_end:
        raise ValueError(
            f"Fund {name!r}: Termination Date ({termination}) must be on or after "
            f"Reinvestment End Date ({reinvest_end})."
        )

    commitment = float(data["commitment"])
    funded = float(data["funded_amount"])
    stated_unfunded = data.get("unfunded")
    if commitment <= 0:
        raise ValueError(f"Fund {name!r}: Total Commitment must be > 0; got {commitment}.")
    if stated_unfunded is not None and float(stated_unfunded) < 0:
        raise ValueError(f"Fund {name!r}: Stated Unfunded must be >= 0; got {stated_unfunded}.")
    if funded > commitment:
        raise ValueError(
            f"Fund {name!r}: Funded Amount ({funded:,.2f}) cannot exceed "
            f"Total Commitment ({commitment:,.2f})."
        )
    if stated_unfunded is not None and float(stated_unfunded) > commitment:
        raise ValueError(
            f"Fund {name!r}: Stated Unfunded ({float(stated_unfunded):,.2f}) cannot exceed "
            f"Total Commitment ({commitment:,.2f})."
        )

    pct_drawn = float(data["pct_drawn"])
    if not 0.0 <= pct_drawn <= 1.0:
        raise ValueError(
            f"Fund {name!r}: % of Capital Drawn must be between 0 and 1 inclusive; "
            f"got {pct_drawn}."
        )

    for field in _RATE_FIELDS:
        rate = float(data[field])
        if not (-1.0 < rate < 1.0):
            raise ValueError(
                f"Fund {name!r}: {field} must be between -100% and 100% "
                f"(-1.0 < rate < 1.0 as a decimal); got {rate}."
            )

    currency = str(data["currency"]).strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"Fund {name!r}: Currency must be one of {sorted(SUPPORTED_CURRENCIES)}; "
            f"got {data['currency']!r}."
        )
    data["currency"] = currency


def _datedif_months(start: date, end: date) -> int:
    """Approximate Excel DATEDIF(start, end, 'M') for even capital-call pacing."""
    return round((end - start).days / (365 / 12))


def _excel_months_span(period_end: date, terminal_date: date) -> int:
    """Month count from period_end through termination (harvest liquidation schedule)."""
    return (
        1
        + (terminal_date.month - period_end.month)
        + (terminal_date.year - period_end.year) * 12
    )


def _advance_period_end(previous_end: date) -> tuple[date, date]:
    """Return (start, end) for the next monthly projection period."""
    month_anchor = previous_end.replace(day=1)
    period_start = month_anchor + relativedelta(months=1)
    return period_start, month_end(period_start)


@lru_cache(maxsize=32)
def fx_rate_to_usd(currency: str, as_of: date) -> float:
    """Spot FX to USD on as_of (yfinance); 1.0 for USD funds."""
    code = str(currency).strip().upper()
    if code == "USD":
        return 1.0
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"Unsupported currency {currency!r}; supported: {sorted(SUPPORTED_CURRENCIES)}."
        )

    as_of_me = month_end(as_of)
    symbol = f"{code}USD=X"
    try:
        history = yf.Ticker(symbol).history(
            start=(as_of_me - timedelta(days=10)).isoformat(),
            end=(as_of_me + timedelta(days=1)).isoformat(),
            auto_adjust=True,
        )
    except Exception as exc:
        raise ValueError(f"FX lookup failed for {code}/USD on {as_of_me}: {exc}") from exc

    if history.empty or "Close" not in history.columns:
        raise ValueError(f"FX lookup failed for {code}/USD on {as_of_me}: no data.")

    index = history.index.tz_localize(None) if history.index.tz is not None else history.index
    closes = history.loc[index.normalize() <= pd.Timestamp(as_of_me), "Close"]
    if closes.empty:
        raise ValueError(f"FX lookup failed for {code}/USD on {as_of_me}: no close on or before date.")
    return float(closes.iloc[-1])


@dataclass
class Fund:
    """Single-fund monthly cash-flow projection (Takahashi–Alexander style)."""

    name: str
    commitment: float
    funded_amount: float
    stated_nav: float
    unfunded: float | None
    initial_commitment_month: date
    invest_end_date: date
    reinvest_end_date: date
    termination_date: date
    annual_return: float
    dist_rate: float
    pct_drawn: float
    mgmt_fee: float
    paid_on_committed: bool
    carry_rate: float
    carry_hurdle: float
    currency: str
    volatility: str = ""
    asset_class: str = ""
    sub_asset_class: str = ""
    geography: str = ""

    def __post_init__(self) -> None:
        """Normalize dates, floor bad NAV/funded inputs, seed unfunded and opening UGL."""
        self.initial_commitment_month = month_end(self.initial_commitment_month)
        self.invest_end_date = month_end(self.invest_end_date)
        self.reinvest_end_date = month_end(self.reinvest_end_date)
        self.termination_date = month_end(self.termination_date)
        self.currency = str(self.currency).strip().upper()

        self._raw_stated_nav = float(self.stated_nav)
        self._raw_funded_amount = float(self.funded_amount)
        if self.funded_amount < 0:
            warnings.warn(
                f"Fund {self.name!r}: funded_amount < 0; flooring to 0 for projection.",
                stacklevel=2,
            )
            self.funded_amount = 0.0
        if self.stated_nav <= 0:
            warnings.warn(
                f"Fund {self.name!r}: stated_nav <= 0; flooring starting NAV to 0.",
                stacklevel=2,
            )
            self.stated_nav = max(float(self.stated_nav), 0.0)

        effective = self.commitment * self.pct_drawn
        if self.unfunded is None:
            self._initial_remaining_effective = max(0.0, effective - self.funded_amount)
        else:
            self._initial_remaining_effective = max(0.0, float(self.unfunded))

        if self.funded_amount > effective + 1.0:
            warnings.warn(
                f"Fund {self.name!r}: Funded Amount ({self.funded_amount:,.2f}) exceeds "
                f"effective commitment ({effective:,.2f}) at % drawn={self.pct_drawn:.0%}; "
                "no further capital calls will be modeled.",
                stacklevel=2,
            )
            self._initial_remaining_effective = 0.0

        self._seed_beginning_unrealized_gl = max(
            self._raw_stated_nav - self._raw_funded_amount, 0.0
        )

    @property
    def monthly_return(self) -> float:
        return (1.0 + self.annual_return) ** (1.0 / 12.0) - 1.0

    @property
    def monthly_distribution(self) -> float:
        return self.dist_rate / 12.0

    def _period_type(self, period_end: date) -> str:
        """Life-cycle label for the month (invest → reinvest → harvest)."""
        end = month_end(period_end)
        if end <= self.invest_end_date:
            return "Invest"
        if end <= self.reinvest_end_date:
            return "Reinvest"
        return "Harvest"

    def _commitment_snapshot(
        self, period_end: date, active: bool, ending_capital: float
    ) -> dict[str, float]:
        """Commitment, effective commitment, and unfunded balances at period_end."""
        total = self.commitment if active else 0.0
        effective = total * self.pct_drawn
        return {
            "total_commitment": total,
            "effective_commitment": effective,
            "remaining_effective_unfunded": (
                max(0.0, effective - ending_capital)
                if self.invest_end_date >= period_end
                else 0.0
            ),
            "legal_unfunded": (
                max(0.0, total - ending_capital)
                if self.reinvest_end_date > period_end
                else 0.0
            ),
        }

    def _period_row(
        self,
        period_index: int,
        period_start: date,
        period_end: date,
        active: bool,
        metrics: dict[str, float],
    ) -> dict[str, Any]:
        """One output row: dates, period_type, commitment snapshot, and cash-flow metrics."""
        return {
            "period": period_index,
            "start_date": period_start,
            "end_date": month_end(period_end),
            "period_type": self._period_type(period_end),
            **self._commitment_snapshot(period_end, active, metrics["ending_capital"]),
            **metrics,
        }

    def _is_commitment_active(self, period_end: date) -> bool:
        """Whether total commitment still applies (between initial month and termination)."""
        return (
            period_end >= self.initial_commitment_month
            and period_end <= self.termination_date
        )

    def _capital_call(
        self,
        period_start: date,
        period_end: date,
        prior_remaining_effective: float,
        projection_start: date,
    ) -> float:
        """Linear draw of remaining effective unfunded through invest_end_date."""
        if (
            self.reinvest_end_date < projection_start
            or period_start > self.invest_end_date
            or period_end > self.invest_end_date
        ):
            return 0.0
        months = _datedif_months(period_start, self.invest_end_date)
        return prior_remaining_effective / months if months > 0 else 0.0

    def _harvest_outflow(self, period_start: date, period_end: date, base: float) -> float:
        """Negative cash flow spreading *base* from reinvest end through termination."""
        if period_end <= self.reinvest_end_date:
            return 0.0
        if period_start <= self.termination_date <= period_end:
            return -base
        if self.reinvest_end_date < period_end <= self.termination_date:
            months = _excel_months_span(period_end, self.termination_date)
            return -base / months if months > 0 else 0.0
        return 0.0

    def _return_of_capital(
        self,
        period_start: date,
        period_end: date,
        beginning_capital: float,
        capital_called: float,
    ) -> float:
        """ROC during harvest (same schedule as other harvest outflows)."""
        return self._harvest_outflow(
            period_start, period_end, beginning_capital + capital_called
        )

    def _gain_on_sale(
        self,
        period_start: date,
        period_end: date,
        beginning_unrealized_gl: float,
        period_gl: float,
    ) -> float:
        """Realize unrealized G/L at termination or via harvest schedule."""
        unrealized_base = beginning_unrealized_gl + period_gl
        if period_start <= self.termination_date <= period_end:
            return -unrealized_base
        if unrealized_base < 0:
            return 0.0
        return self._harvest_outflow(period_start, period_end, unrealized_base)

    def _management_fee(
        self,
        period_end: date,
        prior_ending_capital: float,
        effective_commitment: float,
    ) -> float:
        """Monthly mgmt fee on commitment (invest phase) or invested capital."""
        if self.paid_on_committed and period_end <= self.invest_end_date:
            return -effective_commitment * (self.mgmt_fee / 12.0)
        if prior_ending_capital > 0:
            return -prior_ending_capital * (self.mgmt_fee / 12.0)
        return 0.0

    def _carry_amount(
        self, pre_carry_income: float, prior_ending_capital: float
    ) -> float:
        """Carried interest when annualized pre-carry return exceeds the hurdle."""
        if prior_ending_capital <= 0:
            return 0.0
        annualized = (pre_carry_income / prior_ending_capital) * 12.0
        if annualized > self.carry_hurdle:
            return -pre_carry_income * self.carry_rate
        return 0.0

    def _dividend(self, period_end: date, net_income: float, prior_nav: float) -> float:
        """Cash distribution capped by target yield and available net income."""
        if self.dist_rate <= 0.0 or period_end >= self.termination_date:
            return 0.0
        target = prior_nav * self.monthly_distribution
        dividend = min(-min(net_income, target), 0.0)
        return max(dividend, -net_income)

    def _requires_empty_projection(self, projection_start: date) -> bool:
        """Already terminated with no NAV — only emit a zeroed as-of row."""
        return self.termination_date <= projection_start and self.stated_nav <= 0

    def _requires_immediate_liquidation(self, projection_start: date) -> bool:
        """Terminated on or before as-of — liquidate in the following month."""
        if self.stated_nav <= 0:
            return False
        if self.termination_date <= projection_start:
            return True
        first_period_start = projection_start.replace(day=1)
        return first_period_start <= self.termination_date <= projection_start

    def _single_period_frame(self, period: int, row: dict[str, Any]) -> pd.DataFrame:
        return pd.DataFrame([{"period": period, **row}])

    def _empty_projection(self, start_date: date | datetime) -> pd.DataFrame:
        """Single period-0 row for funds already wound down at the as-of date."""
        projection_start = month_end(start_date)
        return self._single_period_frame(
            0,
            {
                "start_date": projection_start.replace(day=1),
                "end_date": projection_start,
                "period_type": self._period_type(projection_start),
                "total_commitment": 0.0,
                "effective_commitment": 0.0,
                "remaining_effective_unfunded": 0.0,
                "legal_unfunded": 0.0,
                "beginning_capital_account": _NAN,
                "capital_called": 0.0,
                "roc": 0.0,
                "ending_capital": 0.0,
                "beginning_unrealized_gl": _NAN,
                "period_gl": _NAN,
                "gain_on_sale": _NAN,
                "ending_unrealized_gl": 0.0,
                "nav": 0.0,
                "asset_income": _NAN,
                "mgmt_fee_amt": _NAN,
                "pre_carry_income": _NAN,
                "carry_amt": _NAN,
                "dividend": 0.0,
                "retained_income": _NAN,
                "net_cf": 0.0,
            },
        )

    def _liquidation_period(
        self, prior_ending_capital: float, prior_nav: float
    ) -> dict[str, float]:
        """Wind down residual NAV in the month after the as-of period."""
        if self.funded_amount > 0:
            roc = min(prior_nav, self.funded_amount)
        else:
            roc = prior_nav
        gain_on_sale = max(0.0, prior_nav - self.funded_amount)
        return {
            "beginning_capital_account": prior_ending_capital,
            "capital_called": 0.0,
            "roc": roc,
            "ending_capital": 0.0,
            "beginning_unrealized_gl": 0.0,
            "period_gl": 0.0,
            "gain_on_sale": gain_on_sale,
            "ending_unrealized_gl": 0.0,
            "nav": 0.0,
            "asset_income": 0.0,
            "mgmt_fee_amt": 0.0,
            "pre_carry_income": 0.0,
            "carry_amt": 0.0,
            "dividend": 0.0,
            "retained_income": 0.0,
            "net_cf": roc + gain_on_sale,
        }

    def _as_of_period(self, period_end: date, active: bool) -> dict[str, float]:
        """Period 0: stated NAV snapshot; flows are NaN except net_cf offset."""
        if period_end >= self.invest_end_date:
            ending_capital = min(self.stated_nav, self.commitment)
        else:
            ending_capital = self.funded_amount if active else 0.0
        return {
            "beginning_capital_account": _NAN,
            "capital_called": 0.0,
            "roc": 0.0,
            "ending_capital": ending_capital,
            "beginning_unrealized_gl": _NAN,
            "period_gl": _NAN,
            "gain_on_sale": _NAN,
            "ending_unrealized_gl": 0.0,
            "nav": self.stated_nav,
            "asset_income": _NAN,
            "mgmt_fee_amt": _NAN,
            "pre_carry_income": _NAN,
            "carry_amt": _NAN,
            "dividend": _NAN,
            "retained_income": _NAN,
            "net_cf": -self.stated_nav,
        }

    def _operating_period(
        self,
        period_index: int,
        period_start: date,
        period_end: date,
        projection_start: date,
        effective_commitment: float,
        prior_ending_capital: float,
        prior_ending_ugl: float,
        prior_nav: float,
        prior_remaining_effective: float,
    ) -> dict[str, float]:
        """One operating month: calls, harvest, income, fees, carry, NAV roll-forward."""
        beginning_capital = prior_ending_capital
        beginning_ugl = (
            self._seed_beginning_unrealized_gl if period_index == 1 else prior_ending_ugl
        )
        capital_called = self._capital_call(
            period_start, period_end, prior_remaining_effective, projection_start
        )
        roc = self._return_of_capital(
            period_start, period_end, beginning_capital, capital_called
        )
        ending_capital = beginning_capital + capital_called + roc

        asset_income = (
            max(prior_nav * self.monthly_return, 0.0)
            if period_end <= self.termination_date
            else 0.0
        )
        mgmt_fee_amt = self._management_fee(
            period_end, prior_ending_capital, effective_commitment
        )
        pre_carry_income = asset_income + mgmt_fee_amt
        carry_amt = self._carry_amount(pre_carry_income, prior_ending_capital)
        net_income = pre_carry_income + carry_amt
        dividend = self._dividend(period_end, net_income, prior_nav)
        retained_income = net_income + dividend
        period_gl = retained_income
        gain_on_sale = self._gain_on_sale(
            period_start, period_end, beginning_ugl, period_gl
        )
        ending_ugl = beginning_ugl + period_gl + gain_on_sale
        nav = ending_capital + ending_ugl

        return {
            "beginning_capital_account": beginning_capital,
            "capital_called": capital_called,
            "roc": roc,
            "ending_capital": ending_capital,
            "beginning_unrealized_gl": beginning_ugl,
            "period_gl": period_gl,
            "gain_on_sale": gain_on_sale,
            "ending_unrealized_gl": ending_ugl,
            "nav": nav,
            "asset_income": asset_income,
            "mgmt_fee_amt": mgmt_fee_amt,
            "pre_carry_income": pre_carry_income,
            "carry_amt": carry_amt,
            "dividend": dividend,
            "retained_income": retained_income,
            "net_cf": -(capital_called + roc + gain_on_sale + dividend),
        }

    def project(self, start_date: date | datetime, end_date: date | datetime) -> pd.DataFrame:
        """Monthly cash flows from as-of (period 0) through end_date."""
        projection_start = month_end(start_date)
        projection_end = month_end(end_date)
        if projection_end < projection_start:
            raise ValueError("end_date must be on or after start_date")

        if self._requires_empty_projection(projection_start):
            return self._empty_projection(start_date)

        rows: list[dict[str, Any]] = []
        period_end = projection_start
        period_start = period_end.replace(day=1)
        period_index = 0

        prior_ending_capital = 0.0
        prior_ending_ugl = 0.0
        prior_nav = 0.0
        prior_remaining_effective = self._initial_remaining_effective

        while period_end <= projection_end:
            active = self._is_commitment_active(period_end)
            effective_commitment = (self.commitment if active else 0.0) * self.pct_drawn

            if period_index == 0:
                metrics = self._as_of_period(period_end, active)
            else:
                metrics = self._operating_period(
                    period_index,
                    period_start,
                    period_end,
                    projection_start,
                    effective_commitment,
                    prior_ending_capital,
                    prior_ending_ugl,
                    prior_nav,
                    prior_remaining_effective,
                )

            rows.append(self._period_row(period_index, period_start, period_end, active, metrics))

            prior_ending_capital = metrics["ending_capital"]
            prior_ending_ugl = metrics["ending_unrealized_gl"]
            prior_nav = metrics["nav"]
            prior_remaining_effective = rows[-1]["remaining_effective_unfunded"]

            if period_index == 0 and self._requires_immediate_liquidation(projection_start):
                period_start, period_end = _advance_period_end(period_end)
                active = self._is_commitment_active(period_end)
                liq = self._liquidation_period(prior_ending_capital, prior_nav)
                rows.append(self._period_row(1, period_start, period_end, active, liq))
                break

            if period_end >= projection_end:
                break

            period_index += 1
            period_start, period_end = _advance_period_end(period_end)

        return pd.DataFrame(rows)


class Portfolio:
    """Sum fund projections into a single USD monthly series."""

    def __init__(self, funds: list[Fund]) -> None:
        self.funds = list(funds)

    @property
    def portfolio_termination_date(self) -> date | None:
        if not self.funds:
            return None
        return max(fund.termination_date for fund in self.funds)

    def aggregate(
        self,
        start_date: date | datetime | None = None,
    ) -> pd.DataFrame:
        """Top-line portfolio monthly cash flows in USD."""
        if not self.funds:
            return pd.DataFrame()
        detail = build_portfolio_monthly_detail(self.funds, start_date)
        return _sum_portfolio_monthly(detail)


def load_funds_from_assumptions(
    filepath: str | Path,
    as_of_date: date | datetime | None = None,
) -> list[Fund]:
    """Read FundAssumptions.xlsx into validated Fund instances."""
    path = Path(filepath)
    as_of = month_end(as_of_date or DEFAULT_AS_OF_DATE)

    raw_df = pd.read_excel(path)
    raw_df.columns = _normalize_assumptions_columns(raw_df.columns)

    missing = set(_REQUIRED_FUND_ASSUMPTIONS_COLUMNS) - set(raw_df.columns)
    if missing:
        raise ValueError(f"Missing required columns in {path}: {sorted(missing)}")

    funds: list[Fund] = []
    for _, row in raw_df.iterrows():
        name = row.get("Investment Name")
        if pd.isna(name) or str(name).strip() == "":
            continue

        kwargs = _parse_assumptions_row(row, as_of, path)
        validate_fund_assumptions_row(kwargs)
        funds.append(Fund(**kwargs))

    return funds


def validate_fund(
    fund: Fund,
    start_date: date | datetime,
    end_date: date | datetime,
    projection: pd.DataFrame | None = None,
) -> list[str]:
    """Post-projection sanity checks (termination NAV, call cap, negative NAV)."""
    df = projection if projection is not None else fund.project(start_date, end_date)
    errors: list[str] = []

    termination_rows = df[df["end_date"] == fund.termination_date]
    if termination_rows.empty:
        errors.append("No projection period ends on termination_date.")
    elif termination_rows["nav"].iloc[-1] > 1.0:
        errors.append(
            f"NAV at termination ({termination_rows['nav'].iloc[-1]:,.2f}) "
            "does not reach 0."
        )

    total_calls = df["capital_called"].sum()
    effective = fund.commitment * fund.pct_drawn
    if total_calls > effective + 1.0:
        errors.append(
            f"Total capital calls ({total_calls:,.2f}) exceed "
            f"effective commitment ({effective:,.2f})."
        )

    negative_nav = df[df["nav"] < -1.0]
    if not negative_nav.empty:
        errors.append(f"Negative NAV in period(s): {negative_nav['period'].tolist()}.")

    return errors


_ASSUMPTIONS_PATH = Path(__file__).resolve().parent / "inputs" / "FundAssumptions.xlsx"
_CASH_FLOW_OUTPUT_PATH = Path(__file__).resolve().parent / "outputs" / "CashFlowOutputs.xlsx"

_FUND_ANNUAL_GROUP_KEYS = ("fund", "currency", *_FUND_METADATA_COLUMNS)

_PORTFOLIO_ANNUAL_TAG_CATEGORIES = (
    "period_type",
    "volatility",
    "asset_class",
    "sub_asset_class",
    "geography",
)

_FUND_DETAIL_COLUMNS = [
    "fund",
    "currency",
    *_FUND_METADATA_COLUMNS,
    "period",
    "start_date",
    "end_date",
    "period_type",
    *NUMERIC_OUTPUT_COLUMNS,
]


def _sum_portfolio_monthly(monthly_df: pd.DataFrame) -> pd.DataFrame:
    """Add fund monthly rows into one USD portfolio series by month."""
    if monthly_df.empty:
        return pd.DataFrame()
    aggregated = monthly_df.groupby(["period", "start_date", "end_date"], as_index=False)[
        _numeric_columns(monthly_df)
    ].sum()
    aggregated.insert(0, "currency", PORTFOLIO_REPORTING_CURRENCY)
    return aggregated


def build_portfolio_monthly_detail(
    funds: list[Fund],
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """All funds in USD, with labels needed for portfolio annual breakdowns."""
    as_of = month_end(as_of_date or DEFAULT_AS_OF_DATE)
    frames: list[pd.DataFrame] = []
    for fund in funds:
        df = _convert_fund_to_usd(fund.project(as_of, fund.termination_date), fund, as_of)
        for col in _FUND_METADATA_COLUMNS:
            df[col] = getattr(fund, col)
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True)
    return _pick_columns(
        combined,
        [
            "period",
            "start_date",
            "end_date",
            "period_type",
            *_FUND_METADATA_COLUMNS,
            *NUMERIC_OUTPUT_COLUMNS,
        ],
    )


def aggregate_portfolio_annual_tagged(
    portfolio_monthly_usd: pd.DataFrame,
    *,
    as_of_date: date | datetime,
) -> pd.DataFrame:
    """Annual portfolio view: one TOTAL row set plus slices by category tag."""
    slices: list[pd.DataFrame] = []

    def _tagged_annual(monthly: pd.DataFrame, tag_category: str, tag: str) -> None:
        annual = aggregate_monthly_to_annual(
            monthly, as_of_date=as_of_date, group_keys=("currency",)
        )
        if annual.empty:
            return
        annual.insert(1, "tag_category", tag_category)
        annual.insert(2, "tag", tag)
        slices.append(annual)

    _tagged_annual(_sum_portfolio_monthly(portfolio_monthly_usd), "TOTAL", "TOTAL")
    for category in _PORTFOLIO_ANNUAL_TAG_CATEGORIES:
        if category not in portfolio_monthly_usd.columns:
            continue
        for tag_value in sorted(portfolio_monthly_usd[category].dropna().unique(), key=str):
            subset = portfolio_monthly_usd.loc[portfolio_monthly_usd[category] == tag_value]
            _tagged_annual(_sum_portfolio_monthly(subset), category, str(tag_value))

    if not slices:
        return pd.DataFrame()

    result = pd.concat(slices, ignore_index=True)
    leading = ["currency", "tag_category", "tag", "year", "operating_months"]
    trailing = [c for c in ANNUAL_OUTPUT_COLUMNS if c in result.columns]
    return result[leading + trailing]


def build_fund_detail(
    funds: list[Fund],
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """Monthly fund cash flows in each fund's own currency."""
    as_of = month_end(as_of_date or DEFAULT_AS_OF_DATE)
    frames: list[pd.DataFrame] = []
    for fund in funds:
        df = fund.project(as_of, fund.termination_date)
        df.insert(0, "fund", fund.name)
        df.insert(1, "currency", fund.currency)
        for offset, col in enumerate(_FUND_METADATA_COLUMNS, start=2):
            df.insert(offset, col, getattr(fund, col))
        frames.append(df)
    return _pick_columns(pd.concat(frames, ignore_index=True), list(_FUND_DETAIL_COLUMNS))


# --- Excel export ---


def export_cash_flow_outputs(
    assumptions_path: Path | str = _ASSUMPTIONS_PATH,
    output_path: Path | str = _CASH_FLOW_OUTPUT_PATH,
    as_of_date: date | datetime | None = None,
) -> Path:
    """Build all four output tabs and save CashFlowOutputs.xlsx."""
    assumptions_path = Path(assumptions_path)
    output_path = Path(output_path)
    as_of = as_of_date or DEFAULT_AS_OF_DATE

    funds = load_funds_from_assumptions(assumptions_path, as_of_date=as_of)
    funds_monthly_df = build_fund_detail(funds, as_of)
    portfolio_monthly_df = Portfolio(funds).aggregate(as_of)
    portfolio_monthly_usd = build_portfolio_monthly_detail(funds, as_of)
    funds_annual_df = aggregate_monthly_to_annual(
        funds_monthly_df, as_of_date=as_of, group_keys=_FUND_ANNUAL_GROUP_KEYS
    )
    portfolio_annual_df = aggregate_portfolio_annual_tagged(
        portfolio_monthly_usd, as_of_date=as_of
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        funds_monthly_df.to_excel(writer, sheet_name="Funds (Monthly)", index=False)
        funds_annual_df.to_excel(writer, sheet_name="Funds (Annual)", index=False)
        portfolio_monthly_df.to_excel(writer, sheet_name="Portfolio (Monthly)", index=False)
        portfolio_annual_df.to_excel(writer, sheet_name="Portfolio (Annual)", index=False)

    print(f"Loaded {len(funds)} funds from {assumptions_path.name}")
    print(
        f"Portfolio (monthly): {len(portfolio_monthly_df)} periods "
        f"(through {portfolio_monthly_df['end_date'].max()})"
    )
    print(f"Funds (monthly): {len(funds_monthly_df)} rows")
    print(f"Funds (annual): {len(funds_annual_df)} rows")
    print(f"Portfolio (annual): {len(portfolio_annual_df)} rows")
    print(f"Wrote {output_path}")
    return output_path


def main() -> None:
    """CLI entry: load assumptions and write outputs/CashFlowOutputs.xlsx."""
    export_cash_flow_outputs()


if __name__ == "__main__":
    main()