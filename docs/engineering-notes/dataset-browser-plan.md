# Dataset browser — plan

**Status:** planned 2026-11-18, with the scope and the [decisions](#decisions) settled the same day. Phase 1, the browse endpoint, was built the same day. Phase 2 was built the same day in four steps: the schema, the values, their endpoints and the browser's subject column; the sidecar key in the submission gate; the import command; and the displayed fields on a gated dataset with the anonymity report's second sizing. Phase 3, the tags per row with their batch endpoint and the code summary, was built the same day.

A dataset is browsed today as the folder tree on its page, one row per member, carrying only its name. A reader choosing recordings to study or teach from needs more than a name: what was recorded, how, and from whom, at the level the dataset declares. This note plans a browser for every dataset: a table of its recordings with ingest metadata, the dataset's own subject fields and a few tags, and a details drawer per row with the recording's codes. It is a platform feature. The educational review project is the first that needs it, and the rules that apply only to a release-gated dataset are marked as such.

## Settled scope

- **Every dataset gets the browser.** The rules for a release-gated dataset (the class-size condition on displayed fields, small-count suppression, no ordering by arrival) apply only where `Dataset.release_gated` is set. Elsewhere the dataset's readers see what its author chose to declare.
- **Tags are shown for recordings only,** at most three per row; the drawer lists the rest. Dataset-level tags are out of scope.
- **Subject fields are per recording.** No key links recordings of the same person. Linking them is a re-identification aid of its own, and nothing the browser does needs it.
- **Subject values arrive from two places:** the sidecar of a pool submission, and, for a dataset re-released from a published one, the original dataset's subject table. Dataset managers can also set them by hand.

## What exists

| Need | State on 2026-11-18 |
|---|---|
| Ingest metadata | `RecordingMeta` and `SignalInfo` hold duration, signal count, channel layout, per-channel sampling rates and the de-identification version; `Recording` holds modality and `public_source`. The item listing returns none of it, and the recordings listing cannot filter by dataset or by a list of hashes, so a table today costs one request per row. |
| Release gate | The item listing in [library/api/v1/ninja.py](../../library/api/v1/ninja.py) applies it: unreleased members hidden from readers who do not manage the dataset, share tokens refused, `added_at` replaced by the release month, name ordering, sequential ids nulled, members gated in other datasets hidden. [library/release.py](../../library/release.py) holds the rules. |
| Subject metadata | None. `DatasetMeta` has only the licence pair, and its docstring names a subject-group description as a later column. The pool sidecar reaches the profile's `ingest()` and is not kept. `subject` and `patient` are forbidden sidecar keys at any depth. [bids-export-privacy-design.md](bids-export-privacy-design.md) treats a participants table as a PHI sink. |
| Tags | `Tag` and `TaggedItem`, with reach decided by `visible_tag_ids` in [library/tag_scope.py](../../library/tag_scope.py), which walks every tagged item. No endpoint returns the tags of an object or of many objects, and the frontend has no tag client. |
| Codes | `Code` rows hang off events, interruptions and labels, at most one per standard per row. They are served only inline on the annotation listings; nothing summarises a recording's codes. `GET /annotations/api/v1/vocabularies` lists the registered vocabularies. Under a de-identifying grant a code keeps `standard` and `value` and loses `meta` ([annotations/redaction.py](../../annotations/redaction.py)). |
| Frontend | No table component, and Web Awesome 3.13 has none either; its native stylesheet styles a plain `<table>` (`wa-zebra-rows`, `wa-hover-rows`, `wa-tabular-nums`). The only drawer is the recording details drawer in [HomeView.vue](../../frontend/src/views/HomeView.vue). [DatasetView.vue](../../frontend/src/views/DatasetView.vue) never pages its item listing, so it shows the first 50 members and no more. |

## Design

### 1. Browse endpoint

`GET /api/v1/library/datasets/{dataset_id}/browse/` returns one page of rows with server-side sorting, filtering and paging.

- **One membership query.** The gating and hiding the item listing applies move into a helper that both endpoints call, so the browser cannot diverge from the listing. Rows are recordings only; media stay in the item listing.
- **The row:** `hash`, `display_name`, `modality`, `duration`, channel count (non-annotation signals), the highest sampling rate (and the lowest, where they differ), `channel_layout`, `deidentification_version`, the visible date (release month on a gated dataset, otherwise `_visible_created_at`), `public_source`, up to three tags, and the subject fields.
- **A new `Out` schema**, so the key set is a decision: it joins the per-reader key sets in [recordings/tests/test_grantee_visible_shape.py](../../recordings/tests/test_grantee_visible_shape.py) and the given-data tables in [anonymisation-compliance.md](../anonymisation-compliance.md). No sequential id, no `stored_hash`, nothing author-private.
- **Sort keys** are an allowlist: name, modality, duration, channel count, sampling rate and each subject field. On a gated dataset a reader who does not manage it never gets an order by arrival; the release month is allowed because it is what they are shown, and ties break by name.
- **Columns come from joins.** One subquery per derived column over `RecordingMeta` and `SignalInfo` is enough for the dataset sizes in view. An index row per recording, written at the end of ingest, is the fallback if it is not.

### 2. Subject fields

- **A schema per dataset,** a JSON column on `DatasetMeta`: an ordered list of fields, each a key, a label and a fixed list of choices. An age is declared as bands, a diagnosis as categories. On a dataset with a pool, the schema locks when the pool does. No free-text field and no exact number, so a value names a category rather than describing a person, the same line the annotation-text rule draws for a code's value.
- **Values per member,** on a new `DatasetItemSubject` row (one per `DatasetItem`, cascading with it), validated against the schema at every write. A schema change that removes a choice still in use is refused.
- **From a pool submission:** the sidecar carries the values under a top-level `subject_fields` key. `subject` stays forbidden; this key admits only the declared keys and choices, and the gate refuses an unknown key or an undeclared value before anything is written, as it refuses any other sidecar fault. The pooled ingest writes the row with the recording, so it inherits the ingest's rule that nothing joins the recording to its ledger.
- **From a published dataset:** a management command reads the original's subject table, maps its columns and values onto the schema through a mapping file (exact ages to bands, local codes to categories), matches rows to recordings by the original file name, and drops every column the schema does not declare. Unmapped values are counted and reported, never stored. The audit row carries counts and the dataset, not the path or the values (AGENTS.md → Activity metadata carries identifiers, never names).
- **By hand:** dataset managers edit a row's values in the drawer, through a PATCH endpoint behind `_require_auth`.
- **The audit trail does not keep the values.** A value is about a data subject, and the trail is permanent: `DatasetItemSubject` registers its value field with `register_masked_fields(..., withhold=True)`, so the trail records that values changed and not what they were. Withdrawing a recording deletes its membership and the row with it.

### 3. Displayed fields on a gated dataset

The subject fields the table shows are the quasi-identifiers a reader can combine. On a gated dataset:

- The schema marks the fields that may be displayed, and a displayed field must be part of the equivalence class. The platform supplies `subject_class_key(item)`, the tuple of displayed values, and joins it to the registered class function's key wherever it counts classes, in the release gate and the anonymity report, rather than leaving the project's function to return it: that function takes a recording, not a membership, so it cannot tell which dataset's values to read. Once a dataset has a release, which fields are displayed is fixed.
- The anonymity report in [library/reports.py](../../library/reports.py) also sizes the classes over the displayed fields directly, whatever function the project registered or none, so a selector that releases without a class function still shows up in the report rather than nowhere.
- Facet and filter counts below the release's k are shown as "fewer than k". Filters on displayed fields alone cannot isolate a member once every class meets k; combined with a tag or a code they can, which is why the counts are suppressed rather than trusted.

### 4. Tags per row

A batch call answering "the tags on these objects", scoped by the reader's reach. `visible_tag_ids` walks every tagged item in the deployment, so the browser uses `tags_on_objects`, its form bounded to the page's objects: a tag on an object the reader can read is in their reach, so only the page's tagged objects are checked. Per row: curated tags before the reader's own, then the rest, each by name, three at most, with a count of all of them; `GET /api/v1/library/recording-tags/` lists them all for the drawer. A reader holding a share token without an account gets no tags, as the tag endpoints take no share token.

### 5. Codes in the drawer

`GET /annotations/api/v1/recordings/{hash}/codes/` returns the distinct `(standard, value)` pairs across the recording's events, interruptions and labels, with a count each, grouped by vocabulary. It never returns `meta`, so it is outside the withholding rule by construction rather than by a check. It resolves access with `get_read_access_result` and applies `_hidden_for_caller` and the release gate like every recording surface. The drawer lists the deployment's registered vocabularies and marks the ones present on the recording; HED-SCORE strings are shown as the canonical short form the hedscore plugin stores. An index for filtering the table by code waits until that filter is asked for.

### 6. Frontend

- **`DataTable.vue`:** a plain `<table>` on Web Awesome's native styles, inside the usual centred scroller; sortable headers that emit a sort key and set `aria-sort`; a column configuration (key, label, formatter, sortable); `wa-pagination`; `wa-skeleton` rows while loading; row click and keyboard focus. Sorting and filtering stay on the server, which a gated dataset requires anyway.
- **`RecordingDetailsDrawer.vue`:** the drawer body moves out of HomeView so both views share it. The browser adds the tag list, the subject fields, the code summary and an "Open in viewer" action.
- **A Browse tab on the dataset page,** beside the folder tree, which also gains paging and so stops at 50 no longer.

## Phases

1. The browse endpoint, the shared membership helper, the schema and its key-set test, the assessment rows.
2. Subject schema and values: the model and migration, the sidecar key in the submission gate, manager editing, the import command, the class-key helper and the anonymity report's second sizing, the GDPR and assessment documents.
3. Batched tags and the code summary endpoint.
4. `DataTable`, the drawer extraction, the Browse tab and the paging fix.
5. Filters and facets with suppression below k, and a catalogue page listing the datasets a reader can see with at least one release.

Phases 1, 2 and 5 touch the release gate and the served shape, so each runs the `phi-exposure`, `anonymisation` and `gdpr-compliance` agents; every new endpoint also runs `audit-trail-completeness`, and every new write `csrf-coverage`.

## Decisions

Settled on 2026-11-18:

- **The schema lives in a column on `DatasetMeta`,** which keeps the dataset table untouched, as that model intends.
- **A schema locks with the pool,** as the pool's profile does. Adding a field is harmless, but removing or rebanding one changes the classes a past release was sized on.
- **The platform does not say which item of a published dataset a recording came from.** A reader gets nothing beyond the order of the items. The import still has to pair each row of the original's subject table with a recording, and does so by the original's file name, which reaches the platform only in the author-private `original_name`. It matches on it once and stores nothing of it. Pairing by position alone is not enough: one file missing from the import shifts every value after it onto the wrong recording, with nothing to show it happened.
- **A CSV export of the table comes later.** It would be a new path for subject values out of the platform, and it follows the export rules, the `pseudonymised_personal_data` header included.
