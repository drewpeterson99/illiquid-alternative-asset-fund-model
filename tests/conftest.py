"""Shared pytest fixtures and progress reporting."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fund_engine import Fund, load_funds_from_assumptions
from tests.model_template import (
    PROJECTION_END,
    PROJECTION_START,
    load_model_template_data,
)
from tests.progress import progress

ASSUMPTIONS_PATH = (
    Path(__file__).resolve().parents[1] / "inputs" / "FundAssumptions.xlsx"
)


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    progress(f">> {nodeid}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when != "call":
        return
    symbol = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}.get(
        report.outcome, "????"
    )
    duration = f" ({report.duration:.1f}s)" if report.duration else ""
    progress(f"   [{symbol}] {report.nodeid}{duration}")


@pytest.fixture(scope="module")
def model_template_data() -> tuple[Fund, pd.DataFrame]:
    progress("Loading ModelTemplate.xlsx (fund + golden series)...")
    fund, golden = load_model_template_data()
    progress(
        f"  Loaded {fund.name!r}, {len(golden)} golden periods "
        f"(0-{int(golden['period'].max())})"
    )
    return fund, golden


@pytest.fixture(scope="module")
def template_fund(model_template_data: tuple[Fund, pd.DataFrame]) -> Fund:
    return model_template_data[0]


@pytest.fixture(scope="module")
def excel_golden(model_template_data: tuple[Fund, pd.DataFrame]) -> pd.DataFrame:
    return model_template_data[1]


@pytest.fixture(scope="module")
def engine_projection(template_fund: Fund) -> pd.DataFrame:
    progress(f"Running engine projection ({PROJECTION_START} -> {PROJECTION_END})...")
    projection = template_fund.project(PROJECTION_START, PROJECTION_END)
    progress(f"  Projection complete: {len(projection)} periods")
    return projection


@pytest.fixture(scope="module")
def assumptions_funds() -> list[Fund]:
    progress(f"Loading funds from {ASSUMPTIONS_PATH.name}...")
    funds = load_funds_from_assumptions(ASSUMPTIONS_PATH)
    progress(f"  Loaded {len(funds)} funds")
    return funds
