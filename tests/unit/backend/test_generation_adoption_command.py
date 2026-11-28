"""The adoption command is a thin, auditable wrapper: wiring, exit codes, operator notes.

The transaction is proven by ``tests/integration/test_generation_adoption.py``; this module
proves the command's own surface, which is where an operator is either told the truth or
misled: no controller means no adoption (not a silent ledger edit), the node's answer is
checked against the instance that was asked about, and every failure carries a stable code
with an exit code a timer can branch on.
"""

from __future__ import annotations

import json

import pytest

from speechnexus_backend import generation_adoption
from speechnexus_backend.controller_head import (
    ControllerConsultationError,
    ControllerEndpoint,
    NodeInstanceHead,
)
from speechnexus_backend.engine_registration import (
    EXIT_CONFLICT,
    EXIT_UNAVAILABLE,
    RegistrationError,
)
from speechnexus_backend.generation_adoption import AdoptionResult, main

INSTANCE = "instance-asr-1"
NODE = "node-megacube-1"
ENDPOINT = {
    "SPEECHNEXUS_CONTROLLER_API_URL": "http://127.0.0.1:8080",
    "SPEECHNEXUS_CONTROLLER_API_TOKEN": "token",
}


def _result(*, outcome: str = "adopted", evidence: str = "node_reported_stopped") -> AdoptionResult:
    head = NodeInstanceHead(
        INSTANCE,
        11,
        11,
        stopped_generations=(1,),
        last_fencing_token=5,
    )
    return AdoptionResult(
        outcome=outcome,
        instance_id=INSTANCE,
        ledger_generation=1,
        adopted_generation=11 if outcome == "adopted" else None,
        previous_stop_evidence=evidence if outcome == "adopted" else None,
        intent_id="adopt-instance-asr-1-g11" if outcome == "adopted" else None,
        fencing_token=5 if outcome == "adopted" else None,
        pool_fencing_token_before=4,
        pool_fencing_token_after=5 if outcome == "adopted" else 4,
        adopted_at_us=1_000 if outcome == "adopted" else None,
        node_head=head,
        reason="synthetic",
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def read_head(
        endpoint: ControllerEndpoint, *, node_id: str, instance_id: str, **_: object
    ) -> NodeInstanceHead:
        captured["endpoint"] = endpoint
        captured["node_id"] = node_id
        return NodeInstanceHead(instance_id, 11, 11, stopped_generations=(1,), last_fencing_token=5)

    def adopt(**kwargs: object) -> AdoptionResult:
        captured["adopt"] = kwargs
        return _result()

    monkeypatch.setattr(generation_adoption, "read_head", read_head)
    monkeypatch.setattr(generation_adoption, "adopt_generation", adopt)
    return captured


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = 0
    try:
        main(["--database-url", "postgresql://operator@127.0.0.1:5432/synthetic", *args])
    except SystemExit as exit_code:
        code = int(exit_code.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_an_adoption_prints_the_facts_an_operator_needs(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)

    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == 0
    document = json.loads(out)
    assert document["outcome"] == "adopted"
    assert document["adopted_generation"] == 11
    assert document["previous_stop_evidence"] == "node_reported_stopped"
    assert document["pool_fencing_token"] == {"before": 4, "after": 5}
    assert document["node_head"]["stopped_generations"] == [1]
    # The operator is told the next step instead of guessing it.
    assert "speechnexus-register-engine --renew-readiness" in err
    assert recorded["node_id"] == NODE


def test_no_controller_means_no_adoption(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    # A ledger repair cites the node's testimony; without a controller there is none, and
    # substituting the operator's word for it is the refusal this command must keep.
    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == 2
    assert out == ""
    assert "controller_not_configured" in err
    assert "SPEECHNEXUS_CONTROLLER_API_URL" in err
    assert "adopt" not in recorded


def test_a_head_for_another_instance_is_refused(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        generation_adoption,
        "read_head",
        lambda *_, **__: NodeInstanceHead("instance-other", 11, 11, last_fencing_token=5),
    )

    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == EXIT_UNAVAILABLE
    assert out == ""
    assert "controller_head_invalid" in err


def test_an_unreachable_controller_is_an_unavailable_exit(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)

    def unavailable(*_: object, **__: object) -> object:
        raise ControllerConsultationError("controller_unreachable", "the controller is unreachable")

    monkeypatch.setattr(generation_adoption, "read_head", unavailable)

    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == EXIT_UNAVAILABLE
    assert out == ""
    assert "controller_unreachable" in err


def test_a_refusal_exits_with_the_conflict_code_and_prints_nothing(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)

    def refuse(**_: object) -> AdoptionResult:
        raise RegistrationError(
            "previous_generation_stop_unconfirmed", "no testimony", EXIT_CONFLICT
        )

    monkeypatch.setattr(generation_adoption, "adopt_generation", refuse)

    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == EXIT_CONFLICT
    assert out == ""
    assert "previous_generation_stop_unconfirmed" in err


def test_the_flag_reaches_the_transaction(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)

    _run(
        capsys,
        "--instance",
        INSTANCE,
        "--node",
        NODE,
        "--engine-url",
        "http://e",
        "--accept-missing-stop-evidence",
    )

    assert recorded["adopt"]["accept_missing_stop_evidence"] is True  # type: ignore[index]


def test_an_aligned_run_says_nothing_was_written(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        generation_adoption, "adopt_generation", lambda **_: _result(outcome="aligned")
    )

    code, out, err = _run(
        capsys, "--instance", INSTANCE, "--node", NODE, "--engine-url", "http://e"
    )

    assert code == 0
    assert json.loads(out)["outcome"] == "aligned"
    assert "nothing was written" in err


def test_the_missing_testimony_branch_is_announced_as_the_weaker_claim(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        generation_adoption,
        "adopt_generation",
        lambda **_: _result(evidence="missing"),
    )

    code, _out, err = _run(
        capsys,
        "--instance",
        INSTANCE,
        "--node",
        NODE,
        "--engine-url",
        "http://e",
        "--accept-missing-stop-evidence",
    )

    assert code == 0
    assert "without node stop evidence" in err
    assert "previous_stop_evidence=missing" in err


def test_a_missing_engine_url_is_refused_before_any_write(
    recorded: dict[str, object], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENDPOINT.items():
        monkeypatch.setenv(name, value)

    code, out, err = _run(capsys, "--instance", INSTANCE, "--node", NODE)

    assert code == 2
    assert out == ""
    assert "invalid_engine_url" in err
    assert "adopt" not in recorded
