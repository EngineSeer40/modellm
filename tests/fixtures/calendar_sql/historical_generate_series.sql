-- Generate a date spine covering the full range of transaction and invoice dates
with date_bounds as (
    select
        least(
            min(transaction_ts::date),
            (select min(transaction_ts::date) from {{ source('raw', 'transactions_online') }}),
            (select min(invoice_date) from {{ source('raw', 'invoices') }})
        ) as min_date,
        greatest(
            max(transaction_ts::date),
            (select max(transaction_ts::date) from {{ source('raw', 'transactions_online') }}),
            (select max(invoice_date) from {{ source('raw', 'invoices') }})
        ) as max_date
    from {{ source('raw', 'transactions_instore') }}
),

spine as (
    select
        unnest(
            generate_series(
                (select min_date from date_bounds),
                (select max_date from date_bounds),
                interval '1 day'
            )
        )::date as date_day
)

select
    date_day,
    year(date_day)                                          as year_number,
    month(date_day)                                         as month_number,
    strftime(date_day, '%Y-%m')                             as year_month,
    quarter(date_day)                                       as quarter_number,
    dayname(date_day)                                       as day_of_week
from spine
