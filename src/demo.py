from pathlib import Path

import pandas as pd

from .core.contracts import (
    AnalyticalPlan,
    BusinessProcessPlan,
    DimensionalModelPlan,
    DimensionPlan,
    FactMeasure,
    FactPlan,
)

REQUEST = "Show me how customer value and sales performance are changing over time, and which products are driving revenue."


def write_demo(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "customers": [
            {
                "customer_id": 1,
                "customer_name": "Acme",
                "segment": "Business",
                "region": "East",
            },
            {
                "customer_id": 2,
                "customer_name": "Beta",
                "segment": "Consumer",
                "region": "West",
            },
            {
                "customer_id": 3,
                "customer_name": "Cedar",
                "segment": None,
                "region": "East",
            },
            {
                "customer_id": 4,
                "customer_name": "Acme",
                "segment": "Business",
                "region": "North",
            },
        ],
        "products": [
            {"product_id": 10, "product_name": "Notebook", "category": "Stationery"},
            {"product_id": 20, "product_name": "Monitor", "category": "Electronics"},
            {"product_id": 30, "product_name": "Mouse", "category": None},
        ],
        "orders": [
            {
                "order_id": 100,
                "customer_id": 1,
                "order_date": "2025-01-10",
                "status": "completed",
            },
            {
                "order_id": 101,
                "customer_id": 2,
                "order_date": "2025-02-11",
                "status": "completed",
            },
            {
                "order_id": 102,
                "customer_id": 1,
                "order_date": "2025-03-02",
                "status": "completed",
            },
            {
                "order_id": 103,
                "customer_id": 999,
                "order_date": "2025-03-14",
                "status": "completed",
            },
            {
                "order_id": 104,
                "customer_id": 3,
                "order_date": "2025-04-09",
                "status": "cancelled",
            },
        ],
        "order_items": [
            {
                "order_item_id": 1,
                "order_id": 100,
                "product_id": 10,
                "quantity": 2,
                "unit_price": 5.0,
            },
            {
                "order_item_id": 2,
                "order_id": 100,
                "product_id": 20,
                "quantity": 1,
                "unit_price": 150.0,
            },
            {
                "order_item_id": 3,
                "order_id": 101,
                "product_id": 20,
                "quantity": 2,
                "unit_price": 145.0,
            },
            {
                "order_item_id": 4,
                "order_id": 102,
                "product_id": 30,
                "quantity": 3,
                "unit_price": 20.0,
            },
            {
                "order_item_id": 5,
                "order_id": 103,
                "product_id": 10,
                "quantity": 1,
                "unit_price": 5.0,
            },
            {
                "order_item_id": 6,
                "order_id": 104,
                "product_id": 10,
                "quantity": 1,
                "unit_price": 5.0,
            },
            {
                "order_item_id": 7,
                "order_id": 999,
                "product_id": 10,
                "quantity": 1,
                "unit_price": 5.0,
            },
            {
                "order_item_id": 8,
                "order_id": 102,
                "product_id": 999,
                "quantity": 1,
                "unit_price": 10.0,
            },
        ],
    }
    paths = []
    for name, rows in data.items():
        path = directory / f"{name}.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        paths.append(path)
    return paths


def demo_plan() -> AnalyticalPlan:
    return AnalyticalPlan(
        objective="Measure completed order item revenue at its atomic grain by month, customer and product.",
        business_questions=[
            "How is revenue changing by month?",
            "Which customers generate most revenue?",
            "Which products generate most revenue?",
        ],
        assumptions=[
            "Only completed orders contribute to sales revenue.",
            "Line revenue is quantity multiplied by recorded unit price; refunds, tax and discounts are unavailable.",
            "Rows with missing order or product references are excluded from the sales fact; an unknown customer is retained.",
        ],
        unresolved_questions=[
            "Does cancelled mean all sales should be excluded?",
            "Are prices inclusive of taxes or discounts?",
        ],
        target_model=DimensionalModelPlan(
            business_processes=[
                BusinessProcessPlan(
                    name="order_sales",
                    description="Completed order item sales",
                    source_datasets=["orders", "order_items"],
                )
            ],
            dimensions=[
                DimensionPlan(
                    name="dim_customer",
                    business_entity="customer",
                    grain="One row per customer_id",
                    source_datasets=["customers"],
                    natural_key="customer_id",
                    surrogate_key="customer_key",
                    attributes={
                        "customer_name": "customer_name",
                        "segment": "segment",
                        "region": "region",
                    },
                ),
                DimensionPlan(
                    name="dim_product",
                    business_entity="product",
                    grain="One row per product_id",
                    source_datasets=["products"],
                    natural_key="product_id",
                    surrogate_key="product_key",
                    attributes={"product_name": "product_name", "category": "category"},
                ),
            ],
            facts=[
                FactPlan(
                    name="fact_order_items",
                    business_process="order_sales",
                    fact_type="transaction",
                    grain="One completed order item identified by order_item_id",
                    source_datasets=["orders", "order_items"],
                    dimension_keys={
                        "customer_key": "orders.customer_id -> customers.customer_id",
                        "product_key": "order_items.product_id -> products.product_id",
                    },
                    measures=[
                        FactMeasure(
                            name="revenue",
                            source_expression="order_items.quantity * order_items.unit_price",
                            additivity="additive",
                        ),
                        FactMeasure(
                            name="quantity",
                            source_expression="order_items.quantity",
                            additivity="additive",
                        ),
                    ],
                )
            ],
        ),
    )
