"""Cluster adapter stop: a durable Pod-UID fence and a confirmed absence.

Every test drives the real ``KubernetesApi`` over ``httpx.MockTransport`` against
the scripted API server in ``tests/fake/kubernetes_cluster.py``: no cluster and
no container daemon is involved. The tests are built around the mutations this
slice exists to catch:

* reporting ``stopped`` as soon as the delete was sent, without waiting for the
  Pod to leave the API;
* ignoring the pinned UID, so a Pod rebuilt under the same name is deleted by the
  previous generation's stop intent;
* keeping the fencing anchor only in this process's memory, so a restarted
  controller can no longer decide whether it may kill anything;
* reporting ``stopped`` while the Pod is still listed by the API;
* recording a non-zero engine exit as a clean one, or inventing release evidence
  for a Pod whose exit fact was never observed.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_INSTANCE_ID,
    InMemoryStopCheckpoints,
    build_request,
    build_template,
)
from tests.fake.kubernetes_cluster import (
    NAMESPACE,
    POD_UID,
    REPLACEMENT_POD_UID,
    FakeClock,
    FakeKubernetesCluster,
    api_for,
    mark_terminated,
)

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeIntentErrorCode,
)
from speechnexus_node_controller.domain.ports import StopCheckpointStore
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
from speechnexus_node_controller.infrastructure.kubernetes_runtime import (
    KubernetesNodeRuntime,
    KubernetesRuntimeSettings,
)
from speechnexus_node_controller.infrastructure.sqlite_intent_store import (
    SqliteIntentStore,
)

NODE_ID = "node-local-1"
POD_NAME = f"engine-{SAMPLE_INSTANCE_ID}-1"
STOP_GRACE_SECONDS = 20.0
EXIT_WAIT_SECONDS = 30.0
PREPARE = build_request()
STOP = replace(
    PREPARE,
    intent_id="intent-stop",
    action=RuntimeActionKind.STOP,
    fencing_token=2,
    deadline_at=PREPARE.deadline_at,
)
TEMPLATE = build_template()


def _runtime(
    tmp_path: Path,
    cluster: FakeKubernetesCluster,
    *,
    checkpoints: StopCheckpointStore | None = None,
    clock: FakeClock | None = None,
) -> tuple[KubernetesNodeRuntime, FakeClock]:
    resolved = FakeClock() if clock is None else clock
    return (
        KubernetesNodeRuntime(
            api=api_for(tmp_path, cluster),
            node_id=NODE_ID,
            stop_checkpoints=checkpoints or InMemoryStopCheckpoints(),
            settings=KubernetesRuntimeSettings(
                namespace=NAMESPACE,
                poll_interval_seconds=1.0,
                stop_grace_seconds=STOP_GRACE_SECONDS,
                exit_wait_seconds=EXIT_WAIT_SECONDS,
            ),
            sleep=resolved.sleep,
            monotonic=resolved.monotonic,
        ),
        resolved,
    )


def _prepared(
    tmp_path: Path,
    cluster: FakeKubernetesCluster,
    *,
    checkpoints: StopCheckpointStore | None = None,
) -> tuple[KubernetesNodeRuntime, StopCheckpointStore]:
    """Run the real prepare so the stop acts on the Pod it really created."""
    store = checkpoints or InMemoryStopCheckpoints()
    runtime, _clock = _runtime(tmp_path, cluster, checkpoints=store)
    observation = runtime.prepare(PREPARE, TEMPLATE)
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert cluster.pods[POD_NAME]["metadata"]["uid"] == POD_UID
    return runtime, store


def _pin_only_stop(
    tmp_path: Path,
    cluster: FakeKubernetesCluster,
    *,
    checkpoints: StopCheckpointStore | None = None,
) -> tuple[KubernetesNodeRuntime, StopCheckpointStore]:
    """Prepare, then leave the first stop attempt with only its durable UID pin."""
    runtime, store = _prepared(tmp_path, cluster, checkpoints=checkpoints)
    cluster.pods_after_delete = None
    observation = runtime.stop(STOP, TEMPLATE)
    assert observation.observed_instance_state is ObservedInstanceState.STOPPING
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.container_id == POD_UID
    assert checkpoint.stage == "targeted"
    return runtime, store


def _record_stop_intent(store: SqliteIntentStore) -> None:
    """The durable store only checkpoints an intent it already holds."""
    store.add(RuntimeInstanceIntent(request=STOP, created_at=FIXED_NOW, updated_at=FIXED_NOW))


def _rebuild_pod(cluster: FakeKubernetesCluster) -> dict[str, Any]:
    """Replace the Pod with a rebuilt one of the same name and a new UID."""
    rebuilt = copy.deepcopy(cluster.pods[POD_NAME])
    rebuilt["metadata"]["uid"] = REPLACEMENT_POD_UID
    rebuilt["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    cluster.pods[POD_NAME] = rebuilt
    return rebuilt


def test_stop_waits_until_the_pod_leaves_the_api_and_records_release_evidence(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    cluster.exit_code_on_delete = 143
    cluster.pods_after_delete = 2

    observation = runtime.stop(STOP, TEMPLATE)

    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    evidence = observation.stop_evidence
    assert evidence is not None
    assert evidence.exit_code == 143
    assert evidence.runtime_container_id == POD_UID
    assert POD_NAME not in cluster.pods
    # The delete the driver really sent: one Pod, fenced by the pinned UID, with
    # the template's stop grace.
    assert len(cluster.deletes) == 1
    assert cluster.deletes[0]["name"] == POD_NAME
    body = cluster.deletes[0]["body"]
    assert body["preconditions"] == {"uid": POD_UID}
    assert body["gracePeriodSeconds"] == int(STOP_GRACE_SECONDS)
    # Exit is committed before release, and the release is committed last.
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.stage == "released"
    assert checkpoint.exited_at is not None
    assert checkpoint.released_at is not None
    assert checkpoint.exited_at <= checkpoint.released_at


def test_stop_reports_stopping_while_the_deleted_pod_is_still_in_the_api(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    # The delete is accepted but the Pod never leaves the API.
    cluster.pods_after_delete = None

    observation = runtime.stop(STOP, TEMPLATE)

    # "Delete sent" is not "stopped": a Pod the API still lists may hold its GPU.
    assert observation.observed_instance_state is ObservedInstanceState.STOPPING
    assert observation.stop_evidence is None
    assert observation.error_code is RuntimeIntentErrorCode.INSTANCE_DRAIN_INCOMPLETE
    assert POD_NAME in cluster.pods
    assert len(cluster.deletes) == 1
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.stage == "targeted"


def test_stop_never_reports_stopped_while_a_terminated_pod_stays_listed(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    # The engine terminated cleanly, but the Pod object is still listed: the
    # resources are not released, so the stop is not done.
    cluster.exit_code_on_delete = 0
    cluster.pods_after_delete = None

    observation = runtime.stop(STOP, TEMPLATE)

    assert observation.observed_instance_state is ObservedInstanceState.STOPPING
    assert observation.stop_evidence is None
    assert POD_NAME in cluster.pods
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    # The exit fact is committed as soon as the API reports it, before release.
    assert checkpoint.stage == "exited"
    assert checkpoint.exit_code == 0


def test_stop_never_deletes_a_rebuilt_pod_whose_uid_changed(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _pin_only_stop(tmp_path, cluster)
    deletes_before = len(cluster.deletes)
    rebuilt = _rebuild_pod(cluster)
    cluster.pods_after_delete = 0
    cluster.exit_code_on_delete = 0

    observation = runtime.recover_stop(STOP, TEMPLATE)

    # The old generation's stop must not kill the instance that took its name.
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.stop_evidence is None
    assert len(cluster.deletes) == deletes_before
    assert cluster.pods[POD_NAME]["metadata"]["uid"] == REPLACEMENT_POD_UID
    assert rebuilt["metadata"]["uid"] == REPLACEMENT_POD_UID
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.container_id == POD_UID
    assert checkpoint.stage == "targeted"


def test_the_api_server_precondition_refuses_a_pod_rebuilt_between_read_and_delete(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _pin_only_stop(tmp_path, cluster)
    deletes_before = len(cluster.deletes)
    # The Pod is rebuilt between this call's read and its delete request, so only
    # the API server's own UID precondition can still refuse the deletion.
    cluster.rebuild_before_delete = True
    cluster.pods_after_delete = 0
    cluster.exit_code_on_delete = 0

    observation = runtime.recover_stop(STOP, TEMPLATE)

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert len(cluster.deletes) == deletes_before + 1
    assert cluster.pods[POD_NAME]["metadata"]["uid"] == REPLACEMENT_POD_UID
    assert store.get_stop_checkpoint(STOP) is not None


def test_a_restarted_controller_still_fences_a_rebuilt_pod_from_the_checkpoint(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    store: StopCheckpointStore = InMemoryStopCheckpoints()
    _, store = _pin_only_stop(tmp_path, cluster, checkpoints=store)
    _rebuild_pod(cluster)
    cluster.pods_after_delete = 0
    cluster.exit_code_on_delete = 0

    # A new process: a new adapter instance with a new API client. Everything it
    # knows about the Pod UID it created must come back from the checkpoint.
    restarted, _clock = _runtime(tmp_path, cluster, checkpoints=store)
    # The new instance's only possible source of the original UID is the store.
    assert restarted._pod_uids == {}

    observation = restarted.recover_stop(STOP, TEMPLATE)

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    # Nothing was deleted after the restart either: the fence is the persisted UID.
    assert len(cluster.deletes) == 1
    assert cluster.pods[POD_NAME]["metadata"]["uid"] == REPLACEMENT_POD_UID
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.container_id == POD_UID
    assert checkpoint.stage == "targeted"


def test_a_restarted_controller_completes_the_persisted_stop_for_the_pinned_uid(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    store: StopCheckpointStore = InMemoryStopCheckpoints()
    _pin_only_stop(tmp_path, cluster, checkpoints=store)
    cluster.pods_after_delete = 1
    cluster.exit_code_on_delete = 137

    restarted, _clock = _runtime(tmp_path, cluster, checkpoints=store)
    assert restarted._pod_uids == {}

    observation = restarted.recover_stop(STOP, TEMPLATE)

    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    assert observation.stop_evidence is not None
    # The exit code is re-read from the API, not remembered from before the restart.
    assert observation.stop_evidence.exit_code == 137
    assert observation.stop_evidence.runtime_container_id == POD_UID
    assert len(cluster.deletes) == 2
    assert cluster.deletes[1]["body"]["preconditions"] == {"uid": POD_UID}
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.stage == "released"


def test_stop_records_the_api_exit_code_instead_of_a_clean_zero(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _store = _prepared(tmp_path, cluster)
    cluster.exit_code_on_delete = 137
    cluster.pods_after_delete = 1

    observation = runtime.stop(STOP, TEMPLATE)

    assert observation.stop_evidence is not None
    # A SIGKILLed engine is stopped, but it did not exit cleanly; the recorded
    # fact is the code the API reported, never a convenient zero.
    assert observation.stop_evidence.exit_code == 137
    assert observation.stop_evidence.exit_code != 0
    assert observation.error_code is RuntimeIntentErrorCode.ENGINE_EXIT_CODE_NONZERO


def test_stop_records_an_already_terminated_engine_exit_before_deleting(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    # The engine crashed on its own before the stop arrived.
    mark_terminated(cluster.pods[POD_NAME], 1)
    cluster.pods_after_delete = 1

    observation = runtime.stop(STOP, TEMPLATE)

    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    assert observation.stop_evidence is not None
    assert observation.stop_evidence.exit_code == 1
    assert observation.error_code is RuntimeIntentErrorCode.ENGINE_EXIT_CODE_NONZERO
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.exit_code == 1
    assert checkpoint.stage == "released"


def test_stop_writes_no_release_evidence_when_the_exit_fact_was_never_observed(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    # The Pod vanishes between the read and the delete: the API answers 404.
    cluster.vanish_before_delete = True

    observation = runtime.stop(STOP, TEMPLATE)

    # The Pod is gone, but its exit was never observed: an honest observation
    # reports absence without inventing a stop proof.
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None
    assert POD_NAME not in cluster.pods
    assert len(cluster.deletes) == 1
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.stage == "targeted"
    assert checkpoint.exit_code is None


def test_repeated_stop_after_release_is_idempotent_and_deletes_nothing(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    cluster.exit_code_on_delete = 0
    cluster.pods_after_delete = 1
    first = runtime.stop(STOP, TEMPLATE)
    assert first.stop_evidence is not None
    released = store.get_stop_checkpoint(STOP)
    assert released is not None
    assert released.released_at is not None
    deletes_before = len(cluster.deletes)

    second = runtime.recover_stop(STOP, TEMPLATE)

    assert second is not None
    assert second.observed_instance_state is ObservedInstanceState.ABSENT
    assert second.stop_evidence == first.stop_evidence
    assert len(cluster.deletes) == deletes_before
    assert store.get_stop_checkpoint(STOP) == released


def test_stop_of_a_pod_that_was_never_created_reports_absence_without_evidence(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    observation = runtime.stop(STOP, TEMPLATE)

    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None
    assert cluster.deletes == []


def test_stop_refuses_a_request_for_another_node_without_acting(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, _clock = _runtime(tmp_path, cluster)

    with pytest.raises(IntentConflictError):
        runtime.stop(replace(STOP, target_node_id="node-other"), TEMPLATE)

    assert cluster.requests == []


def test_stop_reports_an_api_failure_as_an_incomplete_action(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    cluster.delete_status = 500

    with pytest.raises(RuntimeControllerUnavailableError):
        runtime.stop(STOP, TEMPLATE)

    # The pin survives a failed action, so the retry resumes instead of re-targeting.
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.container_id == POD_UID
    assert checkpoint.stage == "targeted"


def test_stop_refuses_a_same_named_pod_that_is_not_this_generation(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    cluster.pods[POD_NAME] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": POD_NAME,
            "namespace": NAMESPACE,
            "uid": "someone-elses-pod",
            "labels": {},
            "annotations": {},
        },
        "spec": {},
        "status": {"phase": "Running", "conditions": []},
    }
    store = InMemoryStopCheckpoints()
    runtime, _clock = _runtime(tmp_path, cluster, checkpoints=store)

    with pytest.raises(IntentConflictError):
        runtime.stop(STOP, TEMPLATE)

    assert cluster.deletes == []
    assert store.get_stop_checkpoint(STOP) is None


def test_stop_pins_only_the_uid_of_the_pod_it_verified(tmp_path: Path) -> None:
    cluster = FakeKubernetesCluster()
    runtime, store = _prepared(tmp_path, cluster)
    cluster.pods_after_delete = 0
    cluster.exit_code_on_delete = 0

    runtime.stop(STOP, TEMPLATE)

    checkpoint = store.get_stop_checkpoint(STOP)
    assert isinstance(checkpoint, StopCheckpoint)
    assert checkpoint.container_id == POD_UID
    assert len(checkpoint.container_id) == len(POD_UID) <= 128


def test_the_uid_fence_survives_a_reopened_durable_checkpoint_store(tmp_path: Path) -> None:
    """The real node store, closed and reopened: the anchor is not in this process."""
    cluster = FakeKubernetesCluster()
    database = tmp_path / "intents.sqlite3"
    first_store = SqliteIntentStore(database)
    _record_stop_intent(first_store)
    try:
        runtime, _store = _prepared(tmp_path, cluster, checkpoints=first_store)
        cluster.pods_after_delete = None
        pinned = runtime.stop(STOP, TEMPLATE)
        assert pinned.observed_instance_state is ObservedInstanceState.STOPPING
        assert first_store.get_stop_checkpoint(STOP) is not None
    finally:
        first_store.close()
    _rebuild_pod(cluster)
    cluster.pods_after_delete = 0
    cluster.exit_code_on_delete = 0

    reopened = SqliteIntentStore(database)
    try:
        restarted, _clock = _runtime(tmp_path, cluster, checkpoints=reopened)
        assert restarted._pod_uids == {}

        observation = restarted.recover_stop(STOP, TEMPLATE)

        assert observation is not None
        assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
        assert len(cluster.deletes) == 1
        checkpoint = reopened.get_stop_checkpoint(STOP)
        assert checkpoint is not None
        assert checkpoint.container_id == POD_UID
        assert checkpoint.stage == "targeted"
    finally:
        reopened.close()


def test_a_reopened_durable_checkpoint_store_completes_the_same_pod_stop(
    tmp_path: Path,
) -> None:
    cluster = FakeKubernetesCluster()
    database = tmp_path / "intents.sqlite3"
    first_store = SqliteIntentStore(database)
    _record_stop_intent(first_store)
    try:
        runtime, _store = _prepared(tmp_path, cluster, checkpoints=first_store)
        cluster.pods_after_delete = None
        runtime.stop(STOP, TEMPLATE)
    finally:
        first_store.close()
    cluster.pods_after_delete = 1
    cluster.exit_code_on_delete = 143

    reopened = SqliteIntentStore(database)
    try:
        restarted, _clock = _runtime(tmp_path, cluster, checkpoints=reopened)

        observation = restarted.recover_stop(STOP, TEMPLATE)

        assert observation is not None
        assert observation.observed_instance_state is ObservedInstanceState.STOPPED
        assert observation.stop_evidence is not None
        assert observation.stop_evidence.exit_code == 143
        assert observation.stop_evidence.runtime_container_id == POD_UID
        assert cluster.deletes[1]["body"]["preconditions"] == {"uid": POD_UID}
    finally:
        reopened.close()
