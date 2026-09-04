"""Management command — print a freshly-generated VAPID keypair for manual paste into ``.env``.

``init_env`` is the preferred path for the normal bootstrap flow; this
command is for one-off rotation or troubleshooting.
"""

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid01, b64urlencode


class Command(BaseCommand):
    help = "Generate WEBPUSH VAPID keys for .env configuration"

    def handle(self, *args, **options):
        vapid = Vapid01()
        vapid.generate_keys()

        public_raw = vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")

        public_key = b64urlencode(public_raw)
        private_key = b64urlencode(private_raw)

        self.stdout.write("WEBPUSH_VAPID_PUBLIC_KEY=" + public_key)
        self.stdout.write("WEBPUSH_VAPID_PRIVATE_KEY=" + private_key)
