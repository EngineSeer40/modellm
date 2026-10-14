"""Display formatting only; saved results and numeric values remain unchanged."""

import re
from numbers import Real

import pandas as pd


def concise_numbers(text):
    return re.sub(r"(?<![\w.])(-?\d[\d,]*\.\d{3,})(?![\w.])",
                  lambda m: f"{float(m[0].replace(',', '')):,.2f}", str(text))


def finding_bullets(text):
    # Preserve the recorded paragraphs; do not invent a new summary.
    items = re.split(r"\n\s*\n|\n(?=\s*[-*•]\s)", text.strip())
    return [concise_numbers(re.sub(r"^\s*[-*•]\s+", "", item.strip()))
            for item in items if item.strip()][:10]


def display_table(rows):
    table = pd.DataFrame(rows)
    return table.style.format(precision=2, na_rep="—")


def format_chart(figure):
    figure.update_layout(title_text=None)
    def limit_format(value, default=".2f"):
        if not value:
            return default
        return re.sub(r"\.\d+", ".2", value)
    def display_text(value):
        if isinstance(value, str):
            return concise_numbers(value)
        if hasattr(value, "__iter__"):
            return [display_text(v) for v in value]
        return concise_numbers(value) if isinstance(value, Real) else value
    for annotation in figure.layout.annotations or []:
        if annotation.text:
            annotation.text = concise_numbers(annotation.text)
    numeric_axes = set()
    for trace in figure.data:
        values = trace.to_plotly_json()
        def numeric(field):
            match = re.fullmatch(r"customdata\[(\d+)\]", field)
            data = values.get("customdata") if match else values.get("values" if field == "value" else field)
            if data is None:
                return False
            try:
                samples = [row[int(match[1])] for row in data] if match else ([data] if isinstance(data, Real) else data)
                return any(isinstance(v, Real) and not isinstance(v, bool) for v in samples)
            except (TypeError, IndexError):
                return False
        for attribute in ("hovertemplate", "texttemplate"):
            template = values.get(attribute)
            if isinstance(template, str):
                def replacement(match):
                    field, fmt = match[1], match[2]
                    if field == "percent":
                        return "%{percent:" + limit_format(fmt, ".2%") + "}"
                    return "%{" + field + ":" + limit_format(fmt) + "}" if numeric(field) else match[0]
                setattr(trace, attribute, re.sub(r"%\{([^}:]+)(?::([^}]+))?\}", replacement, template))
        for attribute in ("text", "hovertext"):
            if values.get(attribute) is not None:
                setattr(trace, attribute, display_text(values[attribute]))
        if "colorbar" in trace._valid_props:
            trace.colorbar.tickformat = limit_format(trace.colorbar.tickformat, ".2~f")
        if "marker" in trace._valid_props and "colorbar" in trace.marker._valid_props:
            trace.marker.colorbar.tickformat = limit_format(trace.marker.colorbar.tickformat, ".2~f")
        for axis in ("x", "y", "z"):
            if numeric(axis):
                numeric_axes.add(values.get(axis + "axis", axis))
            if numeric(axis) and axis + "hoverformat" in trace._valid_props:
                setattr(trace, axis + "hoverformat", limit_format(values.get(axis + "hoverformat")))
        if "cells" in values:  # Plotly table charts.
            trace.cells.format = [".2f" if any(isinstance(v, Real) for v in column) else ""
                                  for column in values["cells"].get("values", [])]
    for reference in numeric_axes:
        axis_name = reference[0] + "axis" + reference[1:]
        if axis_name in figure.layout:
            axis = figure.layout[axis_name]
            if axis.type not in {"date", "category"}:
                axis.update(tickformat=limit_format(axis.tickformat, ".2~f"), hoverformat=limit_format(axis.hoverformat))
    if figure.layout.coloraxis:
        figure.layout.coloraxis.colorbar.tickformat = ".2~f"
    return figure
