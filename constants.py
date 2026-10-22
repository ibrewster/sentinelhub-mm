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
MAX_CLOUD_COVER = 0.8
MAX_TIMESPAN = 24  # hours


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

SEARCH_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
BROWSER_BASE_URL = "https://browser.dataspace.copernicus.eu/"