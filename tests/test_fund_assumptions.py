"""Tests for loading funds from inputs/FundAssumptions.xlsx."""

from __future__ import annotations

from datetime import date

import pytest

from fund_engine import DEFAULT_AS_OF_DATE, Fund, Portfolio, build_fund_detail, load_funds_from_assumptions, month_end

from tests.conftest import ASSUMPTIONS_PATH


def test_load_funds_from_assumptions(assumptions_funds: list[Fund]) -> None:
    assert len(assumptions_funds) == 17
    assert all(f.initial_commitment_month == DEFAULT_AS_OF_DATE for f in assumptions_funds)

    as_of = date(2026, 6, 30)
    funds = load_funds_from_assumptions(ASSUMPTIONS_PATH, as_of_date=as_of)
    assert funds[0].initial_commitment_month == month_end(as_of)


def test_fund_a_assumptions_and_monthly_columns(assumptions_funds: list[Fund]) -> None:
    fund_a = next(f for f in assumptions_funds if f.name == "Fund A")
    assert fund_a.commitment == pytest.approx(500_000_000)
    assert fund_a.annual_return == pytest.approx(0.165)
    assert fund_a.pct_drawn == pytest.approx(0.90)
    assert fund_a.paid_on_committed is False
    assert fund_a.currency == "USD"
    assert fund_a.invest_end_date == month_end(date(2023, 6, 30))
    assert fund_a.volatility == "Medium Vol"
    assert fund_a.asset_class == "Hybrid/Structured Equity"
    assert fund_a.sub_asset_class == "Preferred Equity"
    assert fund_a.geography == "US"

    monthly = build_fund_detail([fund_a])
    assert list(monthly.columns[:6]) == [
        "fund",
        "currency",
        "volatility",
        "asset_class",
        "sub_asset_class",
        "geography",
    ]


def test_portfolio_extends_to_latest_termination(assumptions_funds: list[Fund]) -> None:
    fund_a = next(f for f in assumptions_funds if f.name == "Fund A")
    fund_b = next(f for f in assumptions_funds if f.name == "Fund B")
    portfolio = Portfolio([fund_a, fund_b])

    assert portfolio.portfolio_termination_date == fund_b.termination_date
    assert fund_a.termination_date < fund_b.termination_date

    agg = portfolio.aggregate(DEFAULT_AS_OF_DATE)
    assert agg["end_date"].max() == fund_b.termination_date

    solo_a = fund_a.project(DEFAULT_AS_OF_DATE, fund_a.termination_date)
    solo_b = fund_b.project(DEFAULT_AS_OF_DATE, fund_b.termination_date)
    assert len(solo_a) < len(solo_b)

    after_a_ends = agg[agg["end_date"] > fund_a.termination_date]
    b_after_a = solo_b[solo_b["end_date"] > fund_a.termination_date]
    for col in ["nav", "net_cf"]:
        merged = after_a_ends.merge(
            b_after_a[["end_date", col]], on="end_date", suffixes=("_agg", "_b")
        )
        for _, row in merged.iterrows():
            assert row[f"{col}_agg"] == pytest.approx(row[f"{col}_b"])
