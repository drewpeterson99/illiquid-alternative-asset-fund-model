"""Tests for loading funds from inputs/FundAssumptions.xlsx."""

from __future__ import annotations

from datetime import date

import pytest

from fund_engine import DEFAULT_AS_OF_DATE, Fund, Portfolio, build_fund_detail, load_funds_from_assumptions, month_end

from tests.conftest import ASSUMPTIONS_PATH
from tests.factories import make_fund


def test_load_funds_from_assumptions(assumptions_funds: list[Fund]) -> None:
    assert len(assumptions_funds) >= 1
    names = [f.name for f in assumptions_funds]
    assert len(names) == len(set(names))
    assert all(names)
    assert all(f.initial_commitment_month == DEFAULT_AS_OF_DATE for f in assumptions_funds)

    as_of = date(2026, 6, 30)
    funds = load_funds_from_assumptions(ASSUMPTIONS_PATH, as_of_date=as_of)
    assert len(funds) == len(assumptions_funds)
    assert funds[0].initial_commitment_month == month_end(as_of)


def test_assumptions_monthly_output_columns(assumptions_funds: list[Fund]) -> None:
    """Any loaded fund produces the expected detail columns (structure only)."""
    sample = assumptions_funds[0]
    monthly = build_fund_detail([sample])
    assert list(monthly.columns[:6]) == [
        "fund",
        "currency",
        "volatility",
        "asset_class",
        "sub_asset_class",
        "geography",
    ]
    assert monthly["fund"].iloc[0] == sample.name
    assert sample.commitment > 0
    assert sample.reinvest_end_date >= sample.invest_end_date
    assert sample.termination_date >= sample.reinvest_end_date


def test_portfolio_extends_to_latest_termination() -> None:
    """Portfolio horizon follows the latest fund termination among members."""
    as_of = DEFAULT_AS_OF_DATE
    fund_early = make_fund(
        name="Early Exit",
        funded_amount=50_000_000,
        stated_nav=55_000_000,
        invest_end_date=date(2024, 6, 30),
        reinvest_end_date=date(2025, 6, 30),
        termination_date=date(2026, 6, 30),
    )
    fund_late = make_fund(
        name="Long Horizon",
        funded_amount=80_000_000,
        stated_nav=90_000_000,
        invest_end_date=date(2026, 6, 30),
        reinvest_end_date=date(2028, 6, 30),
        termination_date=date(2030, 6, 30),
    )
    portfolio = Portfolio([fund_early, fund_late])
    assert portfolio.portfolio_termination_date == fund_late.termination_date

    agg = portfolio.aggregate(as_of)
    assert agg["end_date"].max() == fund_late.termination_date

    solo_early = fund_early.project(as_of, fund_early.termination_date)
    solo_late = fund_late.project(as_of, fund_late.termination_date)
    assert len(solo_early) < len(solo_late)

    after_early_ends = agg[agg["end_date"] > fund_early.termination_date]
    late_only = solo_late[solo_late["end_date"] > fund_early.termination_date]
    for col in ["nav", "net_cf"]:
        merged = after_early_ends.merge(
            late_only[["end_date", col]], on="end_date", suffixes=("_agg", "_solo")
        )
        for _, row in merged.iterrows():
            assert row[f"{col}_agg"] == pytest.approx(row[f"{col}_solo"])
