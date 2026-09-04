"""Management command — produce an Art. 15 subject access export for one user.

The counterpart to ``erase_user``. A subject asks what is held about them; an
operator runs this and sends them the result.

A command rather than a self-service endpoint, for now and deliberately. Serving
an Art. 15 request has a verification step that is not a software problem — the
requester has to be established as the subject — and an endpoint that hands a
complete personal-data dossier to whoever holds a session is a worse answer than
a person doing it on request. A self-service export can follow once the
verification story is decided; the payload builder in ``user.export`` is already
shared, so it will not be a second implementation.

The run is audited under ``user.export`` with the subject's id in the metadata,
because producing a copy of someone's personal data is itself processing worth a
record. Only the subject's id goes there, never their name — the activity
metadata rule in AGENTS.md.
"""

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from user.export import export_user_json, export_user_text, resolve_subject


class Command(BaseCommand):
    help = (
        "Write an Art. 15 subject access export for one user.\n\n"
        "Identify the subject by --username or --user-id. Writes to stdout "
        "unless --output names a file."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Subject's username.")
        parser.add_argument("--user-id", type=int, help="Subject's numeric id.")
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="text (default) is the document a subject receives; json is the "
            "machine-readable form for an Art. 20 portability request.",
        )
        parser.add_argument(
            "--output",
            help="Write to this path instead of stdout. The file contains personal data — "
            "place it somewhere you are willing to have it, and remove it once sent.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        user_id = options["user_id"]
        if not username and user_id is None:
            raise CommandError("Give --username or --user-id.")
        if username and user_id is not None:
            raise CommandError("Give one of --username or --user-id, not both.")

        user = resolve_subject(username=username, user_id=user_id)
        if user is None:
            raise CommandError(f"No user matches {username or user_id!r}.")

        with with_system_activity(
            "user.export",
            interface=Activity.Interface.COMMAND,
            target=user,
            metadata={"subject_id": user.pk},
        ):
            render = export_user_text if options["format"] == "text" else export_user_json
            payload = render(user)

        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote export for user {user.pk} to {options['output']}"))
            self.stdout.write("The file contains personal data. Remove it once the subject has it.")
        else:
            self.stdout.write(payload)
