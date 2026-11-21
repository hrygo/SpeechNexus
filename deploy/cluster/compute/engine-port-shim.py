#!/usr/bin/env python3
"""Publish a controller-created engine on the host loopback.

The node controller creates engine containers without publishing ports, so an engine
it prepares is reachable only on its Docker network (``172.17.0.x:18190``). The
binding endpoint and the bridge tunnel both address ``127.0.0.1:18190``, so the
gateway and the in-cluster readiness check cannot reach it.

This process is the missing hop: it listens on the loopback and forwards every
connection to the engine's container address. It is a development adapter for a
real gap (the approved-template schema has no port field), not a production
component; the plan records it as a named deviation.

The container address changes with every prepare, so ``ENGINE_ADDRESS`` is read at
connect time from the environment of this process; restart the service after a new
instance is prepared with a different address.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading

LISTEN_HOST = os.environ.get("SPEECHNEXUS_ENGINE_SHIM_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SPEECHNEXUS_ENGINE_SHIM_LISTEN_PORT", "18190"))
TARGET = os.environ.get("SPEECHNEXUS_ENGINE_SHIM_TARGET", "")
BUFFER = 65536


def pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(BUFFER)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for side in (source, sink):
            with contextlib.suppress(OSError):
                side.shutdown(socket.SHUT_RDWR)


def handle(client: socket.socket, target: tuple[str, int]) -> None:
    try:
        upstream = socket.create_connection(target, timeout=10)
    except OSError as error:
        print(f"engine shim: cannot reach {target[0]}:{target[1]}: {error}", file=sys.stderr)
        client.close()
        return
    client.settimeout(None)
    upstream.settimeout(None)
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pump, args=(upstream, client), daemon=True).start()


def main() -> int:
    if not TARGET:
        raise SystemExit("SPEECHNEXUS_ENGINE_SHIM_TARGET must be host:port")
    host, _, port = TARGET.rpartition(":")
    target = (host, int(port))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(64)
    print(f"engine shim: {LISTEN_HOST}:{LISTEN_PORT} -> {target[0]}:{target[1]}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client, target), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
