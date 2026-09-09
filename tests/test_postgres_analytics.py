from __future__ import annotations

from src.loading.postgres_loader import ROOT


ANALYTICS_SQL_DIR = ROOT / "sql" / "analytics"


def _analytics_sql(filename: str) -> str:
    return (ANALYTICS_SQL_DIR / filename).read_text(encoding="utf-8")


def test_national_monthly_table_enforces_metric_contract() -> None:
    sql = _analytics_sql("001_create_national_monthly_kpi.sql")

    for column in (
        "month_start",
        "median_unit_price_10k_ping",
        "avg_unit_price_10k_ping",
        "median_total_price_10k",
        "avg_total_price_10k",
        "price_complete_transaction_count",
        "median_unit_price_yoy",
        "avg_unit_price_yoy",
        "median_total_price_yoy",
        "avg_total_price_yoy",
        "price_complete_transaction_count_yoy",
    ):
        assert column in sql

    assert "PRIMARY KEY (month_start)" in sql
    assert "price_complete_transaction_count >= 0" in sql
    assert "national_monthly_kpi_price_population_check" in sql
    assert "median_unit_price_yoy > -1" in sql
    assert "price_complete_transaction_count_yoy >= -1" in sql


def test_full_refresh_uses_dynamic_75_percent_cutoff_before_truncate() -> None:
    sql = _analytics_sql("002_refresh_national_monthly_kpi_full.sql")

    assert "DATE '2024-07-01'" not in sql
    assert "0.75" in sql
    assert "PERCENTILE_CONT(0.5)" in sql
    assert "MAX(monthly.month_start)::DATE AS analysis_end_month" in sql
    assert "national_monthly_full_refresh_cutoff_gate" in sql
    assert sql.index("national_monthly_full_refresh_cutoff_gate") < sql.index(
        "TRUNCATE TABLE analytics.national_monthly_kpi"
    )
    assert "GENERATE_SERIES(" in sql
    assert "cutoff.analysis_end_month" in sql


def test_full_refresh_calculates_yoy_from_stored_price_precision() -> None:
    sql = _analytics_sql("002_refresh_national_monthly_kpi_full.sql")
    complete_months = sql.index("complete_months AS (")
    year_pairs = sql.index("year_pairs AS (")
    complete_months_sql = sql[complete_months:year_pairs]

    assert "median_unit_price_10k_ping::NUMERIC(18,6)" in complete_months_sql
    assert "avg_unit_price_10k_ping::NUMERIC(18,6)" in complete_months_sql
    assert "median_total_price_10k::NUMERIC(18,6)" in complete_months_sql
    assert "avg_total_price_10k::NUMERIC(18,6)" in complete_months_sql


def test_incremental_refresh_extends_without_automatic_shrink() -> None:
    sql = _analytics_sql("003_refresh_national_monthly_kpi_incremental.sql")

    assert "\\if :{?load_batch_id}" in sql
    assert "PERCENTILE_CONT(0.5)" in sql
    assert "candidate_end_month" in sql
    assert "current_end_month" in sql
    assert "GREATEST(" in sql
    assert "effective_end_month" in sql
    assert "national_monthly_affected_base_months" in sql
    assert "national_monthly_affected_yoy_months" in sql
    assert "affected.month_start + INTERVAL '1 year'" in sql
    assert "TRUNCATE TABLE analytics.national_monthly_kpi" not in sql
    assert "DELETE FROM analytics.national_monthly_kpi" not in sql


def test_validation_sql_recomputes_and_does_not_modify_permanent_data() -> None:
    sql = _analytics_sql("004_validate_national_monthly_kpi.sql")

    assert "FROM core.fact_transactions AS fact" in sql
    assert "national_monthly_expected_kpi" in sql
    assert "PASS_ALIGNED" in sql
    assert "REVIEW_CURRENT_AHEAD_OF_CANDIDATE" in sql
    assert "missing_month_count" in sql
    assert "unexpected_month_count" in sql
    assert "median_unit_price_mismatch_count" in sql
    assert "transaction_count_yoy_mismatch_count" in sql
    assert "LIMIT 50" in sql

    assert "INSERT INTO analytics.national_monthly_kpi" not in sql
    assert "UPDATE analytics.national_monthly_kpi" not in sql
    assert "DELETE FROM analytics.national_monthly_kpi" not in sql
    assert "TRUNCATE TABLE analytics.national_monthly_kpi" not in sql


