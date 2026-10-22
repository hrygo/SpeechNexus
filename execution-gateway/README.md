# SpeechNexus execution gateway

This independently deployed process will own engine-facing submission,
fencing, abort, and completion evidence. The bootstrap exposes operational
health endpoints and verifies its PostgreSQL control-plane connection. Engine
adapter and submission barrier behavior remain pending P0 and P1.
