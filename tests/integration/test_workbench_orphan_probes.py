"""Five self-contained probes that existed but ran in no gate.

Each one drives the production bundle (or the Admin bundle) with Chromium against synthetic
inputs and states its own limits in the result. They passed when run by hand but nothing wrapped
them, so their evidence was untraceable and the same ground could be probed again by mistake.

This is synthetic evidence: no model execution, no physical device, no live service and no
speech or audio-quality claim. Where a probe discloses that it bypassed a browser policy or used
a synthetic device, that disclosure is asserted rather than glossed over.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, TypedDict

import pytest
from tests.integration.workbench_real_runtime import ROOT

pytestmark = pytest.mark.integration


class ProbeSpec(TypedDict):
    script: str
    output: str
    screenshot: bool
    result: str  # "file" writes result.json into the output directory, "stdout" prints it


# Each probe has its own interface: some take a directory (and write result.json inside it),
# others take the JSON file itself; only some accept a screenshot path; one prints its result
# instead of writing it.
PROBES: dict[str, ProbeSpec] = {
    "capture": {
        "script": "scripts/dev-env/workbench-capture-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "file",
    },
    "media": {
        "script": "scripts/dev-env/workbench-media-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "file",
    },
    "voice-create": {
        "script": "scripts/dev-env/workbench-voice-create-probe.mjs",
        "output": "dir",
        "screenshot": False,
        "result": "file",
    },
    "admin-task-center": {
        "script": "scripts/dev-env/admin-task-center-probe.mjs",
        "output": "file",
        "screenshot": True,
        "result": "file",
    },
    "file-demo": {
        "script": "scripts/dev-env/workbench-file-demo-probe.mjs",
        "output": "file",
        "screenshot": True,
        "result": "file",
    },
    "admin-workspaces": {
        "script": "scripts/dev-env/admin-workspaces-probe.mjs",
        "output": "dir",
        "screenshot": False,
        "result": "file",
    },
    "tts": {
        "script": "scripts/dev-env/workbench-tts-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "asset-reconcile": {
        "script": "scripts/dev-env/workbench-asset-reconcile-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "file-host": {
        "script": "scripts/dev-env/workbench-file-host-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "recovery": {
        "script": "scripts/dev-env/workbench-recovery-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "admin-task-events": {
        "script": "scripts/dev-env/admin-task-events-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "admin-task-recovery": {
        "script": "scripts/dev-env/admin-task-recovery-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "prompter": {
        "script": "scripts/dev-env/workbench-prompter-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
    "tts-host": {
        "script": "scripts/dev-env/workbench-tts-host-probe.mjs",
        "output": "file",
        "screenshot": False,
        "result": "stdout",
    },
}

# A probe that ever claims one of these is claiming live qualification it cannot have here.
LIVE_CLAIMS = ("liveServiceVerified", "liveSpeechVerified", "publicRealtimeOrGpuVerified")


def output_dir(name: str) -> Path:
    root = Path(
        os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
    )
    output = root / f"orphan-{name}"
    output.mkdir(parents=True, exist_ok=True)
    return output


def read_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text())
    return payload


async def run_probe(name: str) -> dict[str, Any]:
    """Run one probe and read the result file it wrote (stdout is only a log)."""
    output = output_dir(name)
    spec = PROBES[name]
    command: list[str] = [
        "node",
        spec["script"],
        "--output",
        str(output / "result.json") if spec["output"] == "file" else str(output),
    ]
    if spec["screenshot"]:
        command += ["--screenshot", str(output / "screenshot.png")]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout and process.stderr
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async with asyncio.timeout(600):
            stdout, _ = await asyncio.gather(process.stdout.read(), stderr_task)
            assert await process.wait() == 0, stderr_task.result().decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    (output / "stdout.log").write_bytes(stdout)
    if spec["result"] == "stdout":
        printed: dict[str, Any] = json.loads(stdout.decode())
        return printed
    return read_json(output / "result.json")


def assert_no_live_claim(payload: dict[str, Any]) -> None:
    for key in LIVE_CLAIMS:
        if key in payload:
            assert payload[key] is False, f"{key} must not be claimed: {payload[key]}"


async def test_the_capture_graph_probe_stays_on_a_synthetic_device() -> None:
    payload = await run_probe("capture")
    assert payload["kind"] == "synthetic_device_audio_worklet_capture"
    assert payload["bundle"] == "production"
    # The two disclosures that keep this honest: no physical microphone was opened, and the
    # probe had to bypass the autoplay policy that a real user cannot bypass.
    assert payload["physicalMicrophoneAccessed"] is False
    assert payload["autoplayPolicyBypassedForProbe"] is True
    assert payload["unexpectedNetworkRequests"] == 0
    assert payload["pageErrors"] == 0
    checks: dict[str, Any] = payload["checks"]
    assert checks and all(checks.values()), checks
    assert_no_live_claim(payload)


async def test_the_media_probe_stays_synthetic() -> None:
    payload = await run_probe("media")
    assert payload["kind"] == "synthetic_offline_audio_worklet"
    assert payload["bundle"] == "production"
    # No device permission is requested at all on this path.
    assert payload["devicePermissionsRequested"] is False
    checks: dict[str, Any] = payload["checks"]
    # The checks carry both booleans and measured counts, so pin each kind for what it is.
    booleans = [value for value in checks.values() if isinstance(value, bool)]
    assert booleans and all(booleans), checks
    assert checks["renderedSamples"] == 256, checks
    # The first rendered sample aligns with the start of the stream: nothing was dropped first.
    assert checks["firstRenderedOffsetSamples"] == 0, checks
    assert checks["pcmSampleRate"] == 24000, checks
    assert_no_live_claim(payload)


async def test_the_voice_creation_probe_replays_and_refuses_like_the_contract() -> None:
    result = await run_probe("voice-create")
    assert result["production_bundle"] is True
    assert result["synthetic_public_http"] is True
    assert result["physical_device"] is False
    assert result["model_execution"] is False
    assert result["created_jobs"] == 4
    assert len(result["writes"]) == 8
    # Replay keeps the original identity, a stale revision cannot cancel, and a quota refusal
    # blocks the submission without losing the original request.
    assert result["original_create_replayed"] is True
    assert result["original_cancel_replayed"] is True
    assert result["cas_blocks_cancel"] is True
    assert result["quota_blocks_post"] is True
    assert result["first_rejection_correctable"] is True
    assert result["original_operation_restored_by_explicit_get"] is True
    assert result["clone_material_checked"] is True
    assert result["consent_reset"] is True
    assert result["clone_reload_exact"] is True
    assert result["viewport360"] is True
    assert result["basic_focus_tab"] is True
    assert result["outbound"] == []
    assert result["errors"] == []
    assert result["serverErrors"] == []


async def test_the_admin_task_centre_probe_stays_synthetic() -> None:
    payload = await run_probe("admin-task-center")
    assert payload["kind"] == "development_admin_task_center"
    assert payload["scenariosPassed"] == 5
    assert payload["keyboardConfirmation"] is True
    assert payload["originalCancellationIdentity"] is True
    # The probe states for itself that a passed development scenario is not a release.
    assert payload["acceptanceNotRelease"] is True
    assert payload["productionScenarioExcluded"] is True
    assert payload["mobileOverflow"] is False
    assert payload["browserErrors"] == 0
    assert payload["unexpectedRequests"] == 0
    assert_no_live_claim(payload)


async def test_the_file_workflow_demo_probe_stays_synthetic() -> None:
    payload = await run_probe("file-demo")
    assert payload["kind"] == "development_file_workflow"
    assert payload["scenariosPassed"] == 9
    assert payload["originalKeyReplay"] is True
    assert payload["cleanupNotAssumed"] is True
    assert payload["exactDecimalSseCursor"] is True
    assert payload["syntheticDownload"] is True
    assert payload["productionScenarioExcluded"] is True
    assert payload["mobileOverflow"] is False
    assert payload["devicePermissionsRequested"] is False
    assert_no_live_claim(payload)


async def test_the_admin_workspaces_probe_stays_on_synthetic_http() -> None:
    # The live variant runs against an explicitly supplied disposable A fixture and is gated
    # separately; this one drives the Admin bundle against synthetic generated HTTP, so it is
    # the variant reachable without A. Distinct paths, not duplicates.
    payload = await run_probe("admin-workspaces")
    assert payload["production_bundle"] is True
    assert payload["synthetic_generated_http"] is True
    assert len(payload["requests"]) == 8
    assert payload["writes"] == 0
    assert payload["explicit_reads"] is True
    # Re-reading keeps the original page identity, a terminal cursor is not followed, and a
    # selection that arrives after the page moved on is ignored.
    assert payload["pagination_retry_original"] is True
    assert payload["terminal_cursor_ignored"] is True
    assert payload["late_selection_ignored"] is True
    assert payload["wrong_identity_rejected"] is True
    # Failures stay private and the DOM fault injection does not surface a hidden page.
    assert payload["private_errors_hidden"] is True
    assert payload["hidden_visible_dom_fault_injection"] is True
    assert payload["viewport360"] is True
    assert payload["basic_focus_tab"] is True
    assert payload["errors"] == []
    assert payload["outbound"] == []


async def test_the_tts_workflow_probe_decodes_and_releases_like_the_contract() -> None:
    payload = await run_probe("tts")
    assert payload["kind"] == "development_tts_workflow"
    assert payload["scenariosPassed"] == 4
    # The browser decoded the synthetic WAV natively: this is real decoding, not a counter.
    assert payload["nativeWavDecoded"] is True
    assert payload["originalKeyReplay"] is True
    # Cleanup is not assumed and the object URLs are actually revoked when the view unmounts.
    assert payload["cleanupNotAssumed"] is True
    assert payload["blobUrlsRevokedOnUnmount"] is True
    assert payload["mobileOverflow"] is False
    assert payload["productionScenarioExcluded"] is True
    assert payload["devicePermissionsRequested"] is False
    assert payload["outboundRequests"] == 0
    assert payload["pageErrors"] == 0
    assert_no_live_claim(payload)


def assert_quiet(payload: dict[str, Any]) -> None:
    """Shared honesty checks: no live claim and no unexpected egress or page error."""
    assert_no_live_claim(payload)
    for key in ("outboundRequests", "pageErrors", "pageOrHandlerErrors", "browserErrors"):
        if key in payload:
            assert payload[key] == 0, f"{key} must be zero: {payload[key]}"


async def test_the_asset_reconcile_probe_stays_on_synthetic_loopback() -> None:
    payload = await run_probe("asset-reconcile")
    assert payload["kind"] == "synthetic_loopback_asset_recovery"
    assert payload["builtConsumerClasses"] is True
    # The disclosure that keeps this honest: it drives built consumer classes, not the page.
    assert payload["productionUI"] is False
    assert len(payload["scenarios"]) == 10
    assert payload["noAutomaticRestoreRequests"] is True
    assert payload["noOperationRequests"] is True
    assert payload["originalKeys"] is True
    assert payload["metadataOnly"] is True
    assert_quiet(payload)


async def test_the_file_host_probe_verifies_downloaded_bytes_and_revokes_urls() -> None:
    payload = await run_probe("file-host")
    assert payload["productionUI"] is True
    assert payload["syntheticHTTP"] is True
    # Real byte count of a synthetic download, and the object URL is actually revoked.
    assert payload["downloadBytesVerified"] == 46
    assert payload["blobUrlsRevoked"] is True
    assert payload["unknownCreateOriginalKey"] is True
    assert payload["cursorReload"] is True
    assert payload["ownerChangeBlocked"] is True
    assert payload["terminalExplicitNewIntent"] is True
    assert payload["cancellationOriginalTargetKey"] is True
    assert payload["storageUnavailableBlocked"] is True
    assert payload["firstRejectedUploadCorrected"] is True
    assert payload["keyboardFocus"] is True
    assert payload["width360NoOverflow"] is True
    assert payload["fakeExcluded"] is True
    assert_quiet(payload)


async def test_the_recovery_probe_keeps_original_intents_across_reloads() -> None:
    payload = await run_probe("recovery")
    assert payload["kind"] == "chromium-reload-loopback-http-synthetic"
    assert payload["passed"] is True
    assert payload["reloads"] == 3
    # Honest negative and honest non-qualification, asserted rather than glossed over.
    assert payload["unknownUploadRecovered"] is False
    assert payload["qualifiesLiveSpeechOrGpu"] is False
    assert payload["originalCreateKeyAndBody"] is True
    assert payload["originalCancelKey"] is True
    assert payload["exactCursorRestored"] is True
    assert payload["noAudioOrContentStored"] is True
    assert payload["serverStopped"] is True
    # Replay must not multiply the intents.
    assert payload["uniqueUploadIntents"] == 1
    assert payload["uniqueCreateIntents"] == 1
    assert_quiet(payload)


async def test_the_task_events_probe_rereads_the_authoritative_state() -> None:
    payload = await run_probe("admin-task-events")
    assert payload["kind"] == "synthetic_loopback_admin_job_sse"
    assert payload["productionBundle"] is True
    assert payload["productionHostNoAutomaticReads"] is True
    assert payload["httpScenariosPassed"] == 6
    assert payload["developmentScenariosPassed"] == 4
    # A history that expired is re-read instead of being shown as success, a big cursor resumes,
    # duplicate reads are suppressed and reconnection is never automatic.
    assert payload["historyExpiredReread"] is True
    assert payload["largeCursorResume"] is True
    assert payload["duplicateReadSuppression"] is True
    assert payload["noAutomaticReconnect"] is True
    assert payload["wrongIdentityRejected"] is True
    assert payload["failedSnapshotDidNotAdvance"] is True
    assert payload["mobileOverflow"] is False
    assert_quiet(payload)


async def test_the_task_recovery_probe_never_turns_unknown_into_success() -> None:
    payload = await run_probe("admin-task-recovery")
    assert payload["productionBundle"] is True
    assert payload["noAutomaticRecoveryRequests"] is True
    assert payload["lostCancelOriginalKeyAndTarget"] is True
    assert payload["inflightReloadOriginalKey"] is True
    # A save failure whose outcome is unknown must stay unknown, and an expired cursor is cleared.
    assert payload["acknowledgementSaveFailureUnknown"] is True
    assert payload["expiredCursorClearedAcrossReload"] is True
    assert payload["largeCursorReloadResume"] is True
    assert payload["deniedWritePreventedPost"] is True
    assert payload["changedOwnerPreserved"] is True
    assert payload["corruptStoragePreserved"] is True
    assert payload["deniedReadNoRenderCrash"] is True
    assert payload["keyboardConfirmation"] is True
    assert payload["cancellations"] == 6
    assert payload["mobileOverflow"] is False
    assert_quiet(payload)


async def test_the_manual_prompter_probe_stays_local_and_narrow_safe() -> None:
    # This probe was stale until it was pointed at the specific live region it means; the page
    # legitimately has two of them (a local operation status and a saved-deployment snapshot).
    payload = await run_probe("prompter")
    assert payload["kind"] == "production-manual-prompter-synthetic"
    assert payload["exact_text"] is True
    assert payload["explicit_restore_manual"] is True
    assert payload["keyboard_and_focus"] is True
    assert payload["viewport_width"] == 360
    assert payload["horizontal_overflow"] is False
    # Manual reading must not touch the microphone, any API or the network.
    assert payload["microphone_calls"] == 0
    assert payload["api_requests"] == 0
    assert payload["outbound_requests"] == 0
    assert payload["page_errors"] == 0
    # Its own qualification statement: manual in-browser reading only.
    assert payload["qualification"] == "manual-browser-reading-only"


async def test_the_tts_host_probe_keeps_late_audio_and_url_revocation_honest() -> None:
    # Repaired on 2026-10-25: the page had started reading the project catalogue, which this
    # probe's synthetic server did not model. It now answers that route from the contract example.
    payload = await run_probe("tts-host")
    assert payload["production_ui"] is True
    assert payload["synthetic_loopback"] is True
    assert payload["monitored_contexts"] == 3
    assert payload["uploads"] == 3
    assert payload["creates"] == 3
    assert payload["input_utf8_bytes"] == 42
    assert payload["audio_bytes"] == 48044
    assert payload["audio_duration_seconds"] == 1
    assert payload["original_keys_and_versions"] is True
    assert payload["refresh_read_only"] is True
    assert payload["hint_and_original_input_recovery"] is True
    assert payload["native_wav_preview_and_download"] is True
    # Audio that arrives after the page moved on must be discarded, and switching revokes the URL.
    assert payload["late_content_discarded"] is True
    assert payload["switch_revokes_url"] is True
    assert payload["keyboard_and_360px"] is True
    assert payload["fake_excluded"] is True
    assert payload["handler_errors"] == []
    assert payload["outbound_requests"] == 0
    assert payload["page_errors"] == 0
    # Its own non-qualification statement.
    assert payload["real_backend_or_model_qualification"] is False
