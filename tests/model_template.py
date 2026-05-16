"""
ModelTemplate.xlsx helpers — for regression tests only.

Loads the Example Fund assumptions and golden time-series from the template workbook.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from fund_engine import Fund, month_end

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "ModelTemplate.xlsx"
SHEET_NAME = "Model"

ASSUMPTION_FIELD_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("name", ("Investment Name",)),
    ("initial_commitment_month", ("Initial Commitment Month",)),
    ("invest_end_date", ("Investment End Date",)),
    ("reinvest_end_date", ("Reinvestment End Date",)),
    ("termination_date", ("Termination Date",)),
    ("commitment", ("Total Commitment",)),
    ("funded_amount", ("Funded Amount",)),
    ("stated_nav", ("Stated NAV",)),
    (
        "annual_return",
        ("Annual Model Return", "Ann. Asset Class Return (Conservative)"),
    ),
    ("dist_rate", ("Annual Fund Distribution Target",)),
    ("pct_drawn", ("% of Capital Drawn",)),
    ("mgmt_fee", ("Mgmt Fee",)),
    ("paid_on_committed", ("Paid on Committed?", "Paid on Committed")),
    ("carry_rate", ("Carry",)),
    ("carry_hurdle", ("Carry Hurdle",)),
    ("currency", ("Currency",)),
]

PROJECTION_START = date(2025, 12, 31)
PROJECTION_END = date(2030, 6, 30)

EXCEL_ROW_TO_COLUMN: dict[int, str] = {
    10: "total_commitment",
    11: "effective_commitment",
    12: "remaining_effective_unfunded",
    13: "legal_unfunded",
    14: "beginning_capital_account",
    15: "capital_called",
    16: "roc",
    17: "ending_capital",
    20: "beginning_unrealized_gl",
    21: "period_gl",
    22: "gain_on_sale",
    23: "ending_unrealized_gl",
    25: "nav",
    27: "asset_income",
    28: "mgmt_fee_amt",
    29: "pre_carry_income",
    30: "carry_amt",
    32: "dividend",
    33: "retained_income",
    35: "net_cf",
}

_FIRST_PERIOD_COL = 21  # 1-based Excel column U
_PERIOD_HEADER_ROW = 5
_START_DATE_ROW = 6
_END_DATE_ROW = 7
_LABEL_COL = 3  # 0-based column D
_VALUE_COL = 4  # 0-based column E


def _normalize_label(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().casefold()


def _to_date(value: object) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


@lru_cache(maxsize=2)
def _load_model_sheet(path_str: str) -> pd.DataFrame:
    """Read the Model sheet once per path (cached for the test session)."""
    return pd.read_excel(path_str, sheet_name=SHEET_NAME, header=None)


def _fund_from_sheet(sheet: pd.DataFrame) -> Fund:
    label_to_field: dict[str, str] = {}
    for field_name, matchers in ASSUMPTION_FIELD_MAP:
        for matcher in matchers:
            label_to_field[_normalize_label(matcher)] = field_name

    values: dict[str, Any] = {}
    for row_idx in range(sheet.shape[0]):
        label = _normalize_label(sheet.iloc[row_idx, _LABEL_COL])
        if label not in label_to_field:
            continue
        field_name = label_to_field[label]
        raw = sheet.iloc[row_idx, _VALUE_COL]
        if field_name == "paid_on_committed":
            values[field_name] = bool(raw)
        elif field_name in {
            "initial_commitment_month",
            "invest_end_date",
            "reinvest_end_date",
            "termination_date",
        }:
            parsed = _to_date(raw)
            if parsed is None:
                raise ValueError(f"Missing date for {field_name!r}")
            values[field_name] = parsed
        elif field_name == "name":
            values[field_name] = str(raw) if not pd.isna(raw) else ""
        elif field_name == "currency":
            values[field_name] = str(raw) if not pd.isna(raw) else ""
        else:
            values[field_name] = float(raw)

    missing = {f for f, _ in ASSUMPTION_FIELD_MAP} - set(values)
    if missing:
        raise ValueError(f"Missing assumptions in template: {sorted(missing)}")

    return Fund(unfunded=None, **values)


def _golden_from_sheet(sheet: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    col = _FIRST_PERIOD_COL - 1
    while col < sheet.shape[1]:
        period = sheet.iloc[_PERIOD_HEADER_ROW - 1, col]
        if pd.isna(period):
            break
        try:
            period_num = int(period)
        except (TypeError, ValueError):
            break

        start_raw = sheet.iloc[_START_DATE_ROW - 1, col]
        if pd.isna(start_raw):
            break

        start_parsed = _to_date(start_raw)
        record: dict[str, Any] = {
            "period": period_num,
            "start_date": start_parsed.replace(day=1) if start_parsed else None,
            "end_date": month_end(_to_date(sheet.iloc[_END_DATE_ROW - 1, col])),
        }
        for excel_row, column_name in EXCEL_ROW_TO_COLUMN.items():
            value = sheet.iloc[excel_row - 1, col]
            if value == "n/a" or (isinstance(value, float) and pd.isna(value)):
                record[column_name] = float("nan")
            else:
                record[column_name] = float(value)
        records.append(record)
        col += 1

    return pd.DataFrame(records)


def load_model_template_data(
    filepath: Path | None = None,
) -> tuple[Fund, pd.DataFrame]:
    """Load fund assumptions and golden time series in a single Excel read."""
    path = filepath or TEMPLATE_PATH
    sheet = _load_model_sheet(str(path))
    return _fund_from_sheet(sheet), _golden_from_sheet(sheet)


def load_fund_from_model_template(filepath: Path | None = None) -> Fund:
    return load_model_template_data(filepath)[0]


def load_excel_golden_projection(filepath: Path | None = None) -> pd.DataFrame:
    return load_model_template_data(filepath)[1]
