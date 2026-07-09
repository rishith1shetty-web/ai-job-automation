from __future__ import annotations

import json

import pandas as pd


def build_unique_display_frame(
    frame: pd.DataFrame,
    desired_columns: list[str],
    rename_map: dict[str, str],
) -> pd.DataFrame:
    """Select and rename dashboard columns without producing duplicate labels."""
    selected: list[str] = []
    seen_sources: set[str] = set()
    seen_display_names: set[str] = set()
    output_names: dict[str, str] = {}

    for source in desired_columns:
        if source not in frame.columns or source in seen_sources:
            continue
        display_name = rename_map.get(source, source)
        if display_name in seen_display_names:
            suffix = 2
            unique_name = f"{display_name} ({suffix})"
            while unique_name in seen_display_names:
                suffix += 1
                unique_name = f"{display_name} ({suffix})"
            display_name = unique_name

        selected.append(source)
        seen_sources.add(source)
        seen_display_names.add(display_name)
        output_names[source] = display_name

    if not selected:
        return pd.DataFrame()
    return frame[selected].rename(columns=output_names).copy()


def safe_dataframe_for_streamlit(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize mixed object columns so Streamlit/PyArrow can render them."""
    if frame.empty:
        return frame
    safe = frame.copy()
    for column in safe.columns:
        if safe[column].dtype != "object":
            continue
        if safe[column].map(_needs_object_serialization).any():
            safe[column] = safe[column].map(_stringify_dashboard_value)
    return safe


def _needs_object_serialization(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return isinstance(value, (dict, list, tuple, set))


def _stringify_dashboard_value(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, set):
        value = sorted(value, key=str)
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)
