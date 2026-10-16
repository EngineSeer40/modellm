SAMPLE_1_FILES = (
    "customer_records.csv",
    "store_locations.csv",
    "transactions_instore.csv",
    "transactions_online.csv",
    "invoices.json",
)

SAMPLE_0 = """

Show how completed online sales revenue changes by month across subscription levels, while retaining customer gender as an available analytical dimension. I want to see where revenue is concentrated and whether the mix changes over time.
Use only identified customers for demographic breakdowns; do not force-match guest orders.
Treat refunded line value as negative net sales where the source supports it, and exclude cancelled orders.
Prefer one coherent visualisation rather than splitting each dimension into a separate chart.

"""

SAMPLE_1 = """

Produce at least the following analytical concepts:

1. Store performance map

For each active store, show:

net in-store revenue;
purchase-event count;
average basket value;
return rate;
share of identifiable customers who also purchased online during the available period.
Preserve store latitude/longitude and region so the plotting stage can produce a geographic map. A proportional-symbol map is appropriate if supported, but the analysis stage should only provide the correct measures and dimensions.

2. Customer churn heatmap

Using the Level 2 churn definition, show churn rate across age group × current subscription level. Preserve gender if it remains analytically useful, but do not create separate charts purely because gender exists. Show me this in a color coded heat map.

3. Customer churn across time

I want to understand customer churn each month and whether promotions are actually helping across our online and store sales.
Treat someone as churned if they have made at least two completed purchases across either online or store sales, then go 60 days without another completed purchase. 
Calculate the monthly churn across time for online and offline customers and show it on a line graph.

"""
