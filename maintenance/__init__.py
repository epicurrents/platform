"""Remote maintenance: audited management operations requested from the web UI.

The web tier never executes a host operation. It records an intent — a
``MaintenanceJob`` row and, for the host tier, a request file in the spool —
and something else carries it out: a Celery task for operations that are
management commands, a root-owned host agent for the ones that need the
container runtime. See README.md.
"""
