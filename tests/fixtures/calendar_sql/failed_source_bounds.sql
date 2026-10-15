with date_bounds as (
    select
        min(cast(transaction_ts as date)) as min_date,
        max(cast(transaction_ts as date)) as max_date
    from {{ source('raw', 'transactions_online') }}

    union all

    select
        min(cast(transaction_ts as date)) as min_date,
        max(cast(transaction_ts as date)) as max_date
    from {{ source('raw', 'transactions_instore') }}
),

bounds as (
    select
        min(min_date) as spine_start,
        max(max_date) as spine_end
    from date_bounds
),

spine as (
    select
        cast(generate_series as date) as date_day
    from
        bounds,
        generate_series(
            cast(spine_start as timestamp),
            cast(spine_end   as timestamp),
            interval '1 day'
        )
)

select
    date_day,
    strftime(date_day, '%A')                                          as day_of_week_name,
    extract('day'     from date_day)::integer                         as day_of_month,
    extract('month'   from date_day)::integer                         as month_number,
    strftime(date_day, '%B')                                          as month_name,
    strftime(date_day, '%Y-%m')                                       as year_month,
    extract('year'    from date_day)::integer                         as calendar_year,
    extract('quarter' from date_day)::integer                         as quarter_number,
    extract('dow'     from date_day) in (0, 6)                        as is_weekend
from spine
