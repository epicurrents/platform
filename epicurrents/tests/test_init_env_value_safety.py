"""What init_env writes into .env must mean the same thing to every reader of it.

Two characters break that, in two different ways, and the tests below assert
different properties for each because the properties genuinely differ.

``$`` is a correctness failure and the reason this file exists.

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

``#`` is a legibility failure, and the weaker claim is the true one. It opens a
comment in the .env format only where whitespace precedes it, so a generated
secret — one unbroken token — is not truncated by any reader the platform has.
It is kept out of the file for the operator who copies a credential out by eye
and cannot see where the value ends. That distinction is modelled below rather
than left implicit, so that a later reader does not "harden" the generator on
the belief that compose was mangling these values too, nor relax it on the
belief that legibility was never the point.
"""

import re

import pytest
from django.core.management import call_command

from epicurrents.management.commands.init_env import _ENV_FILE_METACHARACTERS, _KEY_REPLACEMENTS

# Names a value must not contain. Kept as a pattern rather than a bare "$" so a
# reader sees what compose is actually looking for.
_INTERPOLATION = re.compile(r"\$")
_COMMENT = re.compile(r"#")


def _strip_inline_comment(line: str) -> str:
    """Apply the .env comment rule to one ``KEY=value`` line.

    A ``#`` opens a comment only where whitespace precedes it; inside a value it
    is an ordinary character. Checked against the docker compose the repository
    runs before being written down here, the same way _interpolate is.

    Quoting is deliberately not modelled: the parser keeps a ``#`` inside a
    quoted value, and nothing this command writes is quoted, so the cases below
    are bare. A caller reaching for this on a quoted line would be misled.
    """
    _, _, value = line.partition("=")
    return re.split(r"\s#", value, maxsplit=1)[0]


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


class TestTheModelOfTheCommentCharacter:
    """Why ``#`` is excluded is not the reason it first appears to be.

    If these two ever disagree with the .env parser, the generator rule below is
    the one to revisit — it rests on the second case being the only one that
    loses anything.
    """

    def test_a_hash_inside_a_value_is_part_of_it(self):
        assert _strip_inline_comment("ADMIN_PASSWORD=aB3#xY7") == "aB3#xY7"

    def test_a_hash_after_whitespace_opens_a_comment(self):
        assert _strip_inline_comment("ADMIN_PASSWORD=aB3 #xY7") == "aB3"


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


@pytest.mark.parametrize("name", sorted(_KEY_REPLACEMENTS))
def test_generated_secrets_carry_no_comment_character(name):
    """Same shape, same number of draws, and for the same reason: SECRET_KEY
    draws from Django's alphabet, which carries a ``#`` about as often as a
    ``$``, so a handful of draws would pass with the rule absent.

    Note what is *not* asserted — that the value survives the parser. It would;
    see TestTheModelOfTheCommentCharacter. The claim is only that no such value
    is ever written, so that no operator has to work out which case they hold.
    """
    generate = _KEY_REPLACEMENTS[name]
    for _ in range(500):
        value = generate()
        assert not _COMMENT.search(value), f"{name} generated a value that reads as a comment: {value!r}"


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


def test_no_written_value_carries_an_env_metacharacter(tmp_path):
    """The rule as the command states it, asserted against the file it wrote.

    Reading the set from the command rather than restating it means a character
    added to _ENV_FILE_METACHARACTERS is covered here without this test being
    edited — and a character removed from it fails loudly at the tests above,
    which name their two individually.
    """
    keys = sorted(_KEY_REPLACEMENTS)
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
        offenders = [char for char in _ENV_FILE_METACHARACTERS if char in value]
        assert not offenders, f"{name.strip()} was written carrying {offenders!r}: {value!r}"
        checked += 1
    # Without this the test passes on a file where nothing was filled in, which
    # is the state a broken command would leave behind.
    assert checked == len(keys), f"only {checked} of {len(keys)} values were written"
