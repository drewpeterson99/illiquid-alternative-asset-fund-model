"""Tests for assumptions validation and projection edge cases."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fund_engine import (
    DEFAULT_AS_OF_DATE,
    Fund,
    Portfolio,
    fx_rate_to_usd,
    load_funds_from_assumptions,
    validate_fund_assumptions_row,
)

from tests.conftest import ASSUMPTIONS_PATH
from tests.factories import fund_kwargs, make_fund


def test_validate_initial_commitment_when_enabled() -> None:
    data = fund_kwargs(invest_end_date=date(2025, 1, 31))
    with pytest.raises(ValueError, match="Investment End Date"):
        validate_fund_assumptions_row(data, validate_initial_commitment=True)


@pytest.mark.parametrize(
    "field, value, match",
    [
        ("reinvest_end_date", date(2027, 1, 31), "Reinvestment End Date"),
        ("termination_date", date(2028, 1, 31), "Termination Date"),
        ("commitment", 0.0, "Total Commitment"),
        ("funded_amount", 200_000_000.0, "cannot exceed"),
        ("pct_drawn", 1.5, "% of Capital Drawn"),
        ("annual_return", 1.5, "annual_return"),
        ("currency", "EUR", "Currency"),
    ],
)
def test_validate_fund_assumptions_row_rejects_bad_input(
    field: str, value: object, match: str
) -> None:
    data = fund_kwargs(**{field: value})
    with pytest.raises(ValueError, match=match):
        validate_fund_assumptions_row(data)


def test_paid_on_committed_strict_coercion() -> None:
    from fund_engine import _coerce_paid_on_committed_strict

    assert _coerce_paid_on_committed_strict(True, "X") is True
    assert _coerce_paid_on_committed_strict("FALSE", "X") is False
    assert _coerce_paid_on_committed_strict(1, "X") is True
    with pytest.raises(ValueError, match="Paid on Committed"):
        _coerce_paid_on_committed_strict("maybe", "X")


def test_empty_projection_when_past_term_and_zero_nav() -> None:
    fund = make_fund(
        name="Wound Down",
        funded_amount=0,
        stated_nav=0,
        invest_end_date=date(2024, 1, 31),
        reinvest_end_date=date(2024, 1, 31),
        termination_date=date(2025, 6, 30),
    )
    df = fund.project(date(2026, 1, 31), date(2027, 6, 30))
    assert len(df) == 1
    assert df.iloc[0]["period"] == 0
    assert df.iloc[0]["nav"] == 0.0
    assert df.iloc[0]["net_cf"] == 0.0


def test_reinvest_equals_invest_no_calls_and_roc_same_month() -> None:
    fund = make_fund(
        name="Deploy Harvest",
        invest_end_date=date(2026, 6, 30),
        reinvest_end_date=date(2026, 6, 30),
        termination_date=date(2030, 6, 30),
        funded_amount=0,
        stated_nav=0,
        pct_drawn=1.0,
        unfunded=50_000_000.0,
    )
    df = fund.project(DEFAULT_AS_OF_DATE, date(2026, 12, 31))
    june = df[df["end_date"] == date(2026, 6, 30)]
    assert len(june) == 1
    row = june.iloc[0]
    if row["period"] > 0:
        assert row["capital_called"] == 0.0 or row["roc"] == 0.0


def test_dividend_zero_when_dist_rate_zero() -> None:
    fund = make_fund(dist_rate=0.0)
    df = fund.project(DEFAULT_AS_OF_DATE, date(2026, 12, 31))
    operating = df[df["period"] > 0]
    assert (operating["dividend"].fillna(0) == 0.0).all()


def test_seed_beginning_unrealized_gl_when_nav_exceeds_funded() -> None:
    fund = make_fund(stated_nav=80_000_000.0, funded_amount=50_000_000.0)
    assert fund._seed_beginning_unrealized_gl == pytest.approx(30_000_000.0)
    df = fund.project(DEFAULT_AS_OF_DATE, date(2026, 3, 31))
    period_1 = df[df["period"] == 1]
    if not period_1.empty:
        assert period_1.iloc[0]["beginning_unrealized_gl"] == pytest.approx(
            30_000_000.0
        )


def test_negative_funded_floors_with_warning() -> None:
    with pytest.warns(UserWarning, match="funded_amount"):
        fund = make_fund(funded_amount=-1_000_000.0)
    assert fund.funded_amount == 0.0


def test_funded_exceeds_effective_commitment_warns_and_no_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.warns(UserWarning, match="exceeds effective commitment"):
        fund = make_fund(funded_amount=90_000_000.0, pct_drawn=0.50)
    assert fund._initial_remaining_effective == 0.0


def test_fx_rate_to_usd_uses_yfinance_on_as_of_date(mocker) -> None:
    fx_rate_to_usd.cache_clear()
    mock_history = mocker.patch("fund_engine.yf.Ticker")
    mock_history.return_value.history.return_value = pd.DataFrame(
        {"Close": [1.3466]},
        index=pd.to_datetime(["2025-12-31"]),
    )

    rate = fx_rate_to_usd("GBP", DEFAULT_AS_OF_DATE)

    assert rate == pytest.approx(1.3466)
    mock_history.assert_called_once_with("GBPUSD=X")
    mock_history.return_value.history.assert_called_once()
    assert fx_rate_to_usd("USD", DEFAULT_AS_OF_DATE) == 1.0


def test_portfolio_converts_gbp_to_usd(mocker) -> None:
    rate = 1.25
    mocker.patch("fund_engine.fx_rate_to_usd", return_value=rate)

    usd = make_fund(name="USD Fund", currency="USD")
    gbp = make_fund(
        name="GBP Fund",
        currency="GBP",
        commitment=10_000_000.0,
        funded_amount=5_000_000.0,
        stated_nav=6_000_000.0,
    )
    agg = Portfolio([usd, gbp]).aggregate(DEFAULT_AS_OF_DATE)
    solo_usd = usd.project(DEFAULT_AS_OF_DATE, date(2026, 3, 31))
    solo_gbp = gbp.project(DEFAULT_AS_OF_DATE, date(2026, 3, 31))
    row = agg[agg["period"] == 0].iloc[0]
    usd_nav = solo_usd.loc[solo_usd["period"] == 0, "nav"].iloc[0]
    gbp_nav = solo_gbp.loc[solo_gbp["period"] == 0, "nav"].iloc[0]
    assert row["nav"] == pytest.approx(usd_nav + gbp_nav * rate)
    assert (agg["currency"] == "USD").all()


def test_load_funds_rejects_invalid_paid_on_committed(tmp_path) -> None:
    path = tmp_path / "bad.xlsx"
    row = {
        "Investment Name": "Bad",
        "Total Commitment": 1e6,
        "Stated Unfunded": pd.NA,
        "Funded Amount": 0,
        "Investment End Date": date(2028, 6, 30),
        "Reinvestment End Date": date(2029, 6, 30),
        "Termination Date": date(2031, 6, 30),
        "Stated NAV": 0,
        "Ann. Asset Class Return (Conservative)": 0.1,
        "Annual Fund Distribution Target": 0.05,
        "% of Capital Drawn": 0.85,
        "Mgmt Fee": 0.01,
        "Paid on Committed?": "YES",
        "Carry": 0.15,
        "Carry Hurdle": 0.08,
        "Currency": "USD",
    }
    pd.DataFrame([row]).to_excel(path, index=False)
    with pytest.raises(ValueError, match="Paid on Committed"):
        load_funds_from_assumptions(path)
