import logging
import os

from datetime import datetime, timedelta, timezone

import pandas
import shapely

import config
import constants
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
    if constants.MAX_CLOUD_COVER < 100:
        payload["filter"] = {
            "op": "<=",
            "args": [{"property": constants.CLOUD_COVER_FIELD}, constants.MAX_CLOUD_COVER],
        }
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

    ## Sort by cloud cover and drop any (volcano, key) duplicates, keeping the least-cloudy scene for each volcano.
    volc_features = (
        volc_features.sort_values("cloud", kind="stable")
        .drop_duplicates(subset=["volcano", "key"], keep="first")
    )

    ## Calculate percentage overlap between the scene and the volcano bounding box
    intersections = shapely.intersection(volc_features['scene_geo'], volc_features['bbox'])
    volc_features['coverage'] = shapely.area(intersections) / shapely.area(volc_features['bbox'])

    volc_features=volc_features.drop(columns=['scene_geo', 'bbox']) #Don't need these anymore.

    ## Filter to only those with at least 50% overlap
    #volc_features = volc_features[volc_features['coverage'] >= 0.5]

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

    ## Sort by color severity, region, and longitude/latitude for display, and reset the index.
    volc_features = volc_features.sort_values(["color_rank", "region", "pos"], kind="stable")
    volc_features = volc_features.reset_index(drop=True)

    ## TODO: Make a mattermost post with the results, including volcano name, sensor, acquisition time, cloud cover, coverage percentage, and view link.
    ## TODO: Implement the mattermost post functionality here.

    ## Persist the newly processed pairs for the next run
    if not volc_features.empty:
        new_rows = pandas.DataFrame({
            "volcano": volc_features["volcano"],
            "key": volc_features["key"],
            "processed_at": datetime.now(timezone.utc),
        })

        pandas.concat([processed_df, new_rows], ignore_index=True)
        _save_processed_pairs(processed_df)

