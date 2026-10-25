"""Trusted image trampoline: transfer self pidfd before executing any payload."""

from __future__ import annotations

import array
import os
import socket
import sys


def main() -> None:
    with socket.socket(fileno=0) as control:
        identity = os.pidfd_open(os.getpid())
        try:
            control.sendmsg(
                [b"speechnexus-inspection-v1"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [identity]))],
            )
        finally:
            os.close(identity)
        if control.recv(1) != b"G":
            raise ValueError("Inspection start was not acknowledged")
    # The payload must never inherit the control channel or forge an identity.
    null = os.open("/dev/null", os.O_RDONLY)
    if null != 0:
        os.dup2(null, 0)
        os.close(null)
    os.set_inheritable(0, True)
    command = sys.argv[1:]
    if not command or not command[0].startswith("/"):
        raise ValueError("Invalid private inspection command")
    os.execv(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        os._exit(78)
