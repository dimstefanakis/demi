from datetime import datetime, timezone

from claudius.messaging.telegram import TelegramUpdateParser


def test_parse_text_message():
    update = {
        "update_id": 10000,
        "message": {
            "message_id": 51,
            "from": {"id": 123, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 987654, "type": "private"},
            "date": 1_700_000_000,
            "text": "Hello world",
        },
    }

    msg = TelegramUpdateParser.parse(update)
    assert msg is not None
    assert msg.provider == "telegram"
    assert msg.tenant_external_id == "987654"
    assert msg.provider_message_id == "51"
    assert msg.text == "Hello world"
    assert msg.images == []
    assert msg.received_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_parse_photo_message_caption():
    update = {
        "update_id": 10001,
        "message": {
            "message_id": 52,
            "from": {"id": 124, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 987654, "type": "private"},
            "date": 1_700_000_100,
            "caption": "Replace the hero image",
            "photo": [
                {"file_id": "abc", "width": 90, "height": 90},
                {"file_id": "def", "width": 320, "height": 320},
            ],
        },
    }

    msg = TelegramUpdateParser.parse(update)
    assert msg is not None
    assert msg.text == "Replace the hero image"
    assert len(msg.images) == 2
    assert msg.images[0].provider_file_id == "abc"
    assert msg.images[1].provider_file_id == "def"


def test_ignore_non_message_update():
    update = {"update_id": 10002, "edited_message": {"message_id": 1}}
    assert TelegramUpdateParser.parse(update) is None
