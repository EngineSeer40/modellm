with date_spine as (
    select
        range::date as date_day
    from
        range(
            date '2023-01-01',
            date '2027-01-01',
            interval '1 day'
        )
)

select
    date_day,
    year(date_day)                                                    as year_number,
    month(date_day)                                                   as month_number,
    strftime(date_day, '%Y-%m')                                       as year_month,
    quarter(date_day)                                                 as quarter_number,
    dayname(date_day)                                                 as day_of_week_name,
    dayofweek(date_day) in (0, 6)                                     as is_weekend
from date_spine
