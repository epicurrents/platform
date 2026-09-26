"""The migrations that turn submission batches into ledgers and a dataset's group into a pool's, run over data.

Run from the state before them, with a dataset naming a group and one contributor holding two batches, so the data
steps have rows to change; on PostgreSQL that is also what would expose a schema step sharing a transaction with
their pending trigger events.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("library", "0004_dataset_submission_group"), ("recordings", "0005_recording_public_source")]
AFTER = [("library", "0006_dataset_submission_pool"), ("recordings", "0007_submission_ledger")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def _back_to_the_leaves():
    yield
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_batches_merge_into_one_ledger_and_old_groups_are_cleared():
    apps = _migrate(BEFORE)
    User = apps.get_model("user", "User")
    Group = apps.get_model("auth", "Group")
    Dataset = apps.get_model("library", "Dataset")
    Batch = apps.get_model("recordings", "SubmissionBatch")
    File = apps.get_model("recordings", "SubmissionFile")
    author = User.objects.create(username="migration-author")
    contributor = User.objects.create(username="migration-contributor")
    group = Group.objects.create(name="Migration contributors")
    dataset = Dataset.objects.create(author=author, name="Pool", object_hash="A" * 32, submission_group=group)
    first = Batch.objects.create(dataset=dataset, contributor=contributor, object_hash="B" * 32, ingested_count=2)
    second = Batch.objects.create(dataset=dataset, contributor=contributor, object_hash="C" * 32, ingested_count=3)
    for n, batch in enumerate((first, second)):
        File.objects.create(
            batch=batch,
            stored_name=f"{n:032X}.edf",
            file_extension=".edf",
            file_path="/nonexistent",
            file_size=1,
            file_hash="0" * 64,
            sidecar_hash="0" * 64,
        )

    apps = _migrate(AFTER)
    Ledger = apps.get_model("recordings", "SubmissionLedger")
    File = apps.get_model("recordings", "SubmissionFile")
    Dataset = apps.get_model("library", "Dataset")
    (ledger,) = Ledger.objects.all()
    assert ledger.object_hash == "B" * 32
    assert ledger.ingested_count == 5
    assert File.objects.filter(ledger=ledger).count() == 2
    migrated = Dataset.objects.get(pk=dataset.pk)
    assert migrated.submission_group_id is None
    assert migrated.submission_profile == ""
    assert apps.get_model("auth", "Group").objects.filter(pk=group.pk).exists()
