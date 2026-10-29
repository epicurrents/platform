# hedscore

Registers HED with the SCORE library ([Hermes et al., *Scientific Data*, 2025](https://doi.org/10.1038/s41597-025-05791-2)) as the `hed` annotation-code vocabulary. Findings, what a reader concludes from the signal, are coded in it; what happened during the recording stays in the platform's own `epicurrents.biosignal` and `epicurrents.eeg` sets ([annotation-event-vocabulary.md § 9](../../docs/engineering-notes/annotation-event-vocabulary.md)). Design in [hed-score-integration.md](../../docs/engineering-notes/hed-score-integration.md).

The plugin has no models, so it adds nothing to the erasure or subject-export registries. Enable it with `scripts/enable_plugin.sh hedscore`.

## The schema

[schemas/HED_score_2.1.0.xml](schemas/HED_score_2.1.0.xml) is the merged release of SCORE 2.1.0 with HED 8.4.0, vendored from [hed-standard/hed-schemas](https://github.com/hed-standard/hed-schemas/tree/main/library_schemas/score/hedxml) unmodified, under CC-BY-4.0 ([schemas/LICENSE](schemas/LICENSE), copyright the HED contributors). `SCHEMA_PIN` in [schema.py](schema.py) names its version and SHA-256. `ready()` checks the digest and parses the file into an index of 1527 terms, 396 of them SCORE's; a missing or altered file raises `ImproperlyConfigured` and the platform does not start. The schema is never fetched at runtime.

A new release is vendored beside the old one and the pin moved in the same commit. Rows keep the release they were validated against in their `meta`, so history can be re-checked against a newer schema.

## The validator

A `hed` code's `value` is a HED string and its `meta` is the schema pin, `{"schema": {"library": "score", "version": "2.1.0", "with_standard": "8.4.0"}}`, exactly. The rules are narrower than HED's own, because a code's value is served to readers under a de-identifying grant and must not carry typed text:

- Tags in their short form, spelt as the schema spells them. Long forms and extensions are refused.
- A value only where the placeholder's value class is numeric and `NUMERIC_DOMAINS` in [validation.py](validation.py) declares a domain for it: one unit, a range and a number of decimals, as below. The number is written plainly in its one spelling, with no sign, exponent, leading zero or trailing fractional zero, so a reading cannot be padded or rescaled to carry other digits: `Intermittent-photic-stimulation/10 Hz`, `Feature-amplitude/150 uV`. A numeric placeholder with no domain is refused, `Age/#` among them, and so is every text placeholder, `Description/#`, `Label/#` and the `Other-…/#` tags included.
- `Sensor-list` is one exception: its value lists canonical channel labels separated by `;` (`Sensor-list/F7;T7`). A raw acquisition label is refused.
- `ID` is the other: its value is the number of a finding group, 100 to 999 without leading zeros (`ID/104`), which the viewer's picker writes on every occurrence of one finding within a recording. 0 to 99 are held back for a use not yet defined, and refused until it is, so no stored row takes on that meaning before it exists.
- Reserved tags (`Onset`, `Duration`, `Definition` and the rest) and deprecated tags are refused. An event's timing belongs to the row.
- A string holds at most 64 tags and 16 numeric values within its 2000 characters, and a sensor list names a channel once.
- Groups are checked for balance and emptiness only.
- A string is stored in one spelling. `canonicalise_hed`, registered as the vocabulary's `canonicalise` and applied before the validator, joins the tags of a group with `, ` and no other whitespace, drops a tag or group repeated within its group, and replaces a group of one member by the member, so `( Spike,Spike , (Left) )` is stored as `(Spike, Left)`. Tags keep their written order: the viewer's picker writes one order, finding first, and the readable form follows it, which leaves the order the one spelling choice a writer still has.

| Placeholder | Unit | Range | Decimals |
|---|---|---|---|
| `Feature-amplitude`, `RPP-absolute-amplitude` | `uV` | 0–10 000 | 0 |
| `Feature-frequency` | `Hz` | 0–1 000 | 2 |
| `Intermittent-photic-stimulation`, `Myoclonus-rhythmic` | `Hz` | 0–100 | 1 |
| `Number-of-RPP-phases` | — | 1–3 | 0 |
| `Episode-event-count` | — | 0–10 000 | 0 |
| `Apnea`, `Hypopnea` | — (seconds) | 0–600 | 0 |
| `Apnea-hypopnea-index` | — (per hour) | 0–200 | 1 |
| `Tachypnea` | — (per minute) | 0–150 | 0 |
| `Oxygen-saturation`, `Percentage` | — (per cent) | 0–100 | 0 |
| `ECG-QT-period` | `ms` | 0–2 000 | 0 |
| `ECG-asystolia` | `s` | 0–600 | 1 |
| `Time-interval` | `s` | 0–3 600 | 2 |

Every SCORE numeric placeholder has a domain; of the HED standard's own numeric properties only `Time-interval` and `Percentage` do, since the rest describe stimuli, displays and subjects rather than a signal. A new release's numeric placeholder is refused until it is given one, and [test_validation.py](tests/test_validation.py) checks each domain's unit against the placeholder's unit class in the pinned schema.

These rules make `hed` a *confined* vocabulary, which the plugin registers it as, and not a closed one: every part of a value comes from a declared domain and none of it is typed text, but a writer still chooses a reading, and a domain bounds how much a reading can say rather than removing it. A gate taking codes from a known writer, such as a teaching project's reviewer set, may accept it; ingest, which takes codes from files, requires a closed vocabulary and takes no `hed` code ([annotations/README.md → Vocabulary registry](../../annotations/README.md#code--standardised-classification)). The registration's `write_meta` returns the pin, which a server-side writer stamps on a code it creates.

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

The picker's terms are not served. eeg-module generates its index from this plugin's vendored file, and the SPA passes this policy to it, together with `GET /annotations/api/v1/vocabularies`, so the picker offers only what the validator accepts.

## Frontend

[frontend/src/plugins/hedscore/](../../frontend/src/plugins/hedscore/) contributes a provider at `modules.eeg.hedScore.policy` in the viewer setup and nothing else; the picker is the viewer's. The viewer calls it when the picker first opens. It answers `null` when the vocabulary listing has no `hed`, which hides the picker, and otherwise the pin and `allowed_branches`; the picker disables itself when the pin is not the release its index was generated from. A project sets its quick palette beside it, as a list of short-form tags at `modules.eeg.hedScore.palette` in its own `extraSetup`, and the two merge.

A picked finding is stored on the viewer event as `codes.hed`. The viewer saves nothing to this server: a project that saves the event through the annotations API sends the code in the event's inline `codes` with `meta` set to the pin, which eeg-module's `hedSchemaMeta(index.schema)` builds.

## Verb registry

Audit verbs this plugin emits. A new verb belongs here in the same commit as the endpoint that emits it: the [`audit-trail-completeness`](../../.review/agents/audit-trail-completeness.md) review agent matches each endpoint's verb against this table. The heading is matched exactly, so keep it as it is.

| Verb | Emitted by |
|---|---|
| `hedscore.policy.read` | `get_policy` |

## Tests

```
DJANGO_SETTINGS_MODULE=plugins.hedscore.settings_test pytest plugins/hedscore/tests/
```

[test_viewer_index.py](tests/test_viewer_index.py) compares eeg-module's term index with this plugin: the release and digest it names, its terms and units against the plugin's parse, the standard subtrees it carries and the branch each validates under, and every numeric term in its default unit against the validator. It skips without the viewer checkout. Moving `SCHEMA_PIN` fails it until the index is regenerated from the new file.

## Gotchas

- **The digest is over the bytes in git.** The repository's `.gitattributes` normalises line endings to LF, which the vendored file already uses; re-vendoring a file with CRLF endings changes its digest on checkout.
- **The artifacts are under `Property`.** SCORE 2 moved them into the HED standard, so `HEDSCORE_ALLOWED_BRANCHES` naming SCORE branches alone refuses every artifact tag, and the picker hides its artifacts group. Listing `Property` admits them, and with them the standard's other property tags; the text-placeholder rule still holds for those.
- **A rule here with no test behind it is a rule nobody holds.** No review agent knows the validator's rules, so the tests are the only thing that notices one being loosened.
