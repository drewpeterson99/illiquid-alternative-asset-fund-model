"""Regression tests against ModelTemplate.xlsx and core projection behavior."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fund_engine import Fund, validate_fund
from tests.factories import make_fund
from tests.model_template import PROJECTION_END, PROJECTION_START

_REL_TOL = 1e-9
_ABS_TOL = 0.01

_NUMERIC_COLUMNS = [
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


def test_template_fund_setup(template_fund: Fund) -> None:
    assert template_fund.name == "Example Fund"
    assert template_fund.commitment == 250_000_000
    assert template_fund.funded_amount == pytest.approx(102_430_077.03)
    assert template_fund.annual_return == pytest.approx(0.165)
    assert template_fund.monthly_return == pytest.approx((1 + 0.165) ** (1 / 12) - 1)


def test_period_type_by_phase(template_fund: Fund, engine_projection: pd.DataFrame) -> None:
    invest_end = template_fund.invest_end_date
    reinvest_end = template_fund.reinvest_end_date
    for _, row in engine_projection.iterrows():
        end = row["end_date"]
        if end <= invest_end:
            assert row["period_type"] == "Invest"
        elif end <= reinvest_end:
            assert row["period_type"] == "Reinvest"
        else:
            assert row["period_type"] == "Harvest"


def test_full_projection_matches_excel(
    engine_projection: pd.DataFrame, excel_golden: pd.DataFrame
) -> None:
    excel = excel_golden[excel_golden["end_date"] <= PROJECTION_END].set_index("period")
    engine = engine_projection.set_index("period")

    assert len(engine) == len(excel)
    assert list(engine.index) == list(excel.index)
    for date_col in ("start_date", "end_date"):
        assert engine[date_col].tolist() == excel[date_col].tolist()

    pd.testing.assert_frame_equal(
        engine[_NUMERIC_COLUMNS],
        excel[_NUMERIC_COLUMNS],
        rtol=_REL_TOL,
        atol=_ABS_TOL,
    )


def test_validate_fund_template_passes(
    template_fund: Fund, engine_projection: pd.DataFrame
) -> None:
    errors = validate_fund(
        template_fund, PROJECTION_START, PROJECTION_END, projection=engine_projection
    )
    assert errors == []


@pytest.mark.parametrize(
    "fund,as_of,period1_end,period1_nav,period1_net_cf",
    [
        (
            make_fund(
                name="Side Pocket",
                funded_amount=60_000_000,
                stated_nav=72_923_860,
                invest_end_date=date(2023, 1, 31),
                reinvest_end_date=date(2023, 1, 31),
                termination_date=date(2025, 12, 31),
            ),
            date(2026, 6, 30),
            date(2026, 7, 31),
            0.0,
            72_923_860,
        ),
        (
            make_fund(
                name="Lag Report",
                funded_amount=40_000_000,
                stated_nav=45_000_000,
                invest_end_date=date(2024, 6, 30),
                reinvest_end_date=date(2025, 6, 30),
                termination_date=date(2025, 12, 15),
            ),
            date(2025, 12, 31),
            date(2026, 1, 31),
            0.0,
            45_000_000,
        ),
    ],
)
def test_immediate_liquidation_next_month(
    fund: Fund, as_of: date, period1_end: date, period1_nav: float, period1_net_cf: float
) -> None:
    df = fund.project(as_of, date(2030, 6, 30))
    assert len(df) == 2
    assert df.loc[df["period"] == 0, "end_date"].iloc[0] == as_of
    assert df.loc[df["period"] == 1, "end_date"].iloc[0] == period1_end
    assert df.loc[df["period"] == 1, "nav"].iloc[0] == pytest.approx(period1_nav)
    assert df.loc[df["period"] == 1, "net_cf"].iloc[0] == pytest.approx(period1_net_cf)
    p1 = df.loc[df["period"] == 1].iloc[0]
    assert p1["roc"] <= 0
    assert p1["gain_on_sale"] <= 0
    assert p1["dividend"] <= 0
    prior_nav = df.loc[df["period"] == 0, "nav"].iloc[0]
    assert p1["roc"] + p1["dividend"] + p1["gain_on_sale"] == pytest.approx(-prior_nav)


def test_stated_nav_floor_emits_warning() -> None:
    with pytest.warns(UserWarning, match="stated_nav"):
        fund = make_fund(stated_nav=-100)
    df = fund.project(date(2025, 12, 31), date(2026, 1, 31))
    assert df.loc[df["period"] == 0, "nav"].iloc[0] == 0.0
