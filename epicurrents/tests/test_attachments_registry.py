"""The attachment type registry: one owner per key, listed in key order."""

import pytest

from epicurrents import attachments
from epicurrents.attachments import PASS_NONE, AttachmentType, register_attachment_type, registered_attachment_types


def _type(key, model_label="media.mediafile", **fields):
    return AttachmentType(key=key, model_label=model_label, describe=dict, platform_pass=lambda _o: PASS_NONE, **fields)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(attachments, "_TYPES", {})


def test_a_key_registered_twice_by_different_owners_is_refused(clean_registry):
    register_attachment_type(_type("thing"))
    with pytest.raises(ValueError):
        register_attachment_type(_type("thing", model_label="recordings.recording"))


def test_a_key_registered_again_with_another_share_token_setting_is_refused(clean_registry):
    register_attachment_type(_type("thing"))
    with pytest.raises(ValueError):
        register_attachment_type(_type("thing", serves_share_tokens=False))


def test_registering_the_same_type_again_is_harmless(clean_registry):
    thing = _type("thing")
    register_attachment_type(thing)
    register_attachment_type(thing)
    assert registered_attachment_types() == [thing]


def test_a_second_ready_with_fresh_callables_is_harmless(clean_registry):
    first = _type("thing")
    register_attachment_type(first)
    # The same owner registering again builds its lambdas anew, so the two never compare equal as dataclasses.
    register_attachment_type(_type("thing", model_label="media.MediaFile"))
    assert registered_attachment_types() == [first]


def test_types_are_listed_in_key_order(clean_registry):
    register_attachment_type(_type("b"))
    register_attachment_type(_type("a"))
    assert [t.key for t in registered_attachment_types()] == ["a", "b"]


def test_media_registers_its_type():
    assert "mediafile" in {t.key for t in attachments.registered_attachment_types()}
