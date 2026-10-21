SAMPLE_CHURN_FILES = (
    "customer_records.csv",
    "store_locations.csv",
    "transactions_instore.csv",
    "transactions_online.csv",
    "invoices.json",
)

SAMPLE_SURVEY_FILES = (
    "survey_stations.csv",
    "field_measurements.csv",
    "sample_registry.csv",
    "lab_assays.csv",
)

SAMPLE_SURVEY = """

Build a dimensional analytical model that connects survey stations, repeated field geophysics campaigns, collected samples, and laboratory assays at their correct grains.

Use the model to produce exactly three Plotly charts:

- Multiple-line chart: for traverse TR20, plot magnetic susceptibility against station chainage, with one line per survey campaign. Exclude invalid or null susceptibility readings and preserve station lithology as an available analytical dimension.

- Elevation contour plot: create a contour plot of ground elevation using station easting and northing. Exclude stations with missing elevation, the 9999 elevation sentinel, and clearly invalid GPS positions or BAD_FIX quality. The contour must be based on the station-level elevation surface rather than on field-measurement rows.

- 2D Bubble plot: show plots of valid copper (CU) and zinc (ZN) along lat and long, Use bubble size to denote assay ppm concentrations. Exclude failed/null assay results and handle QC duplicate rows so they do not double-count samples.

"""

SAMPLE_CHURN = """

Produce at least the following analytical concepts:

Treat someone as churned if they have made at least two completed purchases across either online or store sales, then go 180 days without another completed purchase. 

1. Store performance map

For each active store, show:

net in-store revenue;
purchase-event count;
average basket value;
return rate;
share of identifiable customers who also purchased online during the available period.
Preserve store latitude/longitude and region so the plotting stage can produce a geographic map. A proportional-symbol map is appropriate if supported, but the analysis stage should only provide the correct measures and dimensions.

2. Customer churn heatmap
Show churn rate across age group × current subscription level. Preserve gender if it remains analytically useful, but do not create separate charts purely because gender exists. Show me this in a color coded heat map.

3. Customer churn across time

I also want to understand customer churn each month and whether promotions are actually helping across our online and store sales.
Show the monthly churn across time for online and offline customers and show it on a line graph.

"""
