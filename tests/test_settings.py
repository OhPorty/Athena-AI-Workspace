import settings


def test_load_settings_missing_file_returns_empty_dict():
    assert settings.load_settings() == {}


def test_save_then_load_settings_round_trip():
    settings.save_settings({"theme": "dark", "delegation_async_enabled": True})
    assert settings.load_settings() == {"theme": "dark", "delegation_async_enabled": True}


def test_load_settings_corrupt_file_returns_empty_dict():
    with open(settings._SETTINGS_PATH, "w") as f:
        f.write("{not valid json")
    assert settings.load_settings() == {}


def test_save_settings_overwrites_previous():
    settings.save_settings({"a": 1})
    settings.save_settings({"b": 2})
    assert settings.load_settings() == {"b": 2}


def test_load_notes_missing_file_returns_empty_list():
    assert settings.load_notes() == []


def test_save_then_load_notes_round_trip():
    notes = [{"id": 1, "text": "remember this"}]
    settings.save_notes(notes)
    assert settings.load_notes() == notes


def test_load_notes_corrupt_file_returns_empty_list():
    with open(settings._NOTES_PATH, "w") as f:
        f.write("not json at all")
    assert settings.load_notes() == []
