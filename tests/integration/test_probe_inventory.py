"""Every C-side probe must be run by something, or be listed here with a reason.

Twice this session a probe was found that passed when run by hand but was wrapped by nothing, so
no gate ran it and its evidence was untraceable. My first scan was itself wrong: it counted
references inside `.runtime/`, which made orphaned probes look referenced. This test keeps the
inventory honest instead of relying on a scan being right.

A probe is considered run when its path appears in a test file, in the disposable PG probe driver
or in CI configuration. Anything else has to be listed as known debt below, with the domain and
why it is not gated yet; the list is checked for stale entries too.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.workbench_real_runtime import ROOT

PROBE_DIR = ROOT / "scripts" / "dev-env"
DRIVER = PROBE_DIR / "workbench-pg-probe.py"

# Probe -> (domain, why it is not gated yet). Removing an entry means gating the probe.
# The three below were run by hand on 2026-10-25 and FAILED, so they are stale rather than merely
# ungated: they must not be counted as evidence until someone repairs or retires them.
NOT_GATED: dict[str, tuple[str, str]] = {
    "workbench-manuscripts-probe.mjs": (
        "C02 manuscripts against generated synthetic loopback",
        "stale and partially repaired: the flow now verifies the catalogue and selects the "
        "manuscript, and the uncertain save is replayed with the original key, but '核对当前版本' "
        "stays disabled because the page remains busy; the run's own diagnostics prove it is not a "
        "server-side rejection (no handler errors) and not a missing replay (two attempts with the "
        "same key), so the cause is still unexplained; the gated real-runtime manuscripts probe is "
        "the live evidence and performs the same step successfully",
    ),
}


def runner_text() -> str:
    parts: list[str] = []
    for pattern in ("tests/**/*.py", "scripts/**/*.py", "scripts/**/*.sh", "Makefile"):
        for path in ROOT.glob(pattern):
            # This file names the debt list, so it must not count as a reference to it.
            if path.is_file() and path not in {DRIVER, Path(__file__)}:
                parts.append(path.read_text(errors="ignore"))
    for path in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        parts.append(path.read_text(errors="ignore"))
    parts.append(DRIVER.read_text(errors="ignore"))
    return "\n".join(parts)


def c_side_probes() -> list[str]:
    return sorted(
        path.name
        for path in PROBE_DIR.glob("*probe*")
        if path.suffix in {".mjs", ".py"}
        and path.name != DRIVER.name
        and path.name.startswith(("workbench-", "admin-"))
    )


def test_every_c_side_probe_is_either_run_or_known_debt() -> None:
    text = runner_text()
    probes = c_side_probes()
    assert probes, "the probe directory must not be empty"
    unrun = [name for name in probes if name not in text]
    unexpected = [name for name in unrun if name not in NOT_GATED]
    assert unexpected == [], (
        "these probes are run by nothing and are not listed as known debt: "
        f"{unexpected} — gate them or add them to NOT_GATED with a reason"
    )


def test_the_known_debt_list_has_no_stale_entries() -> None:
    text = runner_text()
    probes = set(c_side_probes())
    stale = [
        name
        for name in NOT_GATED
        if name not in probes or name in text  # gone, or now actually gated
    ]
    assert stale == [], f"these entries are stale and must be removed from NOT_GATED: {stale}"
    for name, (domain, reason) in NOT_GATED.items():
        assert domain and reason, f"{name} needs a domain and a reason"


def test_the_gate_driver_names_the_promoted_probes() -> None:
    driver = DRIVER.read_text()
    for probe in (
        "realtime-consent-probe.mjs",
        "realtime-asr-probe.mjs",
        "realtime-tts-probe.mjs",
        "orphan_probes.py",
    ):
        assert probe in runner_text() or probe in driver, probe
    # The wrapper that runs the promoted probes must be part of the gate.
    assert "test_workbench_orphan_probes.py" in driver
    assert "test_workbench_realtime_synthetic_probes.py" in driver
