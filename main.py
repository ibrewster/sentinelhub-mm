import io
import logging
import os

from datetime import datetime, timedelta, timezone

import pandas
import shapely

import config
import constants
import graphics
import mattermost
import utils
import sentinelhub

file_handler = logging.FileHandler(config.LOG_FILE)
file_handler.setLevel(config.LOG_FILE_LEVEL)

logging.basicConfig(
    level=config.LOG_CONSOLE_LEVEL,
    format=constants.LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        file_handler,
    ],
)

def _load_processed_pairs() -> pandas.DataFrame:
    """Load processed (volcano, key) pairs from a pickled DataFrame.
    
    Drops any entries older than RETENTION_DAYS and returns the retained
    DataFrame. Returns an empty DataFrame if the file doesn't exist or is invalid.
    Does NOT write back to disk — the caller is responsible for saving.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=constants.RETENTION_DAYS)
    
    try:
        df = pandas.read_pickle(config.PROCESSED_PAIRS_FILE)
    except (OSError, Exception):
        return pandas.DataFrame(columns=["volcano", "key", "processed_at"])
    
    # Drop expired entries
    df = df[df["processed_at"] >= cutoff].reset_index(drop=True)
    return df


def _save_processed_pairs(df: pandas.DataFrame) -> None:
    """Save the processed pairs DataFrame to disk as a pickle file."""
    os.makedirs(os.path.dirname(config.PROCESSED_PAIRS_FILE), exist_ok=True)
    df.to_pickle(config.PROCESSED_PAIRS_FILE)


if __name__ == "__main__":
    ## Determine the time range for the search: from MAX_TIMESPAN hours ago to now.
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=constants.MAX_TIMESPAN)

    ## Format the times as ISO 8601 strings for the STAC API query.
    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"start_time: {start_time.isoformat()}")
    print(f"end_time: {end_time.isoformat()}")

    ## Load the volcano targets and their bounding boxes for the search.
    targets = utils.load_targets()
    search_geom = shapely.union_all(targets['bbox'].values)
    geojson=shapely.geometry.mapping(search_geom)

    ## Build the STAC search payload with the time range, bounding box, and cloud cover filter.
    payload = {
        "collections": ["sentinel-2-l1c", "landsat-ot-l1"],
        "intersects": geojson,
        "datetime": f"{start_iso}/{end_iso}",
        "limit": 100,
    }
    cloud_property = {"property": constants.CLOUD_COVER_FIELD}
    cloud_filters = [
        {"op": ">=", "args": [cloud_property, 0]},
    ]
    if constants.MAX_CLOUD_COVER < 100:
        cloud_filters.append(
            {
                "op": "<=",
                "args": [cloud_property, constants.MAX_CLOUD_COVER],
            }
        )
    payload["filter"] = {"op": "and", "args": cloud_filters}
    payload["filter-lang"] = "cql2-json"

    ## Authenticate with SentinelHub and perform the search.
    session = sentinelhub.auth()
    features = sentinelhub.search_sentinelhub(session, payload)

    ## Associate each scene with its volcano(s).
    targets=targets.reset_index(drop=True)
    tree = shapely.STRtree(targets['bbox'].values)
    feature_idx, target_idx = tree.query(features["geometry"].to_numpy(), predicate="intersects")

    volc_features = (
        pandas.DataFrame({"feature_idx": feature_idx, "target_idx": target_idx})
        .join(features.rename(columns={"geometry": "scene_geo"}), on="feature_idx")
        .join(targets, on="target_idx")
        .drop(columns=["feature_idx", "target_idx"])
    )

    # Unknown cloud cover (commonly reported as -1) must not be treated as
    # clearer than valid scenes when choosing one acquisition per volcano.
    volc_features = volc_features[
        volc_features["cloud"].between(0, constants.MAX_CLOUD_COVER)
    ]

    ## Drop any (volcano, key) pairs that have already been processed.
    processed_df = _load_processed_pairs()
    if not processed_df.empty:
        before_count = len(volc_features)

        # Create MultiIndexes from the matching columns
        idx_processed = pandas.MultiIndex.from_frame(processed_df[['volcano', 'key']])
        idx_available = pandas.MultiIndex.from_frame(volc_features[['volcano', 'key']])

        # Keep only rows in available_data that are NOT in processed_data
        volc_features = volc_features[~idx_available.isin(idx_processed)]

        dropped = before_count - len(volc_features)
        if dropped > 0:
            print(f"Dropped {dropped} already-processed (volcano, key) pairs.")

    # IDEA: if we want coverage calculation, we need to union the 'scene_geo'
    # geometries across (volcano, key) pairs before dropping duplicates,
    # otherwise we will only have the coverage of the least-cloudy scene for each volcano.
    ## Sort by cloud cover and drop any (volcano, key) duplicates, keeping the least-cloudy scene for each volcano.
    volc_features = (
        volc_features.sort_values("cloud", kind="stable")
        .drop_duplicates(subset=["volcano", "key"], keep="first")
    )

    ## Calculate percentage overlap between the scene and the volcano bounding box
    ## NOTE: need to use unioned geometries for each (volcano, key) pair here (see IDEA above)
    ## to get the total coverage
    # intersections = shapely.intersection(volc_features['scene_geo'], volc_features['bbox'])
    # volc_features['coverage'] = shapely.area(intersections) / shapely.area(volc_features['bbox'])

    ## Filter to only those with at least 50% overlap
    #volc_features = volc_features[volc_features['coverage'] >= 0.5]

    volc_features=volc_features.drop(columns=['scene_geo', 'bbox']) #Don't need these anymore.

    ## Add view links for each scene to the Copernicus Browser for the appropriate sensor.
    volc_features['view_link'] = [
        utils.build_browser_link(lat,lon,sensor,acquired)
        for lat, lon, sensor, acquired in zip(
            volc_features['latitude'],
            volc_features['longitude'],
            volc_features['sensor'],
            volc_features['acquired'],
        )
    ]

    volc_features['nighttime'] = (utils.solar_elevation_degrees(
        volc_features['acquired'].to_numpy(),
        volc_features['latitude'].to_numpy(),
        volc_features['longitude'].to_numpy()
    ) < 0)

    ## Sort by color severity, region, and longitude/latitude for display, and reset the index.
    volc_features = volc_features.sort_values(["color_rank", "region", "pos"], kind="stable")
    volc_features = volc_features.reset_index(drop=True)

    ## Render each scene sequentially and post it to Mattermost immediately.
    mm_client, mm_channel = mattermost.connect()
    for row in volc_features.itertuples(index=False):
        image = graphics.render_scene_image(
            session=session,
            target=row.volcano,
            lat=row.latitude,
            lon=row.longitude,
            sensor=row.sensor,
            acquired=row.acquired,
            nighttime=row.nighttime,
        )
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="PNG")

        filename = (
            f"{row.volcano}_{row.sensor}_{row.acquired}.png"
        )
        imagery_note = ""
        if row.nighttime:
            imagery_note = (
                "**Imagery:** Thermal infrared (Landsat Band 10)\n"
                if row.sensor == "Landsat 8-9 L1"
                else "**Imagery:** Nighttime; no thermal bands available\n"
            )
        message = (
            f"### {row.volcano} ({row.color})\n"
            f"**Sensor:** {row.sensor}\n"
            f"**Acquired:** {row.acquired} UTC\n"
            f"{imagery_note}"
   #         f"**Cloud cover:** {row.cloud:.1f}%\n"
   #         f"**Coverage:** {row.coverage * 100:.1f}%\n"
            f"**View link:** {row.view_link}"
        )

        mattermost.mm_upload(
            mm_client,
            mm_channel,
            message,
            image=io.BytesIO(image_buffer.getvalue()),
            img_name=filename,
        )

    ## Persist the newly processed pairs for the next run
    if not volc_features.empty:
        new_rows = pandas.DataFrame({
            "volcano": volc_features["volcano"],
            "key": volc_features["key"],
            "processed_at": datetime.now(timezone.utc),
        })

        processed_df = pandas.concat([processed_df, new_rows], ignore_index=True)
        _save_processed_pairs(processed_df)
