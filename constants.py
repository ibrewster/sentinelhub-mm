import re

####################
# Logging config
####################
LOG_FORMAT = '%(asctime)s %(levelname)-8s - %(message)s'


#####################
# Volcano target CSV file
#####################
TARGETS_CSV = 'SentinelHub_Monitor_Volcano_Locations.csv'


####################
# Run-time config
####################
MAX_CLOUD_COVER = 80
MAX_TIMESPAN = 24  # hours

# How long to retain processed (volcano, key) pairs before they become eligible
# for reprocessing again. Older entries are purged on each run.
RETENTION_DAYS = 7


####################
# STAC / Catalog constants
####################
# EOB / STAC property that carries per-scene cloud cover as a percentage (0-100).
CLOUD_COVER_FIELD = "eo:cloud_cover"


# Catalog (STAC) collection IDs on CDSE Sentinel Hub.
#   sentinel-2-l1c  -> Sentinel-2 L1C (top-of-atmosphere)
#   landsat-ot-l1   -> Landsat 8-9 OLI-TIRS Collection 2 Level 1
#
# `browser_layer` is the Copernicus Browser `layerId` for the tonemapped natural
# color visualization. It differs per dataset (the numeric prefix is the layer's
# position in that dataset's layer list), so it is configured per sensor.
SENSORS = {
    "Sentinel-2 L1C": {
        "collection": "sentinel-2-l1c",
        # Copernicus Browser dataset identifier used in deep-links.
        "browser_dataset": "S2_L1C_CDAS",
        "browser_layer": "2_TONEMAPPED_NATURAL_COLOR",
        # Process API data type for image rendering.
        "process_type": "sentinel-2-l1c",
    },
    "Landsat 8-9 L1": {
        "collection": "landsat-ot-l1",
        "browser_dataset": "CDAS_L8_L9_LOTL1",
        "browser_layer": "3_TONEMAPPED_NATURAL_COLOR",
        "process_type": "landsat-ot-l1",
    },
}

######################
# URLs for SentinelHub APIs
######################
SEARCH_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
BROWSER_BASE_URL = "https://browser.dataspace.copernicus.eu/"

######################
# Regexes for parsing scene IDs to identify acquisition pass
######################
# Sentinel-2 scene id, e.g.
#   S2C_MSIL1C_20261019T224551_N0513_R101_T02ULC_20261020T002300.SAFE
# The datatake sensing time (20261019T224551) + relative orbit (R101) uniquely
# identify a single acquisition pass; the tile code (T02ULC) is what varies.
S2_ID_RE = re.compile(r"_(\d{8}T\d{6})_N\d+_R(\d+)_")

# Landsat scene id, e.g.
#   LC09_L1TP_087024_20261018_20261019_02_T2
# The WRS path/row (087024) + acquisition date (20261018) identify one overpass.
LANDSAT_ID_RE = re.compile(r"^L[COTE]\d{2}_[A-Z0-9]+_(\d{6})_(\d{8})_")