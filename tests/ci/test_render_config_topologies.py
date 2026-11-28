"""The read side reads the database its assembly state says it reads.

#1280 moves the coordination database into the compute cluster in stages. The
dangerous state is the intermediate one: the readiness report and renewal are the
first units moved, and if the renderer kept sending them through the reverse tunnel
(or sent them to a ``minio``/engine Service that the compute cluster does not have)
the receipts would keep being signed from the old database -- green, and wrong, the
same shape as #1253. The engine/object-storage hop and the database hop therefore
have to be two switches, and ``data-plane`` is the one topology where they disagree.

This test imports ``render-config.py`` and calls the pure function that decides the
readiness values. It never calls kubectl, reads ``.runtime`` or touches a cluster.

Mutations that must turn it red:

- collapsing the two switches into one (making ``database_loopback`` equal
  ``loopback``): the ``data-plane`` case then expects the tunnel DSN and fails;
- moving the database with the object storage (making ``data-plane`` a synonym of
  ``in-cluster``): the object-storage/engine loopback assertions fail;
- sending ``data-plane`` at ``minio:9000`` / ``engine-asr-1`` before those Services
  exist in the compute cluster: the same assertions fail;
- adding an application-plane Secret to the ``data-plane`` render set, which is how
  a second coordination domain would get assembled while the writers are still on
  the old database.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
RENDER_CONFIG = ROOT / "deploy/cluster/app-plane/render-config.py"

ENV = {
    "SPEECHNEXUS_REGISTRATION_DATABASE_URL": "postgresql://speechnexus:pw@postgres:5432/speechnexus",
    "MINIO_ROOT_USER": "speechnexus",
    "MINIO_ROOT_PASSWORD": "pw",
}

TUNNEL_PG = "postgresql://speechnexus:pw@127.0.0.1:15440/speechnexus"
CLUSTER_PG = "postgresql://speechnexus:pw@postgres:5432/speechnexus"
TUNNEL_S3 = "http://127.0.0.1:19100"
CLUSTER_S3 = "http://minio:9000"
TUNNEL_ENGINE = "http://127.0.0.1:18190/health"
CLUSTER_ENGINE = "http://engine-asr-1:18190/health"


class Renderer(Protocol):
    """The two things this test reads from the deployment renderer."""

    _DATA_PLANE_SECRETS: tuple[str, ...]

    def readiness_env(self, env: dict[str, str], topology: str) -> dict[str, str]: ...


def load() -> Renderer:
    spec = importlib.util.spec_from_file_location("render_config", RENDER_CONFIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Renderer, module)


RENDERER = load()


class ReadinessEndpointTests(unittest.TestCase):
    def readiness(self, topology: str) -> dict[str, str]:
        return RENDERER.readiness_env(ENV, topology)

    def test_tunnel_reads_the_old_database_and_the_loopback_engine(self) -> None:
        values = self.readiness("tunnel")
        self.assertEqual(values["SPEECHNEXUS_DATABASE_URL"], TUNNEL_PG)
        self.assertEqual(values["SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT"], TUNNEL_S3)
        self.assertEqual(values["SPEECHNEXUS_ENGINE_HEALTH_URL"], TUNNEL_ENGINE)

    def test_data_plane_reads_the_cluster_database_while_the_rest_stays_on_loopback(
        self,
    ) -> None:
        values = self.readiness("data-plane")
        self.assertEqual(values["SPEECHNEXUS_DATABASE_URL"], CLUSTER_PG)
        self.assertEqual(values["SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT"], TUNNEL_S3)
        self.assertEqual(values["SPEECHNEXUS_ENGINE_HEALTH_URL"], TUNNEL_ENGINE)

    def test_in_cluster_reaches_everything_by_cluster_name(self) -> None:
        values = self.readiness("in-cluster")
        self.assertEqual(values["SPEECHNEXUS_DATABASE_URL"], CLUSTER_PG)
        self.assertEqual(values["SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT"], CLUSTER_S3)
        self.assertEqual(values["SPEECHNEXUS_ENGINE_HEALTH_URL"], CLUSTER_ENGINE)

    def test_the_database_switch_is_separate_from_the_loopback_hop(self) -> None:
        # If the two switches were collapsed this pair would be equal, and the
        # intermediate state could not be rendered at all.
        self.assertNotEqual(TUNNEL_PG, CLUSTER_PG)
        states = {
            topology: self.readiness(topology)["SPEECHNEXUS_DATABASE_URL"]
            for topology in ("tunnel", "data-plane", "in-cluster")
        }
        self.assertEqual(states["data-plane"], states["in-cluster"])
        self.assertEqual(states["tunnel"], TUNNEL_PG)
        self.assertEqual(states["data-plane"], CLUSTER_PG)


class DataPlaneObjectSetTests(unittest.TestCase):
    """The database move renders its own objects and nothing of the writing side."""

    def test_the_render_set_is_the_database_move_and_the_read_side(self) -> None:
        self.assertEqual(
            tuple(RENDERER._DATA_PLANE_SECRETS),
            ("postgres-credentials", "migrate-credentials", "readiness-env"),
        )

    def test_no_application_plane_secret_is_rendered(self) -> None:
        rendered = set(RENDERER._DATA_PLANE_SECRETS)
        for writer in ("api-env", "dispatch-env", "admission-watch-env", "workbench-api-env"):
            self.assertNotIn(writer, rendered)


if __name__ == "__main__":
    unittest.main()
