with spine as (
    select
        cast(generate_series as date) as date_day
    from generate_series(
        cast('2023-01-01' as timestamp),
        cast('2026-12-31' as timestamp),
        interval '1 day'
    )
)

select
    date_day,
    strftime(date_day, '%A')                       as day_of_week_name,
    extract('day'     from date_day)::integer       as day_of_month,
    extract('month'   from date_day)::integer       as month_number,
    strftime(date_day, '%B')                        as month_name,
    strftime(date_day, '%Y-%m')                     as year_month,
    extract('year'    from date_day)::integer       as calendar_year,
    extract('quarter' from date_day)::integer       as quarter_number,
    extract('dow'     from date_day) in (0, 6)      as is_weekend
from spine
