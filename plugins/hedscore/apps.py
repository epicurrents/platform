"""PluginConfig for the *hedscore* plugin."""

from epicurrents.plugins import PluginConfig


class HedscoreConfig(PluginConfig):
    # Required on every concrete plugin config; without it Django's auto-detection instantiates the bare AppConfig
    # and ready() never runs. See epicurrents/plugins.py.
    default = True
    name = "plugins.hedscore"
    label = "hedscore"
    verbose_name = "HED-SCORE"
    requires_platform = ">=0.1,<0.2"
    requires: list[str] = ["annotations"]

    def ready(self):
        """Verify and parse the vendored schema, then register its validator for the ``hed`` standard.

        Parsing here rather than at the first write is what makes a missing or altered schema file stop the
        platform at boot, instead of failing the first reader who codes a finding.
        """
        from annotations.vocabularies import register_vocabulary
        from plugins.hedscore import checks  # noqa: F401
        from plugins.hedscore.schema import SCHEMA_PIN, load_schema
        from plugins.hedscore.validation import validate_hed

        load_schema()
        register_vocabulary(
            "hed",
            label=f"HED {SCHEMA_PIN.with_standard} with the SCORE library",
            version=SCHEMA_PIN.version,
            validator=validate_hed,
            confined=True,
            write_meta=SCHEMA_PIN.as_meta,
        )
