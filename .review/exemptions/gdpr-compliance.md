# gdpr-compliance exemptions

Personal-data-shaped inputs the [gdpr-compliance agent](../agents/gdpr-compliance.md)
must not flag. Each entry names the model (or model.field) and the reason it is
exempt from the C1 (erasure registration) / C3 (retention path) checks. The
agent consults this file before flagging; anything not listed gets flagged.

| Model / field | Exempt from | Reason |
|---|---|---|
| `recordings.Recording.original_name` | C1 | Patient-side PHI, not account data — erased with the recording via the soft-delete + purge pipeline, and the row cascades from the author on `erase_user`. Subject-erasure registration would misfile it under the *account's* PII. |
| `recordings.Recording.processing_error` | C1 | Same treatment as `original_name`: author-private operational text erased with the row. |
| `recordings.SignalInfo.source_label` / `source_transducer_type` / `source_prefiltering` / `source_index` | C1 | Same pattern as `recordings.Recording.original_name`: recording-subject-adjacent site metadata (pre-de-identification channel descriptors and the original template position), not account PII. Author-private in the API; rows cascade from `RecordingMeta` → `Recording` and are removed by the purge pipeline. Never serialized to `ObjectChangeLog` (`SignalInfo` is digest-only). |
| `media.MediaFile.original_name` | C1 | Same pattern as `recordings.Recording.original_name`; purged with the file. |
| `federation.FederationAuditLog.remote_user_id` | C1, C3 | Remote subject's pseudonymous identifier retained for the deployment's regulatory audit minimum (append-only compliance log; see federation/README.md). Erasure requests from remote users are the remote controller's to fulfil; the identifier alone does not identify a person without the peer's records. Retention pruning is the operator's scheduled job. |
| `activity.Activity.path` / `target_identifier` | C1 | URL paths on this platform carry opaque hashes, never usernames or emails; the fields are audit-essential. |
| `library.Collection.name` / `library.Dataset.name` / `library.DatasetFolder.name` / `library.Tag.name` | C1, C3 | User-chosen labels, not identity data; rows cascade from the author on `erase_user` (`DatasetFolder` via `Dataset.author`). Folder-name PHI on upload is handled by the opt-in hierarchy warning (frontend/README.md → Folder uploads + PHI). |
| `library.DatasetSnapshot.label` / `manifest` | C1, C3 | `label` is the same user-chosen-label class as the row above; `manifest` holds opaque member hashes that are neither invertible nor confirmable (recording `content_hash` covers the random `stored_name`) and resolve to nothing. Rows cascade from both the snapshot author and the dataset author on `erase_user`; member erasure deliberately leaves the manifest sealed (library/README.md → Models). |
