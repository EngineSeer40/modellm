"""Select and render supported encodings of already accepted analytical results."""

import inspect
from pathlib import Path

import pandas as pd
import plotly.express as px

from .core.contracts import AcceptedAnalysisResult, AnalysisSpec, PlotSpec, ResultFilter, validate_plot_template
from .database import connect


PLOTLY_COLUMN_ARGUMENTS = {
    "x",
    "y",
    "z",
    "color",
    "size",
    "symbol",
    "text",
    "facet_row",
    "facet_col",
    "hover_name",
    "hover_data",
    "names",
    "values",
    "path",
    "ids",
    "parents",
    "lat",
    "lon",
    "locations",
    "animation_frame",
    "animation_group",
    "dimensions",
    "custom_data",
    "line_group",
    "line_dash",
    "pattern_shape",
    "base",
    "error_x",
    "error_x_minus",
    "error_y",
    "error_y_minus",
    "error_z",
    "error_z_minus",
    "a",
    "b",
    "c",
    "r",
    "theta",
    "x_start",
    "x_end",
}


PLOTLY_PRESENTATION_ARGUMENTS = {
    "orientation", "barmode", "line_shape", "markers", "points", "opacity",
    "log_x", "log_y", "width", "height", "template", "text_auto",
}


def plot_capabilities() -> dict:
    """Tell the agent which arguments this renderer actually passes to Plotly."""
    capabilities = {}
    for name in dir(px):
        function = getattr(px, name)
        if not callable(function) or name.startswith("_"):
            continue
        parameters = inspect.signature(function).parameters
        if "title" not in parameters or ("data_frame" not in parameters and name != "imshow"):
            continue
        capabilities[name] = {
            "columns": sorted(PLOTLY_COLUMN_ARGUMENTS & parameters.keys()) if name != "imshow" else [],
            "options": sorted(PLOTLY_PRESENTATION_ARGUMENTS & parameters.keys()),
        }
    return capabilities


def validate_plot_arguments(plot: PlotSpec) -> None:
    supported = plot_capabilities().get(plot.function)
    if supported is None:
        raise ValueError(f"Unsupported Plotly Express function: {plot.function}")
    for argument in plot.columns:
        if argument not in supported["columns"]:
            hint = "; place presentation settings in options" if argument in supported["options"] else ""
            raise ValueError(f"Unsupported plot column argument for {plot.function}: {argument}{hint}")
    for argument in plot.options:
        if argument not in supported["options"]:
            raise ValueError(f"Unsupported plot presentation option for {plot.function}: {argument}")
    validate_plot_template(plot.options)


def representation_selections(database: Path, spec: AnalysisSpec, result: AcceptedAnalysisResult):
    """Present every representation Analysis supplied; never compute a new grain."""
    if not spec.variant_field:
        return [(None, [])]
    field = '"' + spec.variant_field.replace('"', '""') + '"'
    with connect(database, read_only=True) as con:
        values = con.execute(f"SELECT DISTINCT {field} FROM {result.relation} ORDER BY {field}").fetchall()
    selections = []
    for (value,) in values:
        if value is None:
            selections.append(("Unspecified representation", [ResultFilter(field=spec.variant_field, operator="is_null", values=[])]))
        else:
            scalar = value if isinstance(value, (str, int, float, bool)) else str(value)
            selections.append((str(value), [ResultFilter(field=spec.variant_field, operator="eq", values=[scalar])]))
    return selections


def validate_plot_spec(plot: PlotSpec, result: AcceptedAnalysisResult,
                       selection: list[ResultFilter] | None = None) -> None:
    validate_plot_arguments(plot)
    if plot.filters != (selection or []):
        raise ValueError("Plot filters must equal the supplied presentation selection; no additional population filters")
    available = set(result.columns)
    for argument, value in plot.columns.items():
        names = value if isinstance(value, list) else [value]
        if not names or any(name not in available for name in names):
            raise ValueError(f"Plot references a missing accepted-result column: {argument}={value}")
    for selection in plot.filters:
        if selection.field not in available:
            raise ValueError(f"Plot filter references missing accepted-result column: {selection.field}")
        if selection.operator == "eq" and len(selection.values) != 1:
            raise ValueError("Plot eq filter needs one value")
        if selection.operator == "in" and not selection.values:
            raise ValueError("Plot in filter needs values")
        if selection.operator == "is_null" and selection.values:
            raise ValueError("Plot is_null filter takes no values")


def filter_frame(frame: pd.DataFrame, filters: list[ResultFilter]) -> pd.DataFrame:
    for selection in filters:
        if selection.field not in frame.columns:
            raise ValueError(f"Plot filter references missing result column: {selection.field}")
        series = frame[selection.field]
        if selection.operator == "eq":
            if len(selection.values) != 1:
                raise ValueError("Plot eq filter needs one value")
            frame = frame.loc[series == selection.values[0]]
        elif selection.operator == "in":
            if not selection.values:
                raise ValueError("Plot in filter needs values")
            frame = frame.loc[series.isin(selection.values)]
        else:
            if selection.values:
                raise ValueError("Plot is_null filter takes no values")
            frame = frame.loc[series.isna()]
    return frame


def render_plot(frame: pd.DataFrame, plot: PlotSpec, title: str):
    """Use only the executed DataFrame as Plotly's data source."""
    validate_plot_arguments(plot)
    frame = filter_frame(frame, plot.filters)
    if plot.function == "imshow":
        if plot.columns:
            raise ValueError("imshow uses the full DataFrame without column arguments")
        return px.imshow(frame, title=title, **plot.options)
    function = getattr(px, plot.function, None)
    if not callable(function):
        raise ValueError(f"Unknown Plotly Express function: {plot.function}")
    parameters = inspect.signature(function).parameters
    if "data_frame" not in parameters or "title" not in parameters:
        raise ValueError(f"Unsupported Plotly Express function: {plot.function}")
    for argument, value in plot.columns.items():
        if argument not in PLOTLY_COLUMN_ARGUMENTS or argument not in parameters:
            raise ValueError(f"Unsupported plot column argument: {argument}")
        names = value if isinstance(value, list) else [value]
        if not names or any(name not in frame.columns for name in names):
            raise ValueError(
                f"Plot references a missing result column: {argument}={value}"
            )
    return function(frame, title=title, **plot.columns, **plot.options)
