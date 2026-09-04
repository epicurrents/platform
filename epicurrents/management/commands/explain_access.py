"""Management command that prints the read-access resolution path for an (object, user) pair."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q
from django.utils import timezone


class Command(BaseCommand):
    """Explain which step of the read-permission resolver grants (or denies) a caller an object.

    Mirrors the resolution order of ``epicurrents.permissions.get_read_access_result`` —
    superuser fast-path, read-visibility gates, direct ``AccessRight`` row, then each
    registered extension in registration order — and reports the first step that grants
    (or the gate that hides), with the ``apply_middleware`` outcome. Read-only; a debugging and review aid, not a permission surface.

    Usage::

        manage.py explain_access recordings.recording 42 alice
        manage.py explain_access recordings.recording 42 --share-token tok123
    """

    help = "Print the read-access resolution path for an object and a user or share token."

    def add_arguments(self, parser):
        parser.add_argument("model", help="Model label, e.g. recordings.recording")
        parser.add_argument("object_id", help="The object's primary key")
        parser.add_argument("username", nargs="?", default=None, help="Caller's username (omit for anonymous)")
        parser.add_argument("--share-token", default=None, help="Public share token presented by the caller")

    def handle(self, *args, **options):
        from epicurrents.models import AccessRight
        from epicurrents.permissions import (
            _READ_PERMISSION_EXTENSIONS,
            _READ_VISIBILITY_GATES,
            ReadAccessTerms,
            get_read_access_result,
        )

        try:
            model = apps.get_model(options["model"])
        except LookupError as exc:
            raise CommandError(str(exc)) from exc
        obj = model.objects.filter(pk=options["object_id"]).first()
        if obj is None:
            raise CommandError(f"{options['model']} with pk {options['object_id']} does not exist")

        user = None
        if options["username"]:
            user = get_user_model().objects.filter(username=options["username"]).first()
            if user is None:
                raise CommandError(f"user {options['username']!r} does not exist")
        token = options["share_token"]

        self.stdout.write(f"object: {options['model']} pk={obj.pk}")
        self.stdout.write(
            f"caller: {user.get_username() if user else 'anonymous'} share_token={'yes' if token else 'no'}"
        )

        # 1. Superuser fast-path.
        if user and user.is_superuser:
            self.stdout.write(self.style.SUCCESS("GRANTED at step 1: superuser fast-path (apply_middleware=False)"))
            return
        self.stdout.write("step 1: not a superuser")

        # 2. Read-visibility gates — a gate that hides the object ends
        # resolution before any grant is consulted.
        gates = _READ_VISIBILITY_GATES.get(obj._meta.label_lower, ())
        for gate in gates:
            name = f"{gate.__module__}.{gate.__name__}"
            if gate(user=user, obj=obj, share_token=token):
                self.stdout.write(
                    self.style.WARNING(
                        f"DENIED at step 2: visibility gate {name} hides this object from the caller; "
                        "no grant can surface it"
                    )
                )
                return
            self.stdout.write(f"step 2: visibility gate {name} does not hide this object")
        if not gates:
            self.stdout.write("step 2: no visibility gates registered for this model")

        # 3. Author shortcut — not part of the resolver, but most endpoints
        # check it before ever calling the resolver, so the answer is
        # incomplete without it.
        author_id = getattr(obj, "author_id", None)
        if user and author_id == user.pk:
            self.stdout.write(
                self.style.SUCCESS(
                    "AUTHOR: the caller authored this object; endpoints grant this before the resolver runs"
                )
            )
        else:
            self.stdout.write("step 3: not the author")

        # 4. Direct AccessRight rows, in the resolver's own filter shape.
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        now = timezone.now()
        base_qs = AccessRight.objects.filter(
            content_type=content_type,
            object_id=str(obj.pk),
            can_read=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

        target_filter = Q()
        if user is not None and user.is_authenticated:
            target_filter |= Q(access_target_id=user.pk)
            group_ids = list(user.groups.values_list("id", flat=True))
            if group_ids:
                target_filter |= Q(access_target_group_id__in=group_ids)
        if (token or "").strip():
            target_filter |= Q(public_share_token=token.strip())

        if target_filter:
            # Same ordering as the resolver, so the row named here is the row
            # the resolver actually returns: direct user row first, then the
            # de-identifying row among group / token rows.
            right = (
                base_qs.filter(target_filter)
                .order_by(F("access_target_id").asc(nulls_last=True), "-apply_middleware")
                .first()
            )
            if right is not None:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"GRANTED at step 4: direct AccessRight row pk={right.pk} "
                        f"(target={self._describe_target(right)}, apply_middleware={right.apply_middleware}); "
                        "extensions are not consulted"
                    )
                )
                return
        self.stdout.write("step 4: no matching direct AccessRight row")

        # 5. Registered extensions, in registration order.
        for checker in _READ_PERMISSION_EXTENSIONS:
            result = checker(user=user, obj=obj, share_token=token)
            granted = result.granted if isinstance(result, ReadAccessTerms) else bool(result)
            name = f"{checker.__module__}.{checker.__name__}"
            if granted:
                middleware = result.apply_middleware if isinstance(result, ReadAccessTerms) else False
                self.stdout.write(
                    self.style.SUCCESS(f"GRANTED at step 5: extension {name} (apply_middleware={middleware})")
                )
                return
            self.stdout.write(f"step 5: extension {name} did not grant")

        # 6. The resolver's own verdict, as a cross-check of the walk above.
        outcome = get_read_access_result(user=user, obj=obj, share_token=token)
        self.stdout.write(self.style.WARNING(f"DENIED (resolver cross-check: granted={outcome.granted})"))

    @staticmethod
    def _describe_target(right):
        """Name which target leg of the AccessRight row matched."""
        if right.access_target_id:
            return f"user:{right.access_target_id}"
        if right.access_target_group_id:
            return f"group:{right.access_target_group_id}"
        if right.public_share_token:
            return "share-token"
        if right.federated_peer_id:
            return f"federated-peer:{right.federated_peer_id}"
        return "unknown"
