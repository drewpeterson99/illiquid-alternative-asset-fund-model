"""Small helpers for building test funds."""

from __future__ import annotations

from datetime import date

from fund_engine import DEFAULT_AS_OF_DATE, Fund


def fund_kwargs(**overrides: object) -> dict:
    """Keyword args for Fund / validation (override any field)."""
    data: dict = {
        "name": "Test Fund",
        "commitment": 100_000_000.0,
        "funded_amount": 50_000_000.0,
        "stated_nav": 60_000_000.0,
        "unfunded": None,
        "initial_commitment_month": DEFAULT_AS_OF_DATE,
        "invest_end_date": date(2028, 6, 30),
        "reinvest_end_date": date(2029, 6, 30),
        "termination_date": date(2031, 6, 30),
        "annual_return": 0.12,
        "dist_rate": 0.05,
        "pct_drawn": 0.85,
        "mgmt_fee": 0.01,
        "paid_on_committed": False,
        "carry_rate": 0.15,
        "carry_hurdle": 0.08,
        "currency": "USD",
    }
    data.update(overrides)
    return data


def make_fund(**overrides: object) -> Fund:
    return Fund(**fund_kwargs(**overrides))
