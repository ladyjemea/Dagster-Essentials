import base64
import os

import geopandas as gpd
import pandas as pd
import plotly.express as px
import plotly.io as pio
from dagster import (
    AssetExecutionContext,
    AssetKey,
    MaterializeResult,
    MetadataValue,
    asset,
)
from dagster_duckdb import DuckDBResource

from ..partitions import weekly_partition
from . import constants


@asset(
    deps=[AssetKey(["taxi_trips"])],
    partitions_def=weekly_partition,
    compute_kind="DuckDB",
)
def trips_by_week(context: AssetExecutionContext, database: DuckDBResource):
    """
    The number of trips per week, aggregated by week.
    These date-based aggregations are done in-memory, which is expensive, but enables you to do time-based aggregations consistently across data warehouses (ex. DuckDB and BigQuery)
    """

    period_to_fetch = context.partition_key

    # get all trips for the week
    query = f"""
        select vendor_id, total_amount, trip_distance, passenger_count
        from trips
        where pickup_datetime >= '{period_to_fetch}'
            and pickup_datetime < '{period_to_fetch}'::date + interval '1 week'
    """

    with database.get_connection() as conn:
        data_for_month = conn.execute(query).fetch_df()

    aggregate = (
        data_for_month.agg(
            {
                "vendor_id": "count",
                "total_amount": "sum",
                "trip_distance": "sum",
                "passenger_count": "sum",
            }
        )
        .rename({"vendor_id": "num_trips"})
        .to_frame()
        .T
    )  # type: ignore

    # clean up the formatting of the dataframe
    aggregate["period"] = period_to_fetch
    aggregate["num_trips"] = aggregate["num_trips"].astype(int)
    aggregate["passenger_count"] = aggregate["passenger_count"].astype(int)
    aggregate["total_amount"] = aggregate["total_amount"].round(2).astype(float)
    aggregate["trip_distance"] = aggregate["trip_distance"].round(2).astype(float)
    aggregate = aggregate[
        ["period", "num_trips", "total_amount", "trip_distance", "passenger_count"]
    ]

    try:
        # If the file already exists, append to it, but replace the existing month's data
        existing = pd.read_csv(constants.TRIPS_BY_WEEK_FILE_PATH)
        existing = existing[existing["period"] != period_to_fetch]
        existing = pd.concat([existing, aggregate]).sort_values(by="period")
        existing.to_csv(constants.TRIPS_BY_WEEK_FILE_PATH, index=False)
    except FileNotFoundError:
        aggregate.to_csv(constants.TRIPS_BY_WEEK_FILE_PATH, index=False)


@asset(
    deps=[AssetKey(["taxi_trips"]), AssetKey(["taxi_zones"])],
    key_prefix="manhattan",
    compute_kind="DuckDB",
)
def manhattan_stats(database: DuckDBResource):
    """
    Metrics on taxi trips in Manhattan
    """

    query = """
        select
            zones.zone,
            zones.borough,
            zones.geometry,
            count(1) as num_trips
        from trips
        left join zones on trips.pickup_zone_id = zones.zone_id
        where geometry is not null
        group by zone, borough, geometry
    """

    with database.get_connection() as conn:
        trips_by_zone = conn.execute(query).fetch_df()

    # Clean the geometry column
    trips_by_zone = trips_by_zone[trips_by_zone["geometry"].notnull()]  # Remove nulls
    trips_by_zone["geometry"] = gpd.GeoSeries.from_wkt(
        trips_by_zone["geometry"], on_invalid="ignore"
    )
    trips_by_zone = gpd.GeoDataFrame(trips_by_zone)

    # Write the GeoDataFrame to a GeoJSON file
    trips_by_zone.to_file(constants.MANHATTAN_STATS_FILE_PATH, driver="GeoJSON")


@asset(
    deps=[AssetKey(["manhattan", "manhattan_stats"])],
    compute_kind="Python",
)
def manhattan_map():
    """
    A map of the number of trips per taxi zone in Manhattan
    """

    file_path = constants.MANHATTAN_STATS_FILE_PATH

    # Ensure the GeoJSON file exists
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"GeoJSON file not found: {file_path}")

    # Read the GeoJSON file
    trips_by_zone = gpd.read_file(file_path)

    # Ensure required columns exist
    if (
        "geometry" not in trips_by_zone.columns
        or "num_trips" not in trips_by_zone.columns
    ):
        raise ValueError(f"Required columns missing in {file_path}")

    # Remove rows with null or invalid geometries
    trips_by_zone = trips_by_zone[trips_by_zone["geometry"].notnull()]

    # Create the Plotly figure
    fig = px.choropleth_mapbox(
        trips_by_zone,
        geojson=trips_by_zone.geometry.__geo_interface__,
        locations=trips_by_zone.index,
        color="num_trips",
        color_continuous_scale="Plasma",
        mapbox_style="carto-positron",
        center={"lat": 40.758, "lon": -73.985},
        zoom=11,
        opacity=0.7,
        labels={"num_trips": "Number of Trips"},
    )

    # Save the map as an image
    try:
        pio.write_image(fig, constants.MANHATTAN_MAP_FILE_PATH)
    except Exception as e:
        print(f"Failed to save the image: {e}")
        raise

    # Convert the image to Base64 for metadata preview
    with open(constants.MANHATTAN_MAP_FILE_PATH, "rb") as file:
        image_data = file.read()

    base64_data = base64.b64encode(image_data).decode("utf-8")
    md_content = f"![Image](data:image/jpeg;base64,{base64_data})"

    return MaterializeResult(metadata={"preview": MetadataValue.md(md_content)})
