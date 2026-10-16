# hedscore

Registers HED with the SCORE library ([Hermes et al., *Scientific Data*, 2025](https://doi.org/10.1038/s41597-025-05791-2)) as the `hed` annotation-code vocabulary. Findings, what a reader concludes from the signal, are coded in it; what happened during the recording stays in the platform's own `epicurrents.biosignal` and `epicurrents.eeg` sets ([annotation-event-vocabulary.md § 9](../../docs/engineering-notes/annotation-event-vocabulary.md)). Design in [hed-score-integration.md](../../docs/engineering-notes/hed-score-integration.md).

The plugin has no models, so it adds nothing to the erasure or subject-export registries. Enable it with `scripts/enable_plugin.sh hedscore`.

## The schema

[schemas/HED_score_2.1.0.xml](schemas/HED_score_2.1.0.xml) is the merged release of SCORE 2.1.0 with HED 8.4.0, vendored from [hed-standard/hed-schemas](https://github.com/hed-standard/hed-schemas/tree/main/library_schemas/score/hedxml) unmodified, under CC-BY-4.0 ([schemas/LICENSE](schemas/LICENSE), copyright the HED contributors). `SCHEMA_PIN` in [schema.py](schema.py) names its version and SHA-256. `ready()` checks the digest and parses the file into an index of 1527 terms, 396 of them SCORE's; a missing or altered file raises `ImproperlyConfigured` and the platform does not start. The schema is never fetched at runtime.

A new release is vendored beside the old one and the pin moved in the same commit. Rows keep the release they were validated against in their `meta`, so history can be re-checked against a newer schema.

## The validator

A `hed` code's `value` is a HED string and its `meta` is the schema pin, `{"schema": {"library": "score", "version": "2.1.0", "with_standard": "8.4.0"}}`, exactly. The rules are narrower than HED's own, because a code's value is served to readers under a de-identifying grant and must not carry typed text:

- Tags in their short form, spelt as the schema spells them. Long forms and extensions are refused.
- A value only where the placeholder's value class is numeric, with a unit from its unit class: `Intermittent-photic-stimulation/10 Hz`, `Feature-amplitude/150 uV`. Every text placeholder is refused, `Description/#`, `Label/#` and the `Other-…/#` tags included.
- `Sensor-list` is the exception: its value lists canonical channel labels separated by `;` (`Sensor-list/F7;T7`). A raw acquisition label is refused.
- Reserved tags (`Onset`, `Duration`, `Definition` and the rest) and deprecated tags are refused. An event's timing belongs to the row.
- Groups are checked for balance and emptiness only.

Enforcement is at the API, as for every registered vocabulary ([annotations/vocabularies.py](../../annotations/vocabularies.py)).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `HEDSCORE_ALLOWED_BRANCHES` | `[]` | Top-level branches a code may use, by name. Empty accepts every branch. A project narrows it in its own settings; `manage.py check` reports a name the schema lacks as `hedscore.E001`. |

## API

Mounted at `/plugin/hedscore/api/v1/`.

| Method | Path | Returns |
|---|---|---|
| GET | `/policy` | The schema release and digest, and the allowed branches (every branch when none is configured) |

The picker's terms are not served. The viewer generates its index from the same release at build time, and the host application passes this policy to it, together with `GET /annotations/api/v1/vocabularies`, so the picker offers only what the validator accepts.

## Verb registry

Audit verbs this plugin emits. A new verb belongs here in the same commit as the endpoint that emits it: the [`audit-trail-completeness`](../../.review/agents/audit-trail-completeness.md) review agent matches each endpoint's verb against this table. The heading is matched exactly, so keep it as it is.

| Verb | Emitted by |
|---|---|
| `hedscore.policy.read` | `get_policy` |

## Tests

```
DJANGO_SETTINGS_MODULE=plugins.hedscore.settings_test pytest plugins/hedscore/tests/
```

## Gotchas

- **The digest is over the bytes in git.** The repository's `.gitattributes` normalises line endings to LF, which the vendored file already uses; re-vendoring a file with CRLF endings changes its digest on checkout.
- **No frontend half yet.** [enable_plugin.sh](../../scripts/enable_plugin.sh) adds the name to `VITE_PLUGINS`, and the development build warns that it names an unknown plugin until the picker's step registers one.
- **A rule here with no test behind it is a rule nobody holds.** No review agent knows the validator's rules, so the tests are the only thing that notices one being loosened.
