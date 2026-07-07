from __future__ import annotations

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