def test_city_monthly_table_enforces_month_county_contract() -> None:
    sql = _analytics_sql("005_create_city_monthly_kpi.sql")

    for column in (
        "month_start",
        "county_id",
        "city",
        "median_unit_price_10k_ping",
        "avg_unit_price_10k_ping",
        "median_total_price_10k",
        "avg_total_price_10k",
        "price_complete_transaction_count",
        "median_unit_price_yoy",
        "avg_unit_price_yoy",
        "median_total_price_yoy",
        "avg_total_price_yoy",
        "price_complete_transaction_count_yoy",
    ):
        assert column in sql

    assert "PRIMARY KEY (month_start, county_id)" in sql
    assert "UNIQUE (month_start, city)" in sql
    assert "county_id ~ '^[0-9]{5}$'" in sql
    assert "price_complete_transaction_count >= 0" in sql
    assert "city_monthly_kpi_price_population_check" in sql
    assert "price_complete_transaction_count_yoy >= -1" in sql
    assert "ON analytics.city_monthly_kpi (county_id, month_start)" in sql


def test_city_full_refresh_uses_national_range_and_complete_county_spine() -> None:
    sql = _analytics_sql("006_refresh_city_monthly_kpi_full.sql")

    assert "MAX(national.month_start)::DATE" in sql
    assert "city_monthly_full_refresh_counties" in sql
    assert "v_county_count <> 22" in sql
    assert "month_county_spine AS (" in sql
    assert "CROSS JOIN city_monthly_full_refresh_counties AS county" in sql
    assert "LEFT JOIN monthly_city_aggregation AS aggregation" in sql
    assert "previous_year.county_id = current_month.county_id" in sql
    assert sql.index("city_monthly_full_refresh_input_gate") < sql.index(
        "TRUNCATE TABLE analytics.city_monthly_kpi"
    )
    assert "COUNT(target.month_start)::BIGINT AS refreshed_row_count" in sql


def test_city_incremental_refresh_recomputes_m_and_m_plus_12() -> None:
    sql = _analytics_sql("007_refresh_city_monthly_kpi_incremental.sql")

    assert "\\if :{?load_batch_id}" in sql
    assert "refresh_range.published_end_month" in sql
    assert "city_monthly_affected_base_months" in sql
    assert "city_monthly_affected_yoy_months" in sql
    assert "affected.month_start + INTERVAL '1 year'" in sql
    assert "CROSS JOIN city_monthly_refresh_counties AS county" in sql
    assert "ON CONFLICT (month_start, county_id)" in sql
    assert "previous_year.county_id = current_month.county_id" in sql
    assert "target.county_id = recalculated.county_id" in sql
    assert "TRUNCATE TABLE analytics.city_monthly_kpi" not in sql
    assert "DELETE FROM analytics.city_monthly_kpi" not in sql


def test_city_validation_recomputes_expected_values_without_permanent_writes() -> None:
    sql = _analytics_sql("008_validate_city_monthly_kpi.sql")

    assert "FROM core.fact_transactions AS fact" in sql
    assert "city_monthly_expected_kpi" in sql
    assert "COUNT(*) <> 22" in sql
    assert "missing_row_count" in sql
    assert "city_name_mismatch_count" in sql
    assert "median_unit_price_mismatch_count" in sql
    assert "transaction_count_yoy_mismatch_count" in sql
    assert "national_city_transaction_count_mismatch_month_count" in sql
    assert "LIMIT 50" in sql

    assert "INSERT INTO analytics.city_monthly_kpi" not in sql
    assert "UPDATE analytics.city_monthly_kpi" not in sql
    assert "DELETE FROM analytics.city_monthly_kpi" not in sql
    assert "TRUNCATE TABLE analytics.city_monthly_kpi" not in sql


