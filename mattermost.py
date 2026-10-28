"""Helpers for posting text and image updates to a Mattermost channel."""

import logging

import mattermostdriver

import config

logger = logging.getLogger(__name__)


def connect():
    """Connect to Mattermost and return the driver plus channel id."""
    mattermost = mattermostdriver.Driver(
        {
            "url": config.MATTERMOST_URL,
            "token": config.MATTERMOST_TOKEN,
            "port": config.MATTERMOST_PORT,
        }
    )
    mattermost.login()
    channel_id = mattermost.channels.get_channel_by_name_and_team_name(
        config.MATTERMOST_TEAM,
        config.MATTERMOST_CHANNEL,
    )["id"]
    return mattermost, channel_id


def mm_upload(mattermost, channel_id, message, image=None, img_name=None):
    """Upload an optional image and return the post payload only on valid 2xx."""
    post_payload = {"channel_id": channel_id}

    if image and img_name:
        image.seek(0)
        upload_response = mattermost.client.make_request(
            "post",
            "/files",
            data={"channel_id": channel_id},
            files={"files": (img_name, image)},
        )
        if not 200 <= upload_response.status_code < 300:
            logger.warning(
                "Mattermost file upload failed with status %s",
                upload_response.status_code,
            )
            return None

        upload_result = upload_response.json()
        matt_id = upload_result["file_infos"][0]["id"]
        post_payload["file_ids"] = [matt_id]

    if message:
        post_payload["message"] = message

    post_response = mattermost.client.make_request(
        "post",
        "/posts",
        options=post_payload,
    )
    if not 200 <= post_response.status_code < 300:
        logger.warning(
            "Mattermost post failed with status %s",
            post_response.status_code,
        )
        return None

    post_result = post_response.json()
    if not isinstance(post_result, dict) or not post_result.get("id"):
        logger.warning("Mattermost returned an invalid post response")
        return None
    return post_result


if __name__ == "__main__":
    mm, channel = connect()
    print(f"Connected to Mattermost channel {channel}")
    msg = mm_upload(mm, channel, "Test post from sentinelhub-mm")
    print(msg)
