with date_bounds as (
    select
        min(cast(transaction_ts as date)) as min_date,
        max(cast(transaction_ts as date)) as max_date
    from {{ source('raw', 'transactions_online') }}
),

date_spine as (
    select
        range_date::date as date_day
    from (
        select unnest(
            generate_series(
                (select min_date from date_bounds),
                (select max_date from date_bounds),
                interval '1 day'
            )
        ) as range_date
    )
)

select
    date_day,
    extract(year    from date_day)::integer                 as year_number,
    extract(month   from date_day)::integer                 as month_number,
    strftime(date_day, '%B')                                as month_name,
    strftime(date_day, '%Y-%m')                             as year_month,
    extract(quarter from date_day)::integer                 as quarter_number,
    strftime(date_day, '%A')                                as day_of_week_name
from date_spine
