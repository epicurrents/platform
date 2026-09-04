# HED-SCORE — vocabulary registry in core, schema provider as a plugin

**Status: Phase 0 implemented 2026-09-02 (§3.1–3.3 — `Code.value` widened, `meta` schemas fixed, vocabulary registry in [annotations/vocabularies.py](../../annotations/vocabularies.py)); the rest is design.** Plans adoption of the HED-SCORE library schema ([Hermes et al., *Scientific Data*, 2025](https://www.nature.com/articles/s41597-025-05791-2)) as the platform's first external annotation vocabulary. Companion to [channel-deidentification-plan.md](channel-deidentification-plan.md) (Phase 4 is the consumer), [bids-export-privacy-design.md](bids-export-privacy-design.md) (where the tags eventually travel), and the EBRAINS/FAIR alignment note (`ebrains-fair-alignment.md`, kept in the archive repository's `docs/engineering-notes/` until the source material it refers to is public) (the registry below is that note's I2 dependency, arriving earlier than its Phase 2 assumed).

## TL;DR

1. **Split, don't choose.** The *mechanism* — a registry that validates `Code` writes against a declared vocabulary — is core annotations work and is already a ROADMAP item. The *vocabulary* — schema data, validator, picker UI — is a plugin. HED-SCORE passes the plugin test verbatim: a deployment would plausibly want ICD-10 or SNOMED beside it or instead of it.
2. **No new model, in core or in the plugin.** One `Code` row per annotation with `standard="hed"`, the HED string in `value`, the schema pin in `meta`. The existing `UniqueConstraint(content_type, object_id, standard)` matches HED's own "one annotation string per event" model exactly, so the constraint that would fight a worse design supports this one.
3. **Three small core changes.** Widen `Code.value`; let `CodeIn.meta` accept an object (it is typed `str | None` today, so the schema pin cannot be written through the API at all); add the registry plus a strict-mode setting.
4. **The payoff is a control, not a feature.** The ROADMAP's server-side vocabulary allowlist exists because a teaching project's data-protection position — the platform receives no patient personal data, coded annotations are recording-condition markers only — currently rests on client behaviour. HED-SCORE's `Modulator` branch is that vocabulary already written down by someone else: manual eye closure and opening, intermittent photic stimulation, hyperventilation graded by effort, sleep induction and awakening. Restricting such a project to one named branch of a published schema turns the position into something rejected at the API and describable to a supervisory authority.
5. **Tagging does not de-fingerprint anything on its own.** Attaching a HED-SCORE tag beside a vendor event name leaves the vendor name exactly where it was, and the annotations API still serves it. Phase 4 of the channel-de-identification plan needs the tag *and* the decision to stop serving `Event.name`; this note supplies the first half and says so.

## 1. The facts that shape the design

Measured against [HED_score_2.1.0.xml](https://github.com/hed-standard/hed-schemas/blob/main/library_schemas/score/hedxml/HED_score_2.1.0.xml) rather than taken from the paper, since the paper describes 1.0.0 and the schema has grown since:

| Property | Value |
|---|---|
| Current release | `version="2.1.0" library="score" withStandard="8.4.0"` |
| File size | 1.0 MB XML (also published as JSON, TSV and MediaWiki) |
| Terms in SCORE branches | 510 nodes across 10 top-level branches |
| Terms inherited from standard HED | 1233 nodes across 6 branches (`Event`, `Agent`, `Action`, `Item`, `Property`, `Relation`) |
| Licence | CC-BY-4.0 |
| Per-term content | hierarchical name, description prose, schema attributes, value placeholders (`#`) |

The SCORE branches are `Modulator`, `Background-activity`, `Critically-ill-patient-patterns`, `Episode`, `Feature-property`, `Interictal-activity`, `Physiologic-pattern`, `Polygraphic-channel-feature`, `Sleep-and-drowsiness`, `Uncertain-significant-pattern`. From 1.1.0 the library declares a partner standard version, so tags from both vocabularies live in one namespace and the `sc:` prefix earlier releases needed no longer applies.

Two consequences for us. The descriptions are shipped with the schema, so a picker UI gets its tooltips for free and needs no separate content authoring. And the branch granularity is fine enough to be a useful allowlist unit: `Modulator` alone is about twenty terms, which is the whole of a teaching project's needed vocabulary.

## 2. Core, plugin, or both

| Option | What it gets right | What breaks |
|---|---|---|
| All in core | One place to look; no plugin wiring | Every deployment carries a megabyte of clinical EEG vocabulary and a picker UI it may never use, and the platform takes a position on which terminology its users speak. The next vocabulary has to be bolted on beside it rather than registered |
| All in a plugin | Optionality is right | The plugin has no legitimate way to constrain a core model's writes. It would have to wrap or shadow the `/codes/` endpoints, which leaves the core endpoints reachable and the control bypassable — the exact failure the allowlist exists to prevent |
| **Split (recommended)** | The registry is a missing validation hook on a core model, useful to ICD-10 and to project-local vocabularies alike; the schema data and UI are optional and versioned separately | Two places to change when the contract between them changes. Bounded, and the contract is one function signature |

The split also matches how every other extension point in the repo already works: `register_export_extension`, `register_csv_subconverter`, `register_read_permission_extension` are all core registries called from an owning app's `AppConfig.ready()`, and `PluginConfig.ready()` is the same hook.

## 3. Phase 0 — core changes in the annotations app

### 3.1 Widen `Code.value`

`CharField(max_length=128)` does not hold a realistic HED annotation. A grouped seizure description with morphology and location runs several hundred characters. Move to `TextField`; the column is not indexed (only `standard` and the `(content_type, object_id)` pair are), so there is no index-width consequence, and widening does not disturb stored `content_hash` values because the hash covers the field's contents, not its declaration.

### 3.2 Let `CodeIn.meta` carry an object

`Code.meta` is a `JSONField`, but the Ninja input schema types it `meta: str | None`, so today the API can only write a JSON string into it. The schema pin below is an object, so this has to change regardless. Widen to the same union the annotation schemas already use for `value` (`dict | list | str | int | float | None`), which keeps every existing string-valued write valid — including the one in the model tests.

This is a pre-existing defect rather than something HED introduces; it is listed here because it is on the critical path.

### 3.3 The vocabulary registry

```python
# annotations/vocabularies.py
def register_vocabulary(
    standard: str,
    *,
    label: str,
    validator: Callable[[str, Any], None],
    version: str = "",
) -> None:
```

Called from any owning `AppConfig.ready()`. The validator raises `ValueError` with a message naming the offending term; the codes endpoints translate that into a 422. Consulted from `create_code` and `update_code` in [annotations/api/v1/ninja.py](../../annotations/api/v1/ninja.py), before the existing `transaction.atomic()` block, so a rejected write opens no transaction.

**Enforcement is at the API layer, not in `Code.save()`, and that is a choice.** A server-side `Code.objects.create` — ingest, a management command, a fixture — bypasses the registry. That is right for the threat model the allowlist item states, where the untrusted writer is a user reaching the API and the server-side paths are the platform's own code; it is wrong if the control is ever restated as "this deployment's database contains only allowlisted terms", which model-level enforcement would be needed to support. Write the narrower claim, and put the reasoning in the settings comment so the next reader does not have to re-derive which one is true.

Two enforcement modes, because the registry has to be adoptable without breaking deployments that already write codes:

- Default: an unregistered `standard` is accepted unvalidated. Existing behaviour, existing rows, and a project's own `epicurrents.<project>.<concept>` standards all keep working.
- `ANNOTATION_CODE_STRICT_VOCABULARY = True`: an unregistered `standard` is rejected. This is the setting that converts convention into control, and it belongs in a project's settings rather than in `common`.

The validator is a callable rather than a term list because a vocabulary's rules are not always membership. HED has value placeholders (`Physical-effort/#`) and group structure; ICD-10 has check-character rules. A list would model none of them and would have to be replaced the first time a second vocabulary arrived.

### 3.4 A convention for `standard`

The annotations README currently documents `epicurrents.<project>.<concept>` and instructs authors to keep the string out of the API's public surface. That rule was written for project-local labels, where the string is an implementation detail. It does not fit an external standard, where the string *is* the meaningful public identifier and hiding it behind a project-shaped endpoint would obscure what the row means.

Extend the convention rather than bending it: external standards use their own registry identifier (`hed`, `icd10`, `snomed`), project-local codes keep the `epicurrents.<project>.<concept>` form. The models module docstring already anticipates this — it names ICD-10, SNOMED and LOINC as the expected inhabitants. One paragraph in [annotations/README.md](../../annotations/README.md), one line in AGENTS.md's annotations cheat-sheet entry.

**The identifier is `hed`, not `hed-score`,** and this had to be settled rather than chosen freshly: [szcore-bids-integration.md](szcore-bids-integration.md) already specified `Code(standard="hed-score")` for detector output, and the EBRAINS/FAIR alignment note (`ebrains-fair-alignment.md`, kept in the archive repository's `docs/engineering-notes/` until the proposal it reads is public) listed both strings side by side. Two strings for one wire format is the I2 defect that note names, shipped by the notes that name it. `hed` wins because from SCORE 1.1.0 the library declares a partner standard version and the two vocabularies share one namespace, so a stored string may legitimately mix SCORE terms with standard-HED ones — a row labelled `hed-score` holding `(Sensor-list, …)` is mislabelled, and a later pure-HED use would need a second standard for an identical payload. Nothing is lost by the broader name: the `meta` schema pin records the library and version more precisely than a name could, and a detector's provenance goes in the same object. The SzCORE note has been updated to match.

### 3.5 `GET /annotations/api/v1/vocabularies`

Returns the registered standards with label and version, so the SPA can decide which editor to render without compiling in knowledge of which plugins the deployment enabled. Unauthenticated-equivalent content (it describes the deployment's configuration, not anyone's data), but routed through `_require_auth` like its neighbours, and logged as `annotations.vocabulary.list`.

## 4. Phases 1–2 — the `hedscore` plugin

| File | Contents |
|---|---|
| `apps.py` | `PluginConfig` subclass, `default = True`, `requires = ["annotations"]`; `ready()` loads the schema index and calls `register_vocabulary("hed", ...)` |
| `schemas/HED_score_2.1.0.xml` | Vendored schema, pinned by version and SHA-256, with the CC-BY-4.0 attribution file beside it |
| `schema.py` | Parses the XML once at startup into a term index: name to path, description, value-placeholder flag, deprecation flag |
| `validation.py` | The registered validator |
| `settings.py` | `HEDSCORE_SCHEMA_VERSION`, `HEDSCORE_ALLOWED_BRANCHES` (empty means the whole schema) |
| `urls.py` | `GET /plugin/hedscore/api/v1/schema` — the term tree for the picker |
| `frontend/src/plugins/hedscore/` | Tag picker component, registered through the existing `ViewerPlugin` contract. Needs the full registration set, not just the directory: a `__PLUGIN_HEDSCORE__` define in vite.config.ts, its declaration in src/vite-plugins.d.ts, a guarded push in plugins/active.ts, and the name in `KNOWN_PLUGINS` — a partial registration produces a silently pluginless build |

**No models, deliberately.** A plugin model with a user FK would pull in the erasure registration, the subject-export classification and a migration set, for data that is a static vocabulary file. Keeping the plugin model-free keeps its compliance surface empty. If an editable vendor-mapping table is wanted later (Phase 4), that is the point to reconsider, and the reconsideration should be explicit.

**No runtime fetch of the schema.** It is vendored, pinned and hashed. A clinical deployment should not make an outbound request to GitHub to decide whether an annotation is valid, and a validator whose answer depends on network reachability is not a control.

Startup cost: parsing a 1 MB XML into a dict at `ready()` is tens of milliseconds and happens once per worker. If it ever matters, the parsed index serialises to JSON at build time; do not optimise it before measuring.

## 5. What goes in the tag string, and what does not

`standard="hed"`, `value` = the descriptive tag string, `meta` = `{"schema": {"library": "score", "version": "2.1.0", "with_standard": "8.4.0"}}`.

The pin is not decoration. A term valid in 2.1.0 may be deprecated in 3.0, and without knowing which schema a row was written against there is no way to re-validate history or to report what an upgrade would invalidate.

Two things stay out of the string:

- **Onset and duration.** `Event.timestamp` and `Event.duration` already own them, and HED's own model puts them in the events file columns rather than the tag string. Duplicating them creates two sources of truth that drift the first time an annotation is edited.
- **Raw channel labels.** HED can express `(Sensor-list, F7)`, and a tag string is an unguarded place to write one. Site de-identification Phases 1–3 spent their effort removing acquisition-site naming conventions from labels; a tag string carrying `Fp1-A1` puts the fingerprint back through a surface no de-identification pass inspects. The rule is that only `SignalInfo.canonical_label` values may appear in a sensor tag, enforced in the plugin validator, and it needs a test that a raw source label is rejected.

The second point generalises: the tag string is a new free-text-shaped field reaching a serving surface, so it gets the same scrutiny as any other. That it is validated against a schema constrains the *tags*, not the values interpolated into them.

## 6. What this does and does not do for de-identification

Does: gives converter-derived events a canonical target vocabulary, so the vendor taxonomy stops being the only description of what happened; gives a teaching project a closed, externally curated term set that a supervisory authority can be pointed at; makes the annotation layer's semantics machine-readable for export without exporting free text.

Does not: make a tagged annotation non-personal. A tag from `Episode` or `Interictal-activity` is a clinical finding about the recording subject — more precisely stated than free text, not less identifying. Only the branch restriction makes a deployment's annotations condition-markers-only, and that is a per-project configuration decision, not a property of adopting HED.

## 7. Phase 3 — a project adopts it as a control

```
# projects/<name>/settings.py
ANNOTATION_CODE_STRICT_VOCABULARY = True
HEDSCORE_ALLOWED_BRANCHES = ["Modulator"]
```

Plugin settings merge before the active project's (`common < plugins < project < .env`), so a project overriding a plugin's default branch list is the loader working as designed rather than a special case. Every `Code` write must then name a registered standard, and every `hed` value must resolve inside `Modulator`. The vocabulary that remains is manual eye closure and opening, intermittent photic and auditory stimulation, hyperventilation with four effort grades, sleep deprivation and induction, awakening, and the medication modulators — which is the condition-marker set such a project states, plus a more precise version of it.

Strict mode also forces a decision such a project has to make anyway: its own `epicurrents.<project>.<concept>` standard becomes unregistered under strict mode, so the project registers that vocabulary through the same registry — a small win in its own right wherever the values were validated in an endpoint and nowhere else.

The remaining half of the ROADMAP item — whether free-text annotation bodies are permitted at all for such a project — is untouched by this note and stays open.

## 8. Phase 4 — ingest mapping, and the thing it does not fix

A mapping from converter-derived event strings to HED-SCORE tags, applied when [recordings/tasks.py](../../recordings/tasks.py) creates events at ingest, shipped as a versioned data file in the plugin.

**The structural precondition comes first: today there is nothing for a translated code to attach to.** Neither ingest path creates `Event` rows — the EDF+ TAL parse (`_save_edf_results`) and the converter sidecar handler ([recordings/converters/sidecar.py](../../recordings/converters/sidecar.py)) each persist one `Annotation` row holding a JSON blob of events, and `Code` attaches only to `Event`, `Interruption`, or `Label`. So the phase carries a storage decision with two tiers. The cheap tier keeps the blob and rewrites each event's label to the vocabulary term at ingest, capturing the vendor string author-private the way `SignalInfo.source_*` captures raw channel labels — this buys the de-fingerprinting and nothing else. The full tier promotes ingest events to real `Event` rows carrying `Code(standard="hed", ...)`, which is what detector benchmarking actually wants — queryable, per-event coded ground truth — at the cost of a schema-shaping change to both ingest paths. The tiers are not exclusive: the cheap one can ship first and the promotion later, since the blob preserves everything the promotion needs.

**The mechanism is a translation registry, and it is a different object from the Phase 0 validator.** `validate_code(standard, value, meta)` answers "is this term legal in this vocabulary"; a mapping from a Nicolet event type to a HED-SCORE term is a per-vendor table with a direction. The shape that fits the platform's existing registry patterns: `register_event_translation(standard, mapper)`, where the mapper takes the converter's typed source event (`type`, `label`) and returns `(value, meta)` or `None` for unmapped, registered from the plugin's `AppConfig.ready()` beside its validator and consulted from the sidecar persistence seam (after schema validation, before the write — the seam exists in `save_sidecar_events` already). Server-side ingest deliberately bypasses the *validation* registry, so the translation hookup is explicit rather than inherited; a translator that wants its output validated calls `validate_code` itself. Phase 0 as shipped accommodates all of this without change — recorded 2026-09-02 so the decision not to pre-build the translator is visibly deliberate rather than an oversight.

The caveat has to be written into the phase rather than discovered during it: **attaching a tag does not remove the vendor string.** `Event.name` still holds it and the annotations API still serves it verbatim to every grantee. Closing the leak needs the second decision — that a mapped event stops serving its raw name, with the raw string captured author-private the way `SignalInfo.source_*` already captures raw channel labels. That decision has a real cost (unmapped events, and existing rows whose names are already out), so it is its own phase, not a corollary of this one.

Mapping coverage also cannot be assumed complete. An unmapped vendor string must stay unmapped and visible rather than being coerced into the nearest tag; a silently wrong clinical tag is worse than an untagged event.

## 9. Phase 5 — downstream, and the hedtools question

BIDS export gains a natural place for these: `_events.json` sidecar entries plus `HEDVersion` in `dataset_description.json`. Worth noting against [bids-export-privacy-design.md](bids-export-privacy-design.md), whose rule is that annotation *text* is never exported — a closed-vocabulary tag is not free text and cannot carry a name or a date, so HED codes are precisely the annotation payload that could travel where the body cannot. That is a change to that note's table and should be argued there, not assumed here.

**Recommendation: do not take the `hedtools` dependency in Phases 0–2.** The package is MIT-licensed and is the correct long-term answer for validating third-party HED strings, but its closure includes pandas, openpyxl, inflect, typeguard and portalocker. Plugin dependencies have no per-plugin lock mechanism — the platform's requirements file says so explicitly at the pydicom entry, because `EPICURRENTS_PLUGINS` is a list where `EPICURRENTS_PROJECT` is one name — so a plugin's Python dependencies go into the platform closure. pandas is today a project dependency rather than a platform one, so taking hedtools would promote it into the platform lock and require regenerating every project lock behind it. The version ranges are likely to intersect (`hedtools` wants `>=2.2.3,<4`), so the cost is the relock and the wider default image rather than a conflict.

For Phases 0–2 that cost buys little, because every stored tag is produced by our own picker from our own vetted index: membership plus structural validation against the vendored schema is the actual control. Revisit at the point where the platform ingests HED strings it did not author — a BIDS import, a third-party sidecar — which is where full validation starts earning its closure.

## 10. Risks and residual

- **Retro-tagging is an audit event.** Adding a `Code` recomputes the parent's `content_hash` by design, so a bulk pass over existing annotations is a mass modification with an `ObjectChangeLog` row each. Plan it as one, in a `with_system_activity` scope, rather than as a backfill migration.
- **Schema upgrades are additive but not free.** Deprecations need a report of affected rows, not an automatic rewrite; a stored pin plus a management command that re-validates history against a newer schema is the shape.
- **The paper's own limits carry over.** HED-SCORE targets scalp EEG graphoelements; iEEG needs care because signal properties differ, and neonatal terminology is not yet in the schema. A deployment doing either should not read branch coverage as term coverage.
- **Strict mode is a breaking change per deployment.** Any existing writer of an unregistered standard starts getting 422s. It is opt-in per project for that reason, and adopting it needs an inventory of what that project actually writes.
- **CC-BY-4.0 attribution travels with the file.** Vendoring the schema means shipping its licence and attribution, and a distribution tarball has to carry them too.

## 11. Sequencing

1. **Phase 0** — core: `value` width, `meta` type, registry, strict-mode setting, `/vocabularies`, convention documented. Tests: unregistered-standard behaviour in both modes, validator rejection shape, existing string-valued `meta` still accepted.
2. **Phase 1** — plugin: vendored schema, index, validator, schema endpoint. No UI. Tests: index term count against the pinned file, branch restriction, canonical-label-only sensor rule, missing-schema-file failure is loud.
3. **Phase 2** — frontend picker, wired into annotation editing.
4. **Phase 3** — a project's strict mode plus branch restriction; the project registers its own vocabularies.
5. **Phase 4** — ingest mapping, and separately the decision to stop serving mapped vendor names.
6. **Phase 5** — BIDS sidecar emission; reconsider `hedtools`.

Phases 0–2 are independently useful and land the FAIR interoperability argument; Phase 3 is where the data-protection value arrives; Phases 4–5 depend on decisions outside this note.

Review agents to expect: `gdpr-compliance` on the registry and on any plugin model that appears later, `phi-exposure` on the schema endpoint and on anything that widens what a `Code` serialises, `documentation-style` on the README and AGENTS.md edits. `load-bearing-diff-reviewer` does not fire — none of the touched files carry the marker — which is worth knowing rather than relying on, since the canonical-label rule in section 5 is exactly the kind of invariant that has no agent behind it.
