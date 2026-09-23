import bots
from bots import BotIn


def make_bot_in(**overrides):
    fields = dict(name="Test Bot", model="llama3", allowed_tools=["read_file", "list_files"])
    fields.update(overrides)
    return BotIn(**fields)


def test_create_bot_and_list_bots():
    created = bots.create_bot(make_bot_in())
    assert "id" in created

    listed = bots.list_bots()
    assert len(listed) == 1
    bot = listed[0]
    assert bot["id"] == created["id"]
    assert bot["name"] == "Test Bot"
    assert bot["model"] == "llama3"
    assert bot["allowed_tools"] == ["read_file", "list_files"]
    assert bot["unload_strategy"] == "none"


def test_update_bot_modifies_fields():
    created = bots.create_bot(make_bot_in())
    bots.update_bot(created["id"], make_bot_in(name="Renamed Bot", allowed_tools=["bash"]))

    listed = bots.list_bots()
    bot = next(b for b in listed if b["id"] == created["id"])
    assert bot["name"] == "Renamed Bot"
    assert bot["allowed_tools"] == ["bash"]


def test_delete_bot_removes_it():
    created = bots.create_bot(make_bot_in())
    bots.delete_bot(created["id"])
    assert bots.list_bots() == []


def test_multiple_bots_ordered_by_creation():
    first = bots.create_bot(make_bot_in(name="First"))
    second = bots.create_bot(make_bot_in(name="Second"))
    listed = bots.list_bots()
    assert [b["id"] for b in listed] == [first["id"], second["id"]]


def test_bot_with_no_allowed_tools_round_trips_empty_list():
    created = bots.create_bot(make_bot_in(allowed_tools=[]))
    bot = next(b for b in bots.list_bots() if b["id"] == created["id"])
    assert bot["allowed_tools"] == []
