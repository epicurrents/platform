# Channel maps

A channel map is an exact table of the labels one source writes into its EDF or BDF headers, and the stored channel name each stands for. It is chosen per upload or import, so a map can safely name labels that would be meaningless or ambiguous as a general rule.

The shipped matcher in [channel_rules.json](../processors/channel_rules.json) stays generic English and international. A language's, vendor's or laboratory's spellings go in a map. A map works on any recording the platform ingests, whatever its modality.

This folder holds examples, none of them active until configured:

| File | Source |
|---|---|
| [italian.json](italian.json) | Italian spellings of common sleep channels |
| [nox.json](nox.json) | A Noxturnal export |
| [finnish.json](finnish.json) | A Finnish sleep montage |

The `nox` and `finnish` maps were written from real headers. `TCM4` and the `Pallea` channels in `finnish` are inferred from their names, units and transducers, not confirmed by the source. Review a map against your own files before configuring it.

## How the platform uses a map

1. **Configuration.** `RECORDING_CHANNEL_MAPS` in a project's settings gives each map a key. A relative path resolves against `BASE_DIR`. A key is lower-case letters, digits, `-` and `_`, and is listed to every signed-in account, so name a kind of source rather than a site.

    ```python
    RECORDING_CHANNEL_MAPS = {
        "nox": "recordings/channel_maps/nox.json",
    }
    ```

2. **Validation.** `manage.py check` refuses a map it cannot read or parse, and a bad key (`recordings.E004`).
3. **Selection.** An uploader picks the map on the upload page, which offers the select only when maps are configured. The API takes `channel_map` on `POST /upload`, and `GET /channel-maps` lists the keys. For a bulk import, use `import_recordings --channel-map <key>`; a resumed job keeps the map it was created with. A map applies to every file of the upload or job. `--link` refuses a map, because a linked file keeps its source labels and every path reading it would classify them again with no map in hand.
4. **Ingest.** The map is consulted first for each channel, before the site tables and the shipped rules, and it decides any label it holds, including a label the shipped rules would otherwise read differently. A label it does not hold falls through to those. A channel nothing names is stored as `MISC_<n>`.
5. **Serving.** The stored file carries only stored names, which resolve to themselves, so nothing after ingest needs the map. The source label stays in the author-private `source_label` field.
6. **Record.** The READY audit row seals the map's key and a digest of its file, and `deidentification_report` names the key. No response says which map a recording went through.

Three consequences for anyone editing a map:

- A process keeps a map for its lifetime, so restart the web and Celery workers after an edit.
- The digest covers the whole file, so changing `description` also changes it.
- An edit does not touch stored recordings.

An upload checks only that the key is configured. A map the worker cannot read fails that recording rather than ingesting it unnamed. Mechanics in [recordings/README.md → Channel maps](../README.md#channel-maps).

## File structure

```json
{
    "version": 1,
    "description": "Example channel map for Italian language",
    "rules": [
        {"canonical": "Resp nasal", "label": ["Flusso"], "transducer": ["press", "cannula"]},
        {"canonical": "Resp nasal", "label": ["Flusso"], "unit": ["cmH2O", "hPa"]},
        {"canonical": "Resp", "label": ["Flusso"]},
        {"canonical": "Thorax", "label": ["Torace"]}
    ]
}
```

| Top-level key | Required | Meaning |
|---|---|---|
| `rules` | Yes | The rules, tried in order. The first that holds names the channel. |
| `version` | No | The format version, `1`. For readers; the parser does not read it. |
| `description` | No | What the map is for. For readers; the parser does not read it. |

| Rule key | Required | Meaning |
|---|---|---|
| `canonical` | Yes | The stored name. It must be a name the platform already stores, and the signal type comes from that name. Rules take no `type`. |
| `label` | Yes | The labels this rule answers to, as a non-empty list. |
| `unit` | No | Canonical units ([processors/units.py](../processors/units.py)), one of which the channel must have. |
| `transducer` | No | Words, one of which the transducer field must contain, in either case. |

Any other key is refused.

**Labels are exact.** A label is compared with the whole header label, ignoring case and surrounding spaces and nothing else: no modality prefix or reference is removed, and separators count. `Leuka` and `Leuka ref` are two entries, and `1-2` does not match `1 2`. A blank label is refused, as is one longer than the 16-byte EDF label field.

**Order matters when a label has conditions.** A rule whose `unit` or `transducer` does not hold is skipped, so put conditional rules before the unconditional rule for the same label, as above. With both conditions on one rule, both must hold. If no rule holds, the channel falls through to the site tables and the shipped rules.

## Stored names

A map chooses among the names the platform stores and cannot add one, which keeps a source's own naming out of every stored file.

| Kind | Names |
|---|---|
| EEG | 10-10 electrodes, plus `A1`, `A2`, `M1`, `M2`, as listed in `_TEN_TEN_ELECTRODES` in [processors/channel_labels.py](../processors/channel_labels.py) |
| EOG | `LOC`, `ROC`, `EOG` |
| EMG | `EMG`, or `EMG/<site>` for `Chin`, `ChinRef`, `Leg`, `LegL`, `LegR`, `ArmL`, `ArmR`, `Diaph` |
| ECG | `ECG`, `ECGL`, `ECGR`, and the lead names `I` to `III`, `aVR`, `aVL`, `aVF`, `V1` to `V6` |
| Everything else | Every `canonical` in [channel_rules.json](../processors/channel_rules.json) |

A name outside these fails `manage.py check` with `is not a name the platform ships`. A genuinely new kind of signal needs a new name in the shipped table, which is a change to the de-identification pass.

## EEG recordings

Map laboratory or device labels onto electrodes the same way. Labels that follow 10-10 already resolve and need no entry, including `Fp1-Ref`, the old names `T3` to `T6`, and bipolar pairs such as `Fp1-F7`. Stored EEG channels keep the platform's canonical order, and the samples move with the name, so a map can also correct a known swap: a rule with `C3` as its label and `C4` as its `canonical` stores those samples as `C4`.

The limits come from a stored name being a single electrode:

- **A map cannot name a derivation.** `Fp1-F7` and `C3-M2` are refused as a `canonical`. Do not map a local bipolar label to its first electrode: `Bip1-Bip2` mapped to `Fp1` is stored as `Fp1`, and the file then reads as if that channel were referential. Leave it unmapped, so it is stored as `MISC_<n>`.
- **The reference is usually lost.** A label the shipped rules cannot parse, such as `Ch1-A2` mapped to `C3`, stores as `C3`, even in a sleep study, which otherwise keeps derivations like `C4-A1`.
- **Use current names.** `T3` is refused as a `canonical`; use `T7`.
- **One electrode, one channel.** If two labels in a file reach the same electrode, the first keeps the name and the rest are stored as `MISC_<n>`. Other signal types may share a name.
- **Only the shipped montage.** Intracranial contacts (`RAH1`) and high-density labels (`E17`) have no stored name, so a map cannot name them and they stay `MISC_<n>`.
