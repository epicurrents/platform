"""The attachment type registry: one owner per key, listed in key order."""

import pytest

from epicurrents import attachments
from epicurrents.attachments import PASS_NONE, AttachmentType, register_attachment_type, registered_attachment_types


def _type(key):
    return AttachmentType(key=key, model_label="media.mediafile", describe=dict, platform_pass=lambda _o: PASS_NONE)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(attachments, "_TYPES", {})


def test_a_key_registered_twice_by_different_owners_is_refused(clean_registry):
    register_attachment_type(_type("thing"))
    with pytest.raises(ValueError):
        register_attachment_type(_type("thing"))


def test_registering_the_same_type_again_is_harmless(clean_registry):
    thing = _type("thing")
    register_attachment_type(thing)
    register_attachment_type(thing)
    assert registered_attachment_types() == [thing]


def test_types_are_listed_in_key_order(clean_registry):
    register_attachment_type(_type("b"))
    register_attachment_type(_type("a"))
    assert [t.key for t in registered_attachment_types()] == ["a", "b"]


def test_media_registers_its_type():
    assert "mediafile" in {t.key for t in attachments.registered_attachment_types()}
