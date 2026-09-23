import annotations
from annotations import PinIn, RatingIn


def test_rate_message_inserts_rating():
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating="up", reason=""))
    result = annotations.get_ratings("s1")
    assert result == {"1": {"rating": "up", "reason": ""}}


def test_rate_message_upserts_existing_rating():
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating="up"))
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating="down", reason="bad"))
    result = annotations.get_ratings("s1")
    assert result == {"1": {"rating": "down", "reason": "bad"}}


def test_rate_message_empty_rating_deletes():
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating="up"))
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating=""))
    assert annotations.get_ratings("s1") == {}


def test_get_ratings_scoped_to_session():
    annotations.rate_message(RatingIn(message_id=1, session_id="s1", model="m1", rating="up"))
    annotations.rate_message(RatingIn(message_id=2, session_id="s2", model="m1", rating="down"))
    assert annotations.get_ratings("s1") == {"1": {"rating": "up", "reason": ""}}
    assert annotations.get_ratings("s2") == {"2": {"rating": "down", "reason": ""}}


def test_pin_message_inserts_pin():
    annotations.pin_message(PinIn(message_id=1, session_id="s1", content="important text", pinned=True))
    result = annotations.get_pins("s1")
    assert result == {"1": {"content": "important text"}}


def test_pin_message_unpin_deletes():
    annotations.pin_message(PinIn(message_id=1, session_id="s1", content="important text", pinned=True))
    annotations.pin_message(PinIn(message_id=1, session_id="s1", pinned=False))
    assert annotations.get_pins("s1") == {}


def test_get_pinned_context_empty_returns_empty_string():
    assert annotations.get_pinned_context("no-pins-session") == ""


def test_get_pinned_context_formats_pinned_messages():
    annotations.pin_message(PinIn(message_id=1, session_id="s1", content="step one", pinned=True))
    context = annotations.get_pinned_context("s1")
    assert "PINNED CONTEXT" in context
    assert "step one" in context
