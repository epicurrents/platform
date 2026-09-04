# Privacy notice — template

A drafting template for the Art. 13 / 14 information a deployment must give the people whose data it processes. It is not a privacy notice. Everything the software determines is filled in and accurate as of the date in the audit log of [gdpr-compliance.md](gdpr-compliance.md); everything only the operator knows is marked `[FILL]`, and the conditional sections are marked with the setting that decides whether they apply.

**Have it reviewed by someone qualified before publishing it.** The facts below are drawn from the code, but which lawful basis applies, whether a DPIA is required, and what your supervisory authority expects are not questions this repository can answer.

## How to use it

1. Copy it to [local/](../local/README.md) and work on the copy, leaving this file untouched. A part-filled notice names your organisation and the compliance questions you have not answered yet, and that directory is gitignored so it cannot reach a shared history. Editing this file in place is the mistake the convention exists to prevent.
2. Work through every `[FILL]`. There is no default that is safe to leave.
3. Delete every conditional block whose feature is off in your `.env`. A notice that tells people their data goes to federated peers when federation is disabled is inaccurate in the direction that erodes trust; one that stays silent when it is enabled is inaccurate in the direction that breaks the law.
4. Check the retention numbers against your own settings — the values below are the shipped defaults and several are meant to be tuned.
5. Publish **both** notices. They address different people with different rights, and the second one has a delivery problem the first does not (see *Reaching recording subjects*).
6. Re-read it whenever the audit log in [gdpr-compliance.md](gdpr-compliance.md) gains a row. That is the trigger — the inventories there are this document's source.

## Which notice goes to whom

The platform processes two families of data subject, and GDPR treats them differently because of where the data comes from.

| | Notice A — account holders | Notice B — recording subjects |
|---|---|---|
| Who | Clinicians, researchers, students and operators who sign in | Patients whose signal recordings, media or images are uploaded |
| Article | 13 (collected from the subject) | 14 (obtained from someone else) |
| Extra duties | — | Must state the **source** of the data, and must be given within a month of obtaining it, or at first communication |
| Delivery | At account creation, and linked from the sign-in page | Usually through the treating institution — see below |

### Reaching recording subjects

Art. 14 has an exemption where notification is impossible or a disproportionate effort, and a deployment that never learns a patient's identity may qualify — the platform de-identifies EDF headers at ingest and its own audit trail is built to avoid holding patient identifiers. That exemption is not automatic and not yours to assume. In practice the notice normally reaches the patient through the institution that recorded them, as part of its own information duties, because that institution knows who they are and this platform deliberately does not. Settle this with the controller who supplies the recordings before the first upload, not after.

---

# Notice A — for people with an account

## Who is responsible

The controller is `[FILL: legal entity name, registered address, company / org number]`.

Contact for privacy questions: `[FILL: email address]`.

`[FILL, or delete: data protection officer's name and contact — required if you have one, whether or not appointment was mandatory]`

## What is collected, and why

| Data | Where it comes from | Why | Lawful basis |
|---|---|---|---|
| Username, first and last name, email address | You, at account creation, or your organisation's directory | Identifying you, letting others share data with you by name, sending password resets | `[FILL]` |
| Password (stored only as a hash) | You | Authenticating you | `[FILL]` |
| Two-factor secret and recovery codes, if you enable them | You | Protecting your account with a second factor | `[FILL]` |
| Sign-in events, including failed attempts and the IP address they came from | Automatically | Detecting attacks on accounts; rate-limiting | `[FILL — commonly legitimate interests in security]` |
| A record of every action you take in the application: what you read, created, changed or deleted, when, and from which interface | Automatically | Reconstructing who did what to clinical data — a requirement of processing health data, not an optional feature | `[FILL]` |
| Free text you write: annotation content, collection and dataset names, tags | You | Providing the service | `[FILL]` |
| Client settings, e.g. your chosen montage | You | Restoring your preferences on another machine | `[FILL]` |
| Push notification endpoint and encryption keys, if you enable notifications | Your browser | Delivering notifications you asked for | `[FILL — commonly consent]` |

`[Conditional — delete unless OIDC_ENABLED: ]` If you sign in through `[FILL: provider name]`, that provider tells us an opaque identifier for you, your email address, your name, and the tenant you belong to. We do not receive your password.

`[Conditional — delete unless the deployment runs a project plugin: ]` `[FILL: what the active project stores about you — see its README. For a teaching project, for instance: your course role, submitted annotation sets and the instructor feedback on them.]`

## How long it is kept

| Data | Kept for |
|---|---|
| Your account and everything linked to it | As long as the account exists |
| Sign-in session | 12 hours from signing in, whether or not you are active (`SESSION_COOKIE_AGE`) |
| Action log — the searchable index | 90 days, then archived but retained (`ACTIVITY_ARCHIVE_AFTER_DAYS`) |
| Action log — the tamper-evident record of changes | **Permanently.** It is an integrity record: entries are chained to each other, so removing one would break the chain that proves the rest were not altered. When you exercise your right to erasure we overwrite the personal data inside these entries and re-seal them, leaving the chain verifiable and the personal data gone |
| Things you put in the trash | 30 days, then permanently deleted (`RECORDINGS_TRASH_RETENTION_DAYS`, `MEDIA_TRASH_RETENTION_DAYS`, `LIBRARY_TRASH_RETENTION_DAYS`) |
| Backups | About six months, encrypted `[FILL if you changed the retention]` |

