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

Build a cross-channel customer and commercial performance model that reconciles customer behaviour, store performance and finance invoicing.
Use the operational transaction sources for purchase behaviour and the invoice source as a separate finance fact. Do not double-count invoices as additional purchases simply because they overlap with operational orders.

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

3. Cross-channel customer mix

Classify identified customers into a mutually exclusive current-period behaviour group such as:

online only;
store only;
omnichannel;
no qualifying purchase in the recent period.
Use a 180-day recent-period window ending 2026-09-30.

Return customer counts and share-of-known-customer population. I want to see as many of these measures together in a series of sub plots.

"""
