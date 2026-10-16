import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import pytest

from src.ui.presentation import format_chart


@pytest.mark.parametrize("faceted", [False, True])
def test_saved_categorical_bars_do_not_gain_colour_scales(faceted):
    frame = pd.DataFrame({
        "channel": ["Online", "Store", "Online", "Store"],
        "subscription": ["Basic", "Basic", "Plus", "Plus"],
        "customers": [118, 15, 81, 8],
    })
    figure = px.bar(frame, x="channel", y="customers", color="subscription",
                    barmode="group", facet_col="subscription" if faceted else None)
    figure.update_yaxes(range=[0, 125])
    # Exercise the same Plotly JSON round trip as saved dashboards.
    figure = pio.from_json(figure.to_json())
    before = figure.to_plotly_json()

    format_chart(figure)

    after = figure.to_plotly_json()
    assert "coloraxis" not in after["layout"]
    assert after["layout"]["barmode"] == "group"
    for original, formatted in zip(before["data"], after["data"]):
        assert formatted["marker"] == original["marker"]
        assert formatted["x"] == original["x"]
        assert formatted["y"] == original["y"]
    for name, axis in before["layout"].items():
        if name.startswith(("xaxis", "yaxis")):
            assert after["layout"][name].get("range") == axis.get("range")


@pytest.mark.parametrize("kind", ["heatmap", "map"])
def test_shared_continuous_colour_scales_keep_their_data_and_formatting(kind):
    if kind == "heatmap":
        figure = px.imshow([[0.1234, 0.5678]])
    else:
        figure = px.scatter_map(pd.DataFrame({
            "latitude": [-33.86, -37.81], "longitude": [151.20, 144.96],
            "share": [0.1234, 0.5678],
        }), lat="latitude", lon="longitude", color="share")
    figure.update_coloraxes(cmin=0, cmax=1, colorbar_tickformat=".4%")
    figure = pio.from_json(figure.to_json())
    before = figure.to_plotly_json()

    format_chart(figure)

    after = figure.to_plotly_json()
    assert after["data"] == before["data"]
    axis = after["layout"]["coloraxis"]
    assert axis["cmin"] == 0 and axis["cmax"] == 1
    assert axis["colorscale"] == before["layout"]["coloraxis"]["colorscale"]
    assert axis["colorbar"]["tickformat"] == ".2%"


def test_configured_trace_and_marker_colourbars_keep_their_scale_settings():
    figure = go.Figure([
        go.Heatmap(z=[[0.1234, 0.5678]], colorbar={"tickformat": ".4f"}),
        go.Scatter(x=[1, 2], y=[3, 4], marker={
            "color": [0.1234, 0.5678], "showscale": True,
            "cmin": 0, "cmax": 1, "colorbar": {"tickformat": ".4%"},
        }),
    ])

    format_chart(figure)

    assert figure.data[0].colorbar.tickformat == ".2f"
    marker = figure.data[1].marker
    assert marker.colorbar.tickformat == ".2%"
    assert marker.showscale is True
    assert marker.cmin == 0 and marker.cmax == 1
    assert "coloraxis" not in figure.layout.to_plotly_json()