def test_district_rolling_table_enforces_anchor_location_contract() -> None:
    sql = _analytics_sql("009_create_district_rolling_3m.sql")

    for column in (
        "month_start",
        "county_id",
        "town_id",
        "city",
        "district",
        "median_unit_price_10k_ping",
        "avg_unit_price_10k_ping",
        "median_total_price_10k",
        "avg_total_price_10k",
        "price_complete_transaction_count",
        "median_unit_price_yoy",
        "avg_unit_price_yoy",
        "median_total_price_yoy",
        "avg_total_price_yoy",
        "price_complete_transaction_count_yoy",
    ):
        assert column in sql

    assert "PRIMARY KEY (month_start, town_id)" in sql
    assert "UNIQUE (month_start, county_id, district)" in sql
    assert "price_complete_transaction_count >= 0" in sql
    assert "district_rolling_3m_price_population_check" in sql
    assert "ON analytics.district_rolling_3m (county_id, month_start, town_id)" in sql


def test_district_full_refresh_uses_dynamic_complete_spine_and_range_join() -> None:
    sql = _analytics_sql("010_refresh_district_rolling_3m_full.sql")

    assert "DATE '2012-10-01' AS first_anchor_month" in sql
    assert "MAX(national.month_start)::DATE AS published_end_month" in sql
    assert "anchor_location_spine AS (" in sql
    assert "CROSS JOIN district_rolling_full_refresh_locations" in sql
    assert "LEFT JOIN rolling_aggregation AS aggregation" in sql
    assert "anchor.anchor_month - INTERVAL '2 months'" in sql
    assert "COUNT(transaction.source_transaction_id)" in sql
    assert "previous_year.town_id = current_anchor.town_id" in sql
    assert "TRUNCATE TABLE analytics.district_rolling_3m" in sql
    assert "368" not in sql


def test_district_incremental_refresh_expands_m_through_m_plus_2_and_yoy() -> None:
    sql = _analytics_sql("011_refresh_district_rolling_3m_incremental.sql")

    assert "\\if :{?load_batch_id}" in sql
    assert "GENERATE_SERIES(0, 2)" in sql
    assert "MAKE_INTERVAL(months => month_offset.offset_value)" in sql
    assert "district_rolling_affected_base_anchors" in sql
    assert "district_rolling_affected_yoy_anchors" in sql
    assert "affected.anchor_month + INTERVAL '1 year'" in sql
    assert "CROSS JOIN district_rolling_refresh_locations" in sql
    assert "COUNT(transaction.source_transaction_id)" in sql
    assert "ON CONFLICT (month_start, town_id)" in sql
    assert "previous_year.town_id = current_anchor.town_id" in sql
    assert "TRUNCATE TABLE analytics.district_rolling_3m" not in sql
    assert "DELETE FROM analytics.district_rolling_3m" not in sql
    assert "368" not in sql


def test_district_validation_recomputes_without_permanent_writes() -> None:
    sql = _analytics_sql("012_validate_district_rolling_3m.sql")

    assert "FROM core.fact_transactions AS fact" in sql
    assert "district_rolling_expected_kpi" in sql
    assert "COUNT(transaction.source_transaction_id)" in sql
    assert "missing_row_count" in sql
    assert "location_name_mismatch_count" in sql
    assert "median_unit_price_mismatch_count" in sql
    assert "transaction_count_yoy_mismatch_count" in sql
    assert "city_district_transaction_count_mismatch_count" in sql
    assert "LIMIT 50" in sql

    assert "INSERT INTO analytics.district_rolling_3m" not in sql
    assert "UPDATE analytics.district_rolling_3m" not in sql
    assert "DELETE FROM analytics.district_rolling_3m" not in sql
    assert "TRUNCATE TABLE analytics.district_rolling_3m" not in sql
    assert "368" not in sql
