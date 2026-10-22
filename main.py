import logging
import os

from datetime import datetime, timedelta, timezone

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

def _read_last_run_time() -> datetime:
    """Read the last run end time from the persisted file, or return datetime.min."""
    try:
        with open(config.LAST_RUN_FILE, "r") as f:
            iso_str = f.read().strip()
            return datetime.fromisoformat(iso_str)
    except (OSError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _write_last_run_time(end_time: datetime) -> None:
    """Persist the current run's end time to file."""
    os.makedirs(os.path.dirname(config.LAST_RUN_FILE), exist_ok=True)
    with open(config.LAST_RUN_FILE, "w") as f:
        f.write(end_time.isoformat())


if __name__ == "__main__":
    end_time = datetime.now(timezone.utc)
    last_run = _read_last_run_time()

    # start_iso is the later of: last run time, or MAX_TIMESPAN before now
    max_timespan_start = end_time - timedelta(hours=constants.MAX_TIMESPAN)
    start_time = max(last_run, max_timespan_start)

    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"start_time: {start_time.isoformat()}")
    print(f"end_time: {end_time.isoformat()}")

    targets = utils.load_targets()
    search_geom = shapely.union_all(targets['bbox'].values)
    geojson=shapely.geometry.mapping(search_geom)

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


    session = sentinelhub.auth()
    features = sentinelhub.search_sentinelhub(session, payload)

    # Persist the end time for the next run
    _write_last_run_time(end_time)

