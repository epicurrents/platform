"""Derived-state digester for group membership.

Group membership is an M2M table, and the audit signals listen for ``post_save``
/ ``pre_delete`` only — ``groups.set()`` emits ``m2m_changed``, which nothing
receives, and the rows are not concrete fields on the user so they never reach
``serialize_instance``. Without something here, the account surface's most
consequential operation would be the one that left no trace, which is the gap
the surface exists to close.

The account endpoints therefore write an explicit ``record_modify_change`` whose
``extra_payload`` carries a digest of the resulting membership. The digest is
mixed into the row's ``after_hash``, so editing the M2M table afterwards and
then editing the row's stored digest to match still breaks chain verification.

The digest is taken over group **primary keys**, not names. Groups can be
renamed — the account surface offers it, and nothing in the platform gates on a
group's name — so a name-based digest would recompute differently after any
rename and report every historical membership row as tampered. Same reasoning as
``canonical_label``'s exclusion from the signal digest: a digest must not be a
function of something that legitimately changes underneath it.
"""

import hashlib
import json

GROUP_MEMBERSHIP_DIGEST_KEY = "group_membership_digest"


def compute_group_membership_digest(user) -> str:
    """Return a deterministic sha256 hex digest over a user's group membership.

    Sorted so the digest does not depend on the database's row order. Returns
    the digest of an empty list for a user in no groups, which is a real state
    rather than a missing one.
    """
    group_ids = sorted(user.groups.values_list("pk", flat=True))
    encoded = json.dumps(group_ids, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
