"""Select and render supported encodings of already accepted analytical results."""

import inspect
from pathlib import Path

import pandas as pd
import plotly.express as px
from pydantic import model_validator

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


class PlotValidationError(ValueError):
    """An encoding can be corrected without changing the accepted data."""


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


def plot_descriptions() -> dict[str, str]:
    """Selection guidance for Analysis, limited to the installed renderer's functions."""
    descriptions = {
        "area": "Show cumulative magnitude and changing composition over an ordered dimension; expects ordered positions, values and optional series.",
        "bar": "Compare category magnitudes or rankings; expects categories and numeric values, optionally grouped or stacked.",
        "bar_polar": "Compare magnitudes around directions or cyclic categories; expects angular positions and radial values.",
        "box": "Compare medians, spread and outliers between groups; expects individual numeric observations and optional categories.",
        "choropleth": "Compare values across geographic regions on a projected map; expects recognised region identifiers and numeric values.",
        "choropleth_map": "Compare regions on a tile map; requires matching boundary geometry, which this renderer cannot currently supply.",
        "density_contour": "Show contours of binned observations across numeric x/y dimensions; does not interpolate an irregular measured surface.",
        "density_heatmap": "Reveal hotspots and bands by binning observations across two dimensions; not an interpolated measurement grid.",
        "density_map": "Reveal geographic concentrations or weighted hotspots; expects latitude/longitude in degrees and an optional intensity measure.",
        "ecdf": "Compare percentiles and distribution tails; turns numeric observations into cumulative proportions, optionally grouped.",
        "funnel": "Compare quantities across ordered process stages to show progression or attrition; expects stage labels and values.",
        "funnel_area": "Show the relative sizes of ordered stages as funnel areas; expects stage labels and numeric values.",
        "histogram": "Explore distribution shape, frequency and tails; numeric observations are automatically binned and counted.",
        "icicle": "Explore hierarchy and relative size with adjacent rectangles; expects hierarchy levels or parent/child identifiers and optional values.",
        "imshow": "Reveal patterns in a numeric matrix or regular raster, such as a value grid or correlation matrix; expects an already-shaped matrix.",
        "line": "Show trends, profiles or transitions over time, distance or another ordered dimension; expects ordered positions, values and optional series.",
        "line_3d": "Show a trajectory or ordered profile through three numeric dimensions; expects ordered x/y/z observations.",
        "line_geo": "Show geographic routes or connections on a projected map; expects ordered latitude/longitude in degrees or recognised locations.",
        "line_map": "Show geographic tracks or routes over a tile map; expects ordered latitude/longitude in degrees and optional groups.",
        "line_polar": "Show directional or cyclic profiles; expects ordered angular positions, radial values and optional series.",
        "line_ternary": "Show an ordered path through three-component compositions; expects three component values per observation in sequence.",
        "parallel_categories": "Explore associations and combinations across several categorical dimensions; expects categorical columns for individual observations.",
        "parallel_coordinates": "Explore multivariate profiles, clusters and unusual combinations; expects several numeric dimensions per observation.",
        "pie": "Show parts of a whole, especially with a few categories; expects category labels and non-negative component values.",
        "scatter": "Explore relationships, clusters and individual outliers between two numeric variables, including planar spatial coordinates; expects paired observations.",
        "scatter_3d": "Explore relationships or spatial structure across three numeric dimensions; expects x/y/z observations with optional colour or size measures.",
        "scatter_geo": "Show locations and geographic variation on a projected map; expects latitude/longitude in degrees or recognised geographic locations.",
        "scatter_map": "Show locations and local geographic patterns on a tile map; expects latitude/longitude in degrees and optional colour or size measures.",
        "scatter_matrix": "Explore pairwise relationships and outliers across several numeric variables; expects multiple numeric columns per observation.",
        "scatter_polar": "Explore relationships between direction or cyclic position and magnitude; expects angular positions and radial values.",
        "scatter_ternary": "Explore balance, clusters and outliers among three components; expects three non-negative component values per observation.",
        "strip": "Compare individual observations across categories while retaining clusters and outliers; expects categories and numeric observations.",
        "sunburst": "Explore hierarchical composition from root to leaves using concentric rings; expects hierarchy levels or parent/child identifiers and optional values.",
        "timeline": "Compare event durations, overlaps and sequences; expects start/end dates and optional entity or category labels.",
        "treemap": "Explore hierarchical composition using nested rectangle areas; expects hierarchy levels or parent/child identifiers and optional values.",
        "violin": "Compare distribution shapes, modes and tails across groups; expects individual numeric observations and optional categories.",
    }
    return {
        name: descriptions.get(name) or " ".join((inspect.getdoc(getattr(px, name)) or "").split("\n\n", 1)[0].split())
        for name in plot_capabilities()
    }


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


def accepted_result_plot_schema(
    result: AcceptedAnalysisResult,
    selection: list[ResultFilter] | None = None,
    *,
    frame: pd.DataFrame,
    title: str = "",
) -> type[PlotSpec]:
    """Return contract and rendering errors to the existing SDK correction loop."""
    exact_selection = [item.model_copy(deep=True) for item in (selection or [])]

    class AcceptedResultPlotSpec(PlotSpec):
        @model_validator(mode="after")
        def accepted_result_contract(self):
            validate_plot_candidate(frame, self, result, exact_selection, title)
            return self

    return AcceptedResultPlotSpec


def validate_plot_candidate(frame: pd.DataFrame, plot: PlotSpec, result: AcceptedAnalysisResult,
                            selection: list[ResultFilter], title: str):
    """Use the real renderer, retaining concise errors for model correction."""
    try:
        validate_plot_spec(plot, result, selection)
        return render_plot(frame, plot, title)
    except (ValueError, TypeError, KeyError, NotImplementedError) as exc:
        detail = " ".join(str(exc).split())[:350]
        hint = "Choose a simpler compatible encoding using the same accepted data and selection."
        raise PlotValidationError(f"{plot.function}: {detail}. {hint}") from exc


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
