"""Exclusive node-local gateway process lease; no TTL-based revocation or DB mutex.

All candidate containers for the same node/gateway MUST share this host directory. The
node identity and engine credentials belong to that host; a different node is a distinct
gateway. Keeping this fd locked closes the paused-old-process/replacement-process race.
"""

import fcntl
import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition

from speechnexus_gateway.domain.fencing import IngressUnavailableError


class ProcessIngressOwnership:
    def __init__(self, directory: Path, *, node_id: str, gateway_id: str) -> None:
        for value in (node_id, gateway_id):
            if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", value):
                raise ValueError("invalid node/gateway identity")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Length-separated identity avoids collisions between node and gateway names.
        name = (
            hashlib.sha256(f"{len(node_id)}:{node_id}{gateway_id}".encode()).hexdigest() + ".lock"
        )
        self._fd = os.open(
            directory / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._fd)
            raise IngressUnavailableError("another process still owns this gateway") from error
        self._node_id = node_id
        self._gateway_id = gateway_id
        self._pid = os.getpid()
        self._condition = Condition()
        self._users = 0
        self._closing = False
        self._closed = False
        self._bound = False

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def gateway_id(self) -> str:
        return self._gateway_id

    def bind_ingress(self) -> None:
        with self._condition:
            if self._bound or self._closed or self._closing or os.getpid() != self._pid:
                raise IngressUnavailableError("a gateway process has exactly one ingress guard")
            self._bound = True

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._condition:
            if self._closing or self._closed or os.getpid() != self._pid:
                raise IngressUnavailableError("gateway process lease is not owned")
            self._users += 1
        try:
            yield
        finally:
            with self._condition:
                self._users -= 1
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if os.getpid() != self._pid:
                raise IngressUnavailableError("a fork cannot close its parent gateway lease")
            if self._closed:
                return
            self._closing = True
            # Shutdown never revokes ownership while an external send may still resume.
            self._condition.wait_for(lambda: self._users == 0)
            if not self._closed:
                os.close(self._fd)
                self._closed = True
