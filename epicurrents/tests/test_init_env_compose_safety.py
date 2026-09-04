"""The .env init_env writes must survive docker compose's interpolation pass.

Compose interpolates the ``.env`` it loads, and the copy it hands a container
through ``env_file`` goes through the same pass. So a ``$`` in a generated
secret is read as a variable reference and replaced with nothing, the name it
accidentally forms being unset. The container then holds a value the file does
not contain, and the operator is handed the one that never arrived.

Found by the first production bring-up, where init_env printed
``ADMIN_PASSWORD=%Q1$ssCCq*Bf*PXK7su33Q@6`` and the container received
``%Q1*Bf*PXK7su33Q@6``. Not an edge case: Django's ``get_random_secret_key``
produced a ``$`` in 62% of draws and the password generator in 30%, so most
fresh deployments had at least one secret their .env misdescribed.

Consistency is what made it survive review. db and web agree, because both read
through compose, so the stack comes up and nothing looks wrong. It is the
readers *outside* compose that break: the printed admin password does not log
in, ``psql`` with the value from .env is refused, and a SECRET_KEY restored from
that file invalidates every session it had signed.

The interpolation itself is asserted here rather than assumed, so the test still
means something if compose's behaviour is ever what changes.
"""

import re

import pytest
from django.core.management import call_command

from epicurrents.management.commands.init_env import _KEY_REPLACEMENTS

# Names a value must not contain. Kept as a pattern rather than a bare "$" so a
# reader sees what compose is actually looking for.
_INTERPOLATION = re.compile(r"\$")


def _interpolate(value: str, environment: dict[str, str]) -> str:
    """Apply the substitution compose performs on a .env value.

    A deliberately small model of `${NAME}` and `$NAME`: enough to demonstrate
    that an unset name is replaced by nothing, which is the whole failure.
    """
    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: environment.get(m.group(1), ""), value)
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", lambda m: environment.get(m.group(1), ""), value)


class TestTheModelOfCompose:
    """The premise. If these fail, the tests below are asserting the wrong thing."""

    def test_an_unset_reference_is_replaced_with_nothing(self):
        assert _interpolate("v$b%k9C5", {}) == "v%k9C5"
        assert _interpolate("%Q1$ssCCq*Bf", {}) == "%Q1*Bf"

    def test_a_value_without_a_dollar_is_unchanged(self):
        for sample in ("v%k9C5FRb*jze9&1#ybkE^", "abc!@#%^&*", "x-y-z-1-2-3"):
            assert _interpolate(sample, {}) == sample


@pytest.mark.parametrize("name", sorted(_KEY_REPLACEMENTS))
def test_generated_secrets_survive_interpolation(name):
    """Every generator, many draws, because the failure is probabilistic.

    A single draw passes 38% of the time against the unfixed SECRET_KEY
    generator, which is a test that reports success more often than not while
    the bug is fully present.
    """
    generate = _KEY_REPLACEMENTS[name]
    for _ in range(500):
        value = generate()
        assert not _INTERPOLATION.search(value), f"{name} generated a value compose would rewrite: {value!r}"
        assert _interpolate(value, {}) == value


def test_a_written_env_file_round_trips(tmp_path):
    """The property at the level that matters: the file on disk, as written.

    Asserted against the real command rather than the generators, since what a
    deployment depends on is the file, and a future change could introduce a
    ``$`` between generating a value and writing it.
    """
    example = tmp_path / ".env.example"
    example.write_text("\n".join(f"{name}=" for name in sorted(_KEY_REPLACEMENTS)) + "\n")
    output = tmp_path / ".env"

    from django.test import override_settings

    with override_settings(BASE_DIR=tmp_path):
        call_command("init_env", output=str(output))

    written = {}
    for line in output.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            written[key.strip()] = value

    assert set(written) >= set(_KEY_REPLACEMENTS), "init_env did not fill every generated key"
    for name in _KEY_REPLACEMENTS:
        value = written[name]
        assert value, f"{name} was left empty"
        assert _interpolate(value, {}) == value, (
            f"{name} in the written .env does not survive compose interpolation: {value!r}"
        )


def test_every_value_the_command_writes_survives_interpolation(tmp_path):
    """Not only the registry: the VAPID and federation keypairs are generated on
    their own path and written to the same file.

    Both are base64url today and so cannot carry a ``$``, which is exactly why
    nothing would notice if an encoding changed. Asserted over whatever the
    command actually wrote, so a key added by any route is covered without this
    test having to learn about it.
    """
    keys = sorted(_KEY_REPLACEMENTS) + [
        "WEBPUSH_VAPID_PUBLIC_KEY",
        "WEBPUSH_VAPID_PRIVATE_KEY",
        "FEDERATION_PUBLIC_KEY",
        "FEDERATION_PRIVATE_KEY",
    ]
    example = tmp_path / ".env.example"
    example.write_text("\n".join(f"{name}=" for name in keys) + "\n")
    output = tmp_path / ".env"

    from django.test import override_settings

    with override_settings(BASE_DIR=tmp_path):
        call_command("init_env", output=str(output))

    checked = 0
    for line in output.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        if not value:
            continue
        assert _interpolate(value, {}) == value, f"{name.strip()} would be rewritten by compose: {value!r}"
        checked += 1
    assert checked >= len(keys), f"only {checked} values were written; expected at least {len(keys)}"
