# SpeechNexus shared media kernel

`speechnexus-media` contains native media types, exact source clocks and the
bounded PyAV decoder extracted from the execution gateway. It imports no
backend, gateway, controller or generated DTO. Core types require only Python;
install `[audio]` to use the decoder or private inspection command.

The gateway's original `domain.media` and `infrastructure.pyav_audio` imports
remain compatibility exports of the same objects. Codec behavior and exception
identity are preserved. Source provenance is in `source-inventory.json`.

## Private inspection entrypoint

In a separately built, immutable runtime image, the existing A02 supervisor may
run the following fixed command with server-selected limits and format:

```sh
python -I -m speechnexus_media.inspection --media-type audio/wav \
  --max-encoded-bytes 1048576 --max-duration-ms 60000 --timeout-seconds 10
```

The only input is the supervisor's read-only `/input/media`. No client path,
URL, command or environment is accepted. Raw PCM requires explicit rate and
channels; no format is inferred from the filename or MIME. The command hashes
the bounded encoded input, exhausts the decoder, discards PCM blocks and emits
one small JSON receipt binding source digest/length to observed sample facts.
This initial private profile accepts PCM WAV and explicitly described raw PCM.
WAV requires complete RIFF chunk headers, bodies and odd-length padding, one
format chunk followed by one frame-aligned data chunk, and header sample facts
matching the actual complete decode. Complete metadata chunks before or after
audio are allowed; duplicate format/data chunks are rejected by this narrow
profile. Chunk traversal shares the inspection deadline and cancellation checks.
FLAC/MP3/Ogg remain available to B through the unchanged shared decoder,
but are not yet admitted by this inspection profile.
No partial result is printed on failure. The private result is not an Asset,
Operation, public error mapping, execution/stop receipt or budget permission.

## Deployment and acceptance boundary

This source package and its tests do not install an inspection image or enable
the public Asset path. A owns packaging/integration; B retains codec semantics
and qualification. Extraction preserves the original recipe `pcm-decode-1`.
The fixed image/interpreter, native libraries, no-fork/thread sandbox, aggregate
memory sizing and old-process reconciliation require target-runtime acceptance.
The Python deadline is cooperative; only the outer supervisor owns stop proof.

The PR includes supervisor tests with the actual decoder and a generated-receipt
AssetInspector consumer. Target deployment qualification, B compatibility
acceptance and public audio errors/media projection remain pending before
composition-root/HTTP/PG/MinIO wiring. The inherited codec alone accepts a WAV
with its tail removed; the private inspector rejects inconsistent RIFF length
or missing declared samples. Other container integrity and recovery cases
still require format-specific acceptance before public ready.
