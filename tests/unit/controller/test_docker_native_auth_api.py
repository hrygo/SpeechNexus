"""Exercise the actual Docker JSON boundary, not just its in-memory double."""

import json

import httpx

from speechnexus_node_controller.infrastructure.docker_api import DockerApi


def test_docker_api_creates_and_inspects_the_native_auth_configuration() -> None:
    requests: list[httpx.Request] = []
    key = "ab" * 32

    def daemon(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"Id": "container-synthetic"})
        if request.url.path.endswith("/stats"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "Id": "container-synthetic",
                "Image": "sha256:" + "c" * 64,
                "State": {"Status": "running", "Running": True},
                "Config": {"Cmd": ["--model", "synthetic"], "Env": ["VLLM_API_KEY=" + key]},
            },
        )

    with DockerApi() as api:
        api._client.close()
        api._client = httpx.Client(
            transport=httpx.MockTransport(daemon), base_url="http://docker-engine"
        )
        identifier = api.create_container(
            name="synthetic",
            image_reference="synthetic:pin",
            command=("--model", "synthetic"),
            labels={},
            host_config={},
            environment=("VLLM_API_KEY=" + key,),
        )
        assert json.loads(requests[0].content)["Env"] == ["VLLM_API_KEY=" + key]
        observed = api.inspect_container(identifier)
        assert observed is not None
        assert observed.command == ("--model", "synthetic")
        assert observed.environment == ("VLLM_API_KEY=" + key,)
        assert observed.image_id == "sha256:" + "c" * 64
        assert key not in repr(observed)