## Who else sees it

| Who | What they get | Why |
|---|---|---|
| `[FILL: your email provider]` | Your address and the content of account emails | Sending password resets |
| `[Conditional — delete unless push is enabled: Google, Mozilla or Apple, depending on your browser]` | Your device's notification endpoint and the timing of notifications. The content is encrypted end-to-end and they cannot read it | Delivering notifications |
| `[Conditional — delete unless OIDC_ENABLED: FILL provider name]` | The fact and time of your sign-in | Authenticating you |
| `[Conditional — delete unless federation is configured: FILL peer institution names]` | Your user identifier, when you request data from them | Letting them decide whether to grant you access |
| `[FILL: hosting provider, if the deployment is not self-hosted]` | Everything, as infrastructure | Running the service |

There is no analytics, advertising or error-reporting service. `[Conditional — delete unless BORG_MONITOR_URL is set: A backup monitor at FILL is told when backups start, finish and fail. FILL: state whether BORG_MONITOR_SEND_LOGS is on, and if so that log excerpts including file paths are sent.]`

`[FILL: where these recipients are located, and — for any outside the EEA — the transfer mechanism you rely on. Push services and most identity providers are US-based; this is not optional to answer.]`

## Your rights

You can ask us to:

- **Show you** what we hold about you (Art. 15). `[FILL: name a contact and a realistic turnaround. There is no self-service export; an operator produces one with `manage.py export_user`, so this is a person's response time, not a download link.]`
- **Correct** it (Art. 16). Your own name and email are editable in your profile; ask us for anything else.
- **Delete** it (Art. 17). We remove your account, your sessions, and the personal data inside the permanent action log. Two consequences worth stating plainly rather than burying. Erasing your account also deletes the recordings and files **you uploaded**, along with the annotations on them — so if you uploaded clinical data that others rely on, `[FILL: say what you do about this — transferring ownership before erasure, or the retention duty you rely on to refuse the request in part]`. And data already written to an encrypted backup disappears when that backup rotates out, which takes up to about six months.
- **Restrict** processing (Art. 18) or **object** to it (Art. 21). `[FILL: contact]`
- **Take your data elsewhere** (Art. 20). You can download recordings you own and export your annotations.
- **Withdraw consent** at any time, where consent is what we rely on — turning off notifications, for instance. This does not undo what was lawful before you withdrew it.

You can also complain to `[FILL: your supervisory authority and its website]`.

## Automated decision-making

`[FILL. If the deployment runs automated analysis that produces a finding about a person, say so, say what it does, and say that a human reviews it. If it does not, say there is no automated decision-making with legal or similarly significant effects. Do not leave this out — the platform can run signal analysis, and whether it decides anything about anyone is a deployment question.]`

---

# Notice B — for people whose recordings are stored here

## Who is responsible

`[FILL: same controller details as above.]`

`[FILL: name the institution that recorded you and supplied the data, and say whether it is a joint controller or a separate one. This is the "source" disclosure Art. 14 requires and it cannot be omitted.]`

## What is held

- Your neurophysiological signal recording — the EEG, EMG or other signal data itself — together with technical details of the equipment and how long the recording ran.
- Notes and markings clinicians or researchers have made on the recording.
- `[Conditional — delete if unused: Related media such as documents or video.]`
- `[Conditional — delete unless the dicom plugin is enabled: Imaging data and the demographic fields stored inside it.]`

Direct identifiers are removed from the recording file when it is uploaded: the patient and recording-identification fields in the file header are blanked, the start date is reset, and channel labels are rewritten to a standard set so the recording does not carry the naming conventions of the place it was made. The file name given by the uploading system is kept, because it is often the only way the uploading clinician can find the recording again. It is shown only to the person who uploaded it and to `[FILL: your term for system administrators]`, and never to anyone they share the recording with, to a holder of a sharing link, or to a federated peer.

## Why

`[FILL: your purposes and the lawful basis for each — typically Art. 6(1) plus an Art. 9(2) condition, since this is health data. Common combinations are (a)+(a) for consent-based research and (e)+(i)/(j) for public-interest health or scientific research. Name the one you actually rely on.]`

## How long

`[FILL: your retention period, and what triggers deletion.]` Once a recording is deleted it goes to a trash state for 30 days and is then permanently removed along with its file. `[Conditional — delete unless RECORDINGS_ORIGINALS_PATH is set: An unmodified copy of each uploaded file is also kept on a separate archive volume that the application can write to but never read. FILL: who controls it, how long they keep it, and how a deletion request reaches it.]`

## Who else sees it

- The clinicians and researchers at `[FILL]` who have been granted access to your recording.
- `[Conditional — delete unless federation is configured: Researchers at FILL: peer institutions, where a grant has been made. Recordings sent to a peer are de-identified in transit by default: the header is anonymised again and clinical note text is removed. Each institution is a separate controller for what it receives.]`

## Your rights

The same rights listed in Notice A apply. To exercise them, contact `[FILL]` — or `[FILL: the treating institution]`, which may be better placed to identify which recording is yours, since this system is built not to know.

`[FILL: if you rely on a research exemption to restrict any of these rights, say which and on what national provision.]`
