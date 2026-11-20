"""Capture this image's vLLM-Omni `serve` CLI surface and one accept/reject pair.

Run *inside* the pinned image, which is why it is stored next to the recorded
fixtures instead of under `tests/`' importable tree:

    IMG=vllm/vllm-omni@sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52
    docker run --rm --entrypoint python $IMG capture_argv.py serve /nonexistent --help=all
    docker run --rm --entrypoint python $IMG capture_argv.py serve /nonexistent \
        --task-type CustomVoice

`vllm-omni --help` aborts on a host without CUDA: vLLM's `DeviceConfig` default
factory asks `current_platform.device_type` while the parser is built and raises
`RuntimeError: Failed to infer device type`. Forcing the platform's device type to
`"cpu"` keeps the parser construction and the argv parse in the image's own code -
only the device is substituted - so the accept/reject decision recorded here is the
image's, and the only unproven step remains loading real weights on a GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

import vllm.platforms as platforms  # type: ignore[import-not-found]

platforms.current_platform.device_type = "cpu"

from vllm_omni.entrypoints.cli import main as omni_main  # type: ignore[import-not-found]  # noqa: E402

# The two version banners the recorded fixtures keep; the dist-info path carries
# both the package and the version, e.g. vllm_omni-0.32.0rc1.dist-info/METADATA.
for package in ("vllm_omni-", "vllm-"):
    metadata = sorted(
        Path("/usr/local/lib/python3.12/dist-packages").glob(f"{package}*.dist-info/METADATA")
    )
    print(f"{package.rstrip('-')}: {metadata[0]}")

sys.argv = ["vllm-omni", *sys.argv[1:]]
try:
    omni_main.main()
except SystemExit as error:
    # Printed so a capture keeps the exit code next to the argparse message.
    print(f"exit={error.code}")
