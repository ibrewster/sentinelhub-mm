import logging
import json

from datetime import datetime,UTC
from pathlib import Path
from typing import overload, Literal, Any

import requests

import oauthlib.oauth2.rfc6749.errors
from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session

import config, constants

def sentinelhub_compliance_hook(response):
    response.raise_for_status()
    return response


@overload
def auth(token_only: Literal[True]) -> dict: ...

@overload
def auth(token_only: Literal[False] = False) -> OAuth2Session: ...

def auth(token_only: bool = False) -> dict | OAuth2Session:
    """Authenticate with the SentinelHub OAuth2 service.

    Fetches a cached token from disk if it is still valid (more than 2 minutes
    until expiry).  Otherwise, obtains a new token from the Copernicus Data
    Space Ecosystem identity provider and persists it.

    Args:
        token_only: If True, return only the raw token dictionary instead of
            an OAuth2Session instance.

    Returns:
        A token dictionary when *token_only* is True; otherwise an
        OAuth2Session ready to make authenticated requests.
    """
    oauth_secret = config.SH_OAUTH_SECRET
    oauth_id = config.SH_OAUTH_ID

    token_path = Path(__file__).parent / 'ds_auth' / 'sentinelhub_token.jwt'
    token = None
    if token_path.exists():
        token = json.loads(token_path.read_text())

        expires = datetime.fromtimestamp(token['expires_at'], tz=UTC)
        valid_time = round((expires - datetime.now(UTC)).total_seconds() / 60, 2)
        logging.info(f"Loaded token will expire in {valid_time} minutes")
        if valid_time < 2:
            token = None #token has expired or will soon. Get rid of it.

    client = BackendApplicationClient(client_id = oauth_id)
    if token is None:
        logging.info("Fetching new sentinel hub access token")
        session = OAuth2Session(client = client)

        # Get token for the session
        token = session.fetch_token(token_url='https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token',
                                    client_secret=oauth_secret)

        with open(token_path, 'w') as f:
            json.dump(token, f)
    else:
        session = OAuth2Session(client = client, token = token)

    if token_only:
        return token

    session.register_compliance_hook("access_token_response", sentinelhub_compliance_hook)

    return session

def search_sentinelhub(session: OAuth2Session, payload: dict) -> list[Any]:
    """Search the SentinelHub STAC catalog.

    Args:
        session: An authenticated OAuth2Session.
        payload: A dictionary of search parameters to send to the STAC API.

    Returns:
        A list of features from the STAC API response.
    """
    features=[]
    while True:
        try:
            response = session.post(constants.SEARCH_URL, json=payload, timeout=60)
            response.raise_for_status()
        except TokenExpiredError as e:
            logging.warning(f"Token expired: {e}. Refreshing token and retrying.")
            session = auth()  # Refresh the session with a new token
            continue
        except requests.exceptions.RequestException as e:
            logging.error(f"Request failed: {e}")
            return []

        data = response.json()
        features.extend(data.get('features', []))

        next_val = data['context'].get('next', -1)
        next_val = int(next_val)
        if next_val<0:
            break

        payload['next'] = next_val
        logging.info(f"Fetching next page: {next_val}")


    return features