# Management commands

Index of the custom Django management commands available on the platform. This page is the index — for full options and worked examples, follow the cross-link per command to the relevant app README. Argument lists below are the ones the commands declare; `--help` is always authoritative.

## Running commands

All commands run inside the Docker stack. The helper script handles the invocation:

```bash
scripts/manage.sh <command> [args...]
```

It's a thin wrapper around:

```bash
docker compose run --rm web python manage.py <command> [args...]
```

> **Always run management commands inside the Docker container, not on the host.** The host's local SQLite development database is different from the Docker stack's PostgreSQL. Running a command on the host while the Docker stack is up applies migrations or writes data to the wrong database and leaves the stack in a broken state.

Every command supports `--help`:

```bash
scripts/manage.sh <command> --help
```

This shows the full argument list with descriptions.

## Bootstrap and setup

Commands you run once when first deploying. After the stack is running, you usually don't need these again.

| Command | What it does | Example |
|---|---|---|
| `init_env` | Fill empty values in `.env` with generated secrets — `SECRET_KEY`, `BORG_PASSPHRASE`, `ADMIN_PASSWORD`, VAPID keypair, federation Ed25519 keypair. Never overwrites a value you've already set. Creates `.env` from `.env.example` if missing.<br/>**Args:** `--force` (regenerate every secret regardless), `--output <path>` (write to a different file). | `scripts/manage.sh init_env` |
| `createadmin` | Create a superuser from `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL`. No-op when any superuser already exists. Run automatically by `entrypoint.sh` on first start. | `scripts/manage.sh createadmin` |
| `generate_vapid_keys` | Print a freshly-generated VAPID keypair to stdout for manual paste into `.env`. Use `init_env` instead for the normal bootstrap path. | `scripts/manage.sh generate_vapid_keys` |

Full detail: [epicurrents/README.md](../epicurrents/README.md#management-commands).

## Project lifecycle

Commands for activating, switching, or removing project plugins. **Stop the application services first** — the helper script [`scripts/switch_project.sh`](../scripts/switch_project.sh) automates the full switch sequence.

| Command | What it does | Example |
|---|---|---|
| `activate_project <name>` | Restore `_archived_<name>_*` tables back to live, run `migrate`. `EPICURRENTS_PROJECT` must equal `<name>`.<br/>**Args:** `--fresh` (clear migration history and start with empty tables; archived data preserved). | `scripts/manage.sh activate_project <project>` |
| `deactivate_project` | Rename live tables of the currently-active project to `_archived_<name>_*` so a different project can be activated. `EPICURRENTS_PROJECT` must match the currently-active project. | `scripts/manage.sh deactivate_project` |
| `remove_project_data <name>` | **Irreversibly** drop the `_archived_<name>_*` tables. Prompts for confirmation unless `--yes` is given. `EPICURRENTS_PROJECT` is not required. | `scripts/manage.sh remove_project_data old_project --yes` |

Full detail: [epicurrents/README.md](../epicurrents/README.md#management-commands). For the recommended switch flow: [`scripts/switch_project.sh`](../scripts/switch_project.sh).

## Data operations

Day-to-day data management.

| Command | What it does | Example |
|---|---|---|
| `import_recordings <path> --username <user>` | Bulk import EDF/BDF files — and any format with a converter registered in `RECORDING_CONVERTERS` — from a directory tree.<br/>**Args:** `--pipeline <label>` (default `import`), `--structure {recursive,recursive-flat,flat}`, `--preserve-annotations`, `--resume` / `--discard`, `--reprocess`. Supports resume after interruption. | `scripts/manage.sh import_recordings /data/import --username alice --structure recursive-flat --preserve-annotations` |
| `index_dicom <directory> --user <user>` | Bulk-index DICOM files from a directory into the platform. Ships with the `dicom` plugin and is only available when that plugin is enabled.<br/>**Args:** `--dry-run`, `--resume`. | `scripts/manage.sh index_dicom /data/dicom --user alice` |
| `refresh_signal_metadata` | Re-derive `RecordingMeta` and `SignalInfo` from the recording files on disk, for every recording or one.<br/>**Args:** `--recording <hash>`, `--dry-run`. | `scripts/manage.sh refresh_signal_metadata --dry-run` |
| `backfill_canonical_labels` | Re-derive `SignalInfo.canonical_label` from raw labels. Idempotent; `signal_type` is never changed. Reports channels it could not classify.<br/>**Args:** `--dry-run`, `--batch-size <n>` (default 2000), `--show-unclassified`. | `scripts/manage.sh backfill_canonical_labels --show-unclassified` |
| `validate_originals` | Validate the recordings originals volume against the database. Read-only and metadata-only; the check a restore drill ends with.<br/>**Args:** `--json`, `--no-size-check`, `--expect-tier <mode>` (validate against a given originals-preservation mode instead of the current setting, e.g. when auditing a volume after a mode switch). | `scripts/manage.sh validate_originals --json` |
| `sync_prod_to_dev` | Copy data from the production database to the development database via an intermediate JSON dump. Excludes `contenttypes`, `auth.permission`, `admin.logentry`, `sessions.session` by default.<br/>**Args:** `--output <path>` (keep the dump at a specific location), `--no-flush` (don't wipe dev DB before loading), `--keep-dump` (don't delete the intermediate file), `--exclude <app.Model>` (repeatable), `--no-default-excludes`. | `scripts/manage.sh sync_prod_to_dev` |

Full detail: [recordings/README.md](../recordings/README.md) for the recordings commands; [plugins/dicom/README.md](../plugins/dicom/README.md) for `index_dicom`; [epicurrents/README.md](../epicurrents/README.md#management-commands) for `sync_prod_to_dev`.

## Compute and analysis

Server-side signal processing from [compute/](../compute/README.md). The `--input` / `--output` commands operate on files, not on stored recordings, so they can be run on operator-supplied data without touching the database. Model-based commands load weights from operator-supplied paths only — the platform ships the mechanism and the provisioning instructions, never the weights; see [compute/README.md → Integrating an ML model-analysis workflow](../compute/README.md).

| Command | What it does | Example |
|---|---|---|
| `compute_leadfield <montage>...` | Pre-compute and cache EEG lead field matrices for one or more standard MNE montages (`standard_1020`, `biosemi64`, etc.) so the browser-side source-localisation script doesn't have to redo the forward solution.<br/>**Args:** `--grid-resolution-mm <mm>` (default 7.5), `--n-orient {1,3}` (default 1 = fixed), `--sphere-radius-m <r>`, `--sphere-center-m <x> <y> <z>`, `--force` (replace an existing cached entry). | `scripts/manage.sh compute_leadfield standard_1020 biosemi64 --n-orient 3` |
| `generate_static_leadfields <montage>...` | Generate static, content-addressed lead-field blobs plus a manifest for PWA caching. Same geometry arguments as `compute_leadfield`, plus `--output-dir <path>`. | `scripts/manage.sh generate_static_leadfields standard_1020` |
| `generate_compute_static` | (Re)generate every compute static asset (lead fields, …) for service-worker caching in one go. | `scripts/manage.sh generate_compute_static` |
| `eeg_clean --input <edf> --output <edf>` | Clean EEG with ICLabel ICA component removal and/or autoreject bad-channel repair.<br/>**Args:** `--method {iclabel,ransac,both}` (default `iclabel`), `--montage <name>` (default `standard_1020`), `--n-components <n>` (default 15), `--prob-threshold <p>`, `--epoch-seconds <s>` (default 2.0). | `scripts/manage.sh eeg_clean --input in.edf --output out.edf --method both` |
| `sleep_stage --input <edf> --output <csv>` | YASA sleep staging, with optional spindle and slow-wave detection, after 10-20 remontaging.<br/>**Args:** `--eeg <derivation>` (default `C4-M1`), `--eog`, `--emg`, `--age`, `--male {0,1}`, `--spindles`, `--slow-waves`, `--events-output <path>`. | `scripts/manage.sh sleep_stage --input night.edf --output stages.csv --spindles` |
| `braindecode_score --input <edf> --output <csv> --sfreq <hz>` | Score an EEG recording with a braindecode model and write per-window scores. Requires the explicit `--noncommercial` acknowledgement, matching the `EPICURRENTS_NONCOMMERCIAL_USE` declaration for licence-restricted models.<br/>**Args:** `--repo-id` / `--checkpoint` / `--arch` / `--random-init` (model source), `--n-chans`, `--channels`, `--window-seconds`, `--n-times`, `--hop-seconds` (default 1.0), `--n-outputs` (default 2), `--output-type {logits,probs,raw}`, `--normalization {none,zscore,percentile,exp_moving}`, `--notch`, `--positive-index`, `--threshold` (default 0.5), `--noncommercial`. | `scripts/manage.sh braindecode_score --input in.edf --output scores.csv --sfreq 256 --checkpoint /models/model.pt --noncommercial` |

Full detail: [compute/README.md](../compute/README.md) and the per-model READMEs under `compute/`.

## Recovery, audit and access

| Command | What it does | Example |
|---|---|---|
| `rollback_change <change_id> --user-id <id>` | Roll back a single `ObjectChangeLog` entry. Host-side equivalent of the rollback API; useful during incident recovery when the API is offline. The user must satisfy the same permission rules as the API. | `scripts/manage.sh rollback_change 142 --user-id 1` |
| `rotate_activity_hash_key` | Bump `ACTIVITY_HASH_KEY_CURRENT` in `.env` to the next key version of the audit-chain HMAC. Refuses if the next version is not yet present in settings, so the key is announced before it is used.<br/>**Args:** `--target-version <n>`, `--env-file <path>` (default `.env`). | `scripts/manage.sh rotate_activity_hash_key` |
| `explain_access <model> <object_id> [username]` | Print the read-access resolution path for an object and a user or share token — superuser fast-path, direct `AccessRight` row, or which registered extension granted, with the `apply_middleware` outcome. The permission debugger.<br/>**Args:** `--share-token <token>` instead of a username. | `scripts/manage.sh explain_access recordings.recording 42 alice` |
| `erase_user <username>` | GDPR Art. 17 account erasure: unlink owned recording/media files, flush the user's sessions, delete the account (cascading to owned data), and scrub the subject's personal data from the audit trail. Prints an inventory without `--yes`.<br/>**Args:** `--yes` (perform the erasure), `--user-id <pk>` (scrub-only mode for an account already deleted through another path). | `scripts/manage.sh erase_user alice --yes` |
| `export_user` | GDPR Art. 15 subject-access export for one user, as text or JSON, to stdout or a file.<br/>**Args:** `--username <name>` or `--user-id <pk>`, `--format {text,json}` (default `text`), `--output <path>`. | `scripts/manage.sh export_user --username alice --format json --output alice.json` |

Full detail: [activity/README.md](../activity/README.md), [epicurrents/README.md](../epicurrents/README.md) for `explain_access`, and [user/README.md → Account erasure](../user/README.md#account-erasure-gdpr-art-17).

## Federation

Peer and grant management for the tailnet federation layer. The `--user` / `--giver` / `--actor` arguments name the local account the action is audited under.

| Command | What it does | Example |
|---|---|---|
| `federation_add_peer --url <url>` | Register a federated peer by URL and fetch its public key. The peer is created **untrusted**.<br/>**Args:** `--display-name <name>`, `--user <username>`. | `scripts/manage.sh federation_add_peer --url https://peer.example --display-name "Peer"` |
| `federation_trust_peer --peer <id>` | Set a peer's trust flag. `--fingerprint <fp>` enforces the out-of-band key check before trusting.<br/>**Args:** `--fingerprint`, `--untrust`, `--user`. | `scripts/manage.sh federation_trust_peer --peer 3 --fingerprint SHA256:…` |
| `federation_check_peer` | Check reachability, TLS, key, and mutual trust with a peer.<br/>**Args:** `--peer <id>` or `--url <url>`, `--timeout <s>` (default 10), `--no-probe`. | `scripts/manage.sh federation_check_peer --peer 3` |
| `federation_refresh_peer_key --peer <id>` | Re-fetch and store a peer's public key from its well-known URL — the receiving side of a peer's key rotation.<br/>**Args:** `--user`. | `scripts/manage.sh federation_refresh_peer_key --peer 3` |
| `federation_list_peers` | List federated peers: id, URL, trust state, key fingerprint. | `scripts/manage.sh federation_list_peers` |
| `federation_grant --peer <id> --giver <username>` | Grant a peer (optionally one remote user) access to an object. De-identification is on by default for federated grants.<br/>**Args:** `--recording <hash>` or `--content-type <app.model> --object-id <id>`, `--remote-user <sub>`, `--write`, `--share`, `--no-apply-middleware` (serve raw bytes — a deliberate cross-controller disclosure), `--expires <datetime>`. | `scripts/manage.sh federation_grant --peer 3 --giver alice --recording <hash>` |
| `federation_list_grants` | List federation grants: peer, remote user, target, expiry.<br/>**Args:** `--giver <username>`. | `scripts/manage.sh federation_list_grants` |
| `federation_renew_grant --grant-id <id>` | Set a grant's expiry, or clear it with `--no-expiry`.<br/>**Args:** `--expires <datetime>`, `--no-expiry`, `--actor <username>`. | `scripts/manage.sh federation_renew_grant --grant-id 7 --expires 2027-01-01` |
| `federation_revoke_grant --grant-id <id>` | Revoke a federation grant.<br/>**Args:** `--actor <username>`. | `scripts/manage.sh federation_revoke_grant --grant-id 7` |
| `mount_federation_fs <mountpoint> --user-id <id>` | Mount a read-only FUSE virtual filesystem exposing federated recordings as ordinary local files for the given local user. Requires `fusepy` (in requirements.txt) and `libfuse2` (in the Dockerfile). Inside Docker the container needs `--cap-add SYS_ADMIN --device /dev/fuse`.<br/>**Args:** `--foreground` (don't daemonize), `--debug` (verbose FUSE logging), `--no-threads` (single-threaded; useful for debugging). | `scripts/manage.sh mount_federation_fs /mnt/fed --user-id 1 --foreground` |
| `rotate_federation_keys` | Generate a new Ed25519 federation keypair and (optionally) rewrite `.env`. Prints to stdout by default for review.<br/>**Args (mutually exclusive):** `--announce` (phase 1 of overlap rotation — writes the new pair to `FEDERATION_*_KEY_NEXT`, current key keeps signing), `--promote` (phase 2 — moves NEXT into current and clears NEXT), `--apply` (emergency one-step replacement, breaks outbound traffic until every peer refreshes). Also `--env <path>` to target a non-default `.env`.<br/>Recommended flow: `--announce` → wait for peer refreshes → `--promote`. See [federation/README.md](../federation/README.md#key-rotation) for details. | `scripts/manage.sh rotate_federation_keys --announce` |

Full detail: [federation/README.md](../federation/README.md#management-commands).

Inbound federation requests are also recorded in `FederationAuditLog` for compliance reconstruction. No management command for this yet — query via the Django shell or admin (see [operations.md](operations.md#query-the-federation-audit-log)).

## Adding a new command

Any Django app, plugin or project plugin can register management commands under `<app>/management/commands/<name>.py`. After adding one, update this file with a new row in the appropriate category (or add a new category if none fit). The convention used here is intent-based grouping rather than alphabetical, so a reader looking up "I need to do X" can find the command without already knowing its name.
