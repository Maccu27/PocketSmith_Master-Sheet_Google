from __future__ import annotations

from typing import Any

# Dezent / klassisch: Anthrazit-Header, hellgrauer Zebra, Pastell-Akzente.
COLOR_HEADER_BG = {"red": 0.20, "green": 0.24, "blue": 0.31}  # anthracite
COLOR_HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}
COLOR_AUTO_BG = {"red": 0.96, "green": 0.96, "blue": 0.97}  # very light grey
COLOR_INPUT_BG = {"red": 1.0, "green": 0.97, "blue": 0.83}  # pale yellow
COLOR_OK_BG = {"red": 0.87, "green": 0.94, "blue": 0.86}  # pale green
COLOR_WARN_BG = {"red": 0.99, "green": 0.89, "blue": 0.78}  # pale orange
COLOR_NOTE_BG = {"red": 1.0, "green": 1.0, "blue": 1.0}  # white
COLOR_ZEBRA = {"red": 0.97, "green": 0.97, "blue": 0.98}


def header_format() -> dict[str, Any]:
    return {
        "backgroundColor": COLOR_HEADER_BG,
        "horizontalAlignment": "LEFT",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {
            "foregroundColor": COLOR_HEADER_FG,
            "fontSize": 10,
            "bold": True,
        },
        "padding": {"top": 4, "bottom": 4, "left": 6, "right": 6},
    }


def cell_format(
    *,
    background: dict[str, float] | None = None,
    bold: bool = False,
    number_format: dict[str, str] | None = None,
    horizontal_alignment: str = "LEFT",
) -> dict[str, Any]:
    fmt: dict[str, Any] = {
        "wrapStrategy": "WRAP",
        "verticalAlignment": "MIDDLE",
        "horizontalAlignment": horizontal_alignment,
        "textFormat": {"fontSize": 10, "bold": bold},
        "padding": {"top": 3, "bottom": 3, "left": 6, "right": 6},
    }
    if background is not None:
        fmt["backgroundColor"] = background
    if number_format is not None:
        fmt["numberFormat"] = number_format
    return fmt


def repeat_cell_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    cell_format_data: dict[str, Any],
    fields: str = "userEnteredFormat",
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {"userEnteredFormat": cell_format_data},
            "fields": fields,
        }
    }


def freeze_rows_request(sheet_id: int, frozen_row_count: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": frozen_row_count},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def auto_resize_columns_request(sheet_id: int, start_col: int, end_col: int) -> dict[str, Any]:
    return {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_col,
                "endIndex": end_col,
            }
        }
    }


def set_column_width_request(sheet_id: int, start_col: int, end_col: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_col,
                "endIndex": end_col,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def add_protected_range_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    description: str,
    warning_only: bool = True,
) -> dict[str, Any]:
    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row,
                    "endRowIndex": end_row,
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col,
                },
                "description": description,
                "warningOnly": warning_only,
            }
        }
    }


def add_data_validation_checkbox_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
) -> dict[str, Any]:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "rule": {
                "condition": {"type": "BOOLEAN"},
                "strict": True,
            },
        }
    }


def add_data_validation_dropdown_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    values: list[str],
) -> dict[str, Any]:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def conditional_format_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    formula: str,
    background: dict[str, float],
    index: int = 0,
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": end_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": formula}],
                    },
                    "format": {"backgroundColor": background},
                },
            },
            "index": index,
        }
    }


def delete_conditional_format_rules_request(sheet_id: int) -> list[dict[str, Any]]:
    """Caller fetches existing rules to know how many to delete; we delete in reverse order."""
    return []
