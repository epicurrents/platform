# PHI-exposure exemptions

Exemption registry for the [`phi-exposure` agent](../agents/phi-exposure.md).
Sibling registries for other agents live next to this file in
[.review/exemptions/](.); see that directory's [README](README.md)
for the convention.

The PHI-exposure rules (C1–C8 in the agent spec) are the strict
default. Listing an item here means the agent treats its deviation
from the default as an audited intentional choice rather than as a
finding. Exemptions are per-check (`C1` / `C2` / …); an item can be
exempt from one check while still subject to the rest.

## How to add an entry

1. Add a row to the table below with the precise locator — for an
   endpoint, the HTTP method + path exactly as written in the `@api.*`
   decorator; for a schema, the file path + class name; for a code
   path, the file path + function name.
2. Name the exempt check(s) by their C-code (`C1`, `C3`, …). A single
   row may exempt one item from multiple checks.
3. Write a one-line reason that explains *why* the deviation is
   structurally safe — what other constraint makes the deviation
   equivalent to satisfying the rule. "Convenience" / "not needed
   here" are not acceptable reasons; the rule defaults exist
   precisely to catch cases where the reviewer's intuition is wrong.

If the answer to "is this item exempt?" is anything other than an
unambiguous yes with a written constraint, add the gate instead.

## Current exemptions

| Locator | Exempt from | Reason |
|---|---|---|
| `POST /recordings/api/v1/upload` | C3 | The upload endpoint is author-only by construction — the caller IS the author at this moment, so the `_can_see_original_name` gate would always return True. `RecordingUploadOut` returns `original_name` unconditionally for this reason. |
| `recordings/api/v1/ninja.py:RecordingUploadOut` | C2 | This schema is the upload response and the upload endpoint is author-only by construction (see above). `original_name` is permitted here. Other Recording-related Out classes (`RecordingOut`, `RecordingSliceOut`) gate the field via C3. |
| `POST /media/api/v1/upload` | C3 | Same author-only-by-construction shape as the recordings upload. The `MediaFile` row is created in the same transaction as the response; the `author=` argument passed to `MediaFile.objects.create` is the request user, so the caller is always the author for this response. No non-author code path exists; `_can_see_original_name` would always return True. |
| `media/api/v1/ninja.py:MediaFileUploadOut` | C2 | Schema for the upload response; the endpoint is author-only by construction (see above). `original_name` is permitted here. `MediaFileDetailOut` gates the field via C3 on all other read paths. |
| `GET /media/api/v1/{content_hash}/file` | C6 | `MediaFile` carries opaque media (markdown / PDF / HTML today; image / audio / video later) that no platform middleware pipeline rewrites — the PHI-sanitising `MiddlewarePipeline` is EDF/BDF-specific and structurally has no work to do on these formats. Federation is not yet wired for media (phase-2 work), so there is no peer-vs-grantee distinction to resolve `apply_middleware` against. When federation lands, this exemption is the prompt to revisit. |
| `GET /recordings/api/v1/status/{hash}` | C5 | The status endpoint returns only a status string with no `original_name`, `processing_error`, or other PHI-bearing field — even when the recording is FAILED. The 404 / FAILED distinction is by design: a failed upload's author needs to poll for the failure to surface, and the response carries no PHI beyond the state token. |
| Datasets — all `/api/v1/library/datasets/{id}/...` endpoints | C1 | Hash-first addressing: routes resolve `Dataset.object_hash` (what the frontend serves in URLs) with the integer PK accepted for internal callers and old links via `_get_active_dataset`. The accepted PK form conveys nothing about the contained data. Documented in AGENTS.md → De-identification and library/README.md → Identifiers. |

Project-plugin deviations are **not** listed here — projects are due to be extracted from this repository, and a central row would go stale the moment one leaves. A project documents its deviation in its own README (and the deviating function's docstring) and discharges the agent's finding manually when its code appears in a diff.

## When to add the next exemption

Three legitimate shapes appear in practice:

1. **Author-only by construction.** The endpoint cannot be reached
   by any caller other than the recording author. Upload is the
   canonical example. New endpoints claiming this must justify
   *which* check prevents non-author access (an auth-mode constraint,
   a path that's not exposed to federation, a permission helper that
   short-circuits the body).
2. **No PHI in the response.** The endpoint surfaces a Recording row
   but returns a derived value that carries no PHI (a status string,
   an aggregate count, a derived hash). C5 may be exempt because the
   FAILED state itself isn't sensitive in this response shape.
3. **Structural identity exception.** Datasets, as above. New
   resources claiming this need a clear "the identifier conveys
   nothing" rationale.

Convenience, code symmetry, or "the caller will check it" are not
shapes. The reviewer enforces because the call site is the wrong
place to put the check — defence-in-depth lives at the response
boundary.

## Re-scoping when behaviour changes

An endpoint's exemption is tied to the constraint that justifies it.
If the constraint changes — e.g. `POST /recordings/api/v1/upload`
gains a non-author caller (admin re-upload? federated peer mirror
upload?) — the exemption needs to be revisited in the same change
pass. Update the row, switch the response to the standard gate, or
remove the exemption.

## Related

- Agent that consults this file:
  [.review/agents/phi-exposure.md](../agents/phi-exposure.md).
- Rule source:
  [AGENTS.md](../../AGENTS.md) → *De-identification*,
  *FAILED recording hiding*, *Originals preservation volume is
  strictly write-only*.
- Recording-side narrative:
  [recordings/README.md](../../recordings/README.md) →
  *Display name vs. original filename*, *FAILED-hidden rule*,
  *Preservation tiers*.
