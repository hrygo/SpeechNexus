# Audio Asset inspection boundary

This A02 unit supplies a **private Linux process supervisor**, not an audio
decoder or a public upload implementation. `AssetRuntime` still installs
`TextAssetInspector`; audio inspection remains unavailable. Existing public
Asset identities, original-key recovery, object versions and the deployment's
seven-day default retention are unchanged. No schema, lock, migration or grant
is changed by this unit.

The acceptance target is [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)
AC1/AC2 and the [file ASR roadmap](../plans/2026-10-25-team-a-realignment.md).
Public `ready` will require the later decoder, result validation and actual
HTTP/PG/MinIO acceptance below. A successful command is not a media fact.

## Independent deployment and reuse

The current reusable implementation is B's
`execution-gateway/src/speechnexus_gateway/infrastructure/pyav_audio.py`, with
`domain/media.py` and the [bounded media interface](file-asr-audio.md).
The backend and gateway are independent deployments under `pyproject.toml`'s
import boundary. The backend must not import gateway modules or run its VAD,
recognizer, engine client, deployment controller or GPU runtime.

The source inventory is frozen at integration base
`33233c8a42b284b627e891b32b1b28faeafb30e7`: `domain/media.py` has blob
`4d70acc4e8226a977ab84a0ab11ee0265d182020`, and `infrastructure/pyav_audio.py`
has blob `12684eb017b0dfdcc36a694f891cfdbc0ad51b37`. These identify reuse inputs,
not a published shared package or acceptance of that decoder in this sandbox.

The next shared change must agree a separately distributed media kernel and a
small inspection entrypoint with B. A owns packaging/consumer integration; B
retains codec semantics and qualification. The extraction must record the exact
source revision and source mapping, preserve existing B consumers/tests, and
serially update any shared dependency locks. Do not copy the decoder into A or
silently depend on the whole gateway wheel. The shared artifact does not exist
in this unit; neither its source nor B's implementation is modified here.

The inspection image must contain the pinned shared artifact, its native
dependencies, an explicitly configured Python interpreter, the inspection
command and `/usr/bin/prlimit`. Its `/inspection/launch.py` placeholder is
overmounted read-only from A's installed, fixed launcher source. Supply a minimal,
read-only image without deployment credentials, host directories or network
configuration. The supervisor accepts only server configuration, rejects `/`
as an image, and does not accept a client command, path, URL or environment.
Shape checks do not establish image provenance or immutability; the image build,
version/digest selection and target-runtime qualification remain prerequisites
before composition-root activation. No image is built or installed here.

## Process and resource boundary

`speech/infrastructure/audio_inspection_process.py` runs one local inspection
at a time per instance. Its limits are explicit server inputs; this is not
cluster admission or a shared compute-budget ledger.

| Boundary | Enforcement / remaining qualification |
| --- | --- |
| Isolation | bubblewrap requires a user namespace and isolates network, PID, IPC and UTS namespaces; capabilities are dropped and further user namespaces disabled. No unsandboxed fallback. |
| Input | Flush and bound the private regular file, reopen it read-only, then use `--ro-bind-data` to create an anonymous, read-only `/input/media`. No API descriptor-directory access or persistent named copy. |
| Filesystem | Runtime image, `/input/media`, `/proc`, `/dev` and `/tmp` are read-only; no writable scratch tree. stdout is the only content-bearing writable regular file. |
| CPU / address space | Host `prlimit` installs equal hard/soft CPU and address-space limits before namespace setup. They constrain the guardian and its one child individually. Budget both bounded processes and outer setup overhead; these are not aggregate CPU/RSS or cgroup limits. |
| Process multiplication | Only the fixed read-only launcher can fork once during trusted bootstrap. The non-exec guardian is PID 1; both it and the actual worker install additional seccomp filters denying fork/vfork/clone/clone3 before any untrusted command can execute. All stages deny unshare/setns. Supported syscall ABIs are Linux x86_64/aarch64; another ABI is unavailable. |
| Encoded input memory | The anonymous encoded copy is separately bounded by `input_bytes`; its pages are additional to the worker's address-space limit. Include both, plus bounded supervisor overhead, in deployment capacity sizing. No claim of a cgroup-wide memory limit. |
| Output | Setup's file-size limit permits only the bounded encoded input. Inside the image, `prlimit` narrows it to `output_bytes + 1` before executing the worker. Read at most that many bytes; excess or failure exposes no partial result. |
| Actual identity | Before forking, the fixed launcher receives the original API process pidfd. It transfers its guardian pidfd and its own unreaped child's actual worker pidfd over the private socket. Sealed-child readiness and original API acknowledgement precede payload exec. No client PID, wrapper report or recycled numeric PID identifies the payload. |
| Deadline / cancellation | The supervisor monitors setup and execution, signals the actual worker, guardian and separately owned outer pidfds, requires worker and guardian `POLLIN` plus outer wait/reap, and never uses a Python pre-exec callback. Outer exit or a changed process group alone is no stop proof. The guardian independently monitors the same absolute monotonic deadline. |
| Unknown stop | Failed kill or missing actual identity/exit proof yields `stop_unknown`; the local slot remains held even when the outer wrapper has exited. Startup without a recoverable identity is not automatically retried. |
| Parent crash | The guardian never execs and watches the original API pidfd during startup and execution. API exit causes an actual-worker pidfd kill and wait. This does not depend on exec preserving a parent-death signal. `--die-with-parent` remains defence in depth. Anonymous descriptors retain no named private copy; a fresh API still recovers Asset metadata/object outcomes through the existing ledger. |

The wall deadline starts before process creation. Process creation and kernel
I/O/reaping can exceed a requested deadline; an uninterruptible kernel task is
not proof of release. The supervisor reports `stop_unknown` rather than claiming
an absolute bound on physical termination. Confirmed kill/reap is only evidence
about this local inspection process, never an ASR engine stop receipt, Job
terminal state or permission to release gateway/instance budgets.

The launcher is trusted setup code. Its private version-3 socket carries only
the original API pidfd, bounded syscall programs, guardian/worker pidfds and
acknowledgement. Before forking, the guardian becomes nondumpable; it never
execs afterward. The child closes inherited API/guardian pidfds and the API
control socket, restores stdin to `/dev/null`, installs its stricter filter,
reports sealing readiness and waits for the guardian's go-ahead. The guardian
checks API liveness and the absolute deadline while waiting for readiness and
API acknowledgement. Thus a child fork preceding readiness does not expose an
untrusted execution window or require the guardian to arm a death signal later.
The child closes its final private gate before exec.

All stages deny ptrace, process_vm read/write, kcmp, pidfd_getfd and io_uring.
The payload additionally denies process signal syscalls, including queued
signals; it cannot signal or inspect guardian memory. These restrictions and
guardian nondumpability do not rely on the host's Yama setting. The payload's
fork/thread and `PR_SET_PDEATHSIG` prohibitions remain. The bootstrap allows
only trusted setup to fork; it is not an execution mode for a decoder.

A required pidfd API missing from the host fails closed; there is no PID-only
stop qualification. Parent-death signals can be cleared by kernel exec/LSM
transitions and are tied to the creating Linux thread. They are never treated
as an acknowledged stop when a request thread finishes. The guardian's original
API process identity and actual worker exit observations supply the independent
liveness path.

One reentrant identity-lifetime lock covers creation, receipt, polling, signal,
wait and descriptor closure. `run()` owns it while using these identities;
`close()` requests cancellation through an event and does not claim success
when that ownership is unavailable. In particular, two closers cannot close a
pidfd while a third actor still holds its integer for a later signal. A controlled
barrier regression reproduces the earlier premature close and verifies the
descriptor remains valid until its last user finishes; its signal hook never
sends signals to another real process.

The current decoder is configured for single-thread processing, but the actual
native stack still needs qualification under this no-fork/no-thread policy.
If it requires another execution model, keep audio unavailable and review that
model explicitly; do not relax the policy silently. Missing bubblewrap/prlimit,
namespace restrictions, missing image files or command failure fail closed.
No host sysctl, role, production permission or sandbox policy is relaxed.

`close()` requests cancellation and returns true only after it can own the
local slot and reap the original process. A concurrent creator or failed stop
cannot produce a positive close receipt. The supervisor does not persist
process identities across API generations. Before enabling the public path,
the runtime must also define startup reconciliation/outer-container cleanup
and retain unavailable capacity if an old kernel task cannot be proved stopped;
the ordinary parent-crash test does not establish this exceptional case.

## Failure, cancellation and recovery

The supervisor's private result distinguishes complete, unavailable, busy,
failed, output-limit, timeout, cancelled and stop-unknown. Only complete exposes
bounded bytes. The adapter must still validate its result against the original
input size/digest, complete EOF decode and actual source sample clock; it must
not trust MIME, filename, a header-only probe, an incomplete stream or a worker's
unvalidated claim. No vendor stderr, credentials, private command or input is
put in public errors or logs.

Public mapping is a separate contract-first step. The current public asset
adapter has no stable audio rejection mapping. Do not turn worker/setup failure
into a permanent content rejection or hide an invented code in the global
fallback. Until the source contract and adapter are ready, use the existing
inspection-unavailable behavior and persist no fake ready or rejected audio.

The later wiring must preserve the original upload key and full intent. An
unavailable decoder on replay must not replace a previously accepted result.
Once an object PUT may have been sent, query the original object/version and
metadata outcome; do not re-PUT from a lost acknowledgement, switch keys or
claim that cancellation deleted an unknown object. Decoding/inspection never
runs inside a metadata transaction. Successful facts enter the existing private
staging, verification and atomic publication path. No new cleanup or physical
deletion authorization is introduced.

## Smallest next acceptance slice

1. Freeze the shared media artifact/entrypoint and minimal image ownership with
   B; build and qualify the real native decoder in this process boundary.
2. Decode the entire allowlisted container input, discard bounded PCM chunks,
   and validate complete sample counts/rate/channels and duration. No VAD,
   recognition, language inference or usage promise. Raw PCM needs explicit
   format metadata; the current Asset upload cannot infer it from bytes.
3. Freeze any missing source-contract audio errors and result projection before
   wiring A's inspector/HTTP consumer. Preserve existing text behavior.
4. In isolated PG/MinIO, exercise actual HTTP with valid audio, corrupt/truncated
   media, input/decoded-duration limits, cancellation and hard timeout. Check
   zero object/metadata publication on inspection-unavailable and no partial
   PCM or result becoming ready.
5. Exercise original-key replay/conflict, process/API restart, lost PUT/publish
   acknowledgement, fixed storage versions and seven-day default TTL. These
   are distinct from process supervision tests and remain unaccepted here.

## Validation scope

`tests/unit/backend/test_audio_inspection_process.py` uses real Linux processes,
namespaces, seccomp and resource limits with synthetic bytes and tiny Python
commands. It checks read-only input/scratch, environment stripping, failed
connection to a parent loopback listener, denied fork/thread creation, memory
and CPU limits, bounded output, wall timeout, cancellation, occupied capacity,
unconfirmed kill, interrupt cleanup and parent crash. A real kill-denial scenario
is injected to exercise unknown-stop bookkeeping; it is not evidence of an
actual uninterruptible kernel task.

P1 review reproduced the old bug on source `306bc2f`: a controlled payload could
clear its parent-death signal and change session, after which the outer wait
returned cancelled while an independent pidfd still showed it alive. The probe
explicitly killed and observed that owned payload before finishing. Regressions
now verify denied parent-death changes, detached execution with actual exit
proof, and a reaped outer/live actual identity that cannot free the slot when
the actual stop is unavailable. This is a lifecycle test, not evidence of a host
filesystem/network escape or an uninterruptible kernel task.

The parent-crash test relays stable outer, guardian and actual worker pidfds
from an owned API helper, crashes that helper without cleanup, and requires all
three kernel exit observations within the original three-second assertion.
It covers running and pre-acknowledgement phases; wrapper exit alone cannot
satisfy it. A separate controlled bootstrap clears its own death signal before
the production bootstrap filter, then runs the same launcher and handshake.
That regression reproduced surviving actual identities before the guardian fix;
it simulates exec clearing without changing host policy and is not a namespace
or AppArmor qualification. Safe probes cover denied peer-memory/signal access,
inaccessible guardian memory and absent inherited control descriptors.
The image fixture copies
only the current interpreter/stdlib, prlimit and their linked libraries; it
does not mount the host root or load a model. CI installs bubblewrap only in the
Python test job and never skips these tests or relaxes namespace policy.

Initial failed probes remain relevant: direct descriptor path binding was not
available after isolation; the anonymous FD transport replaces it without new
permissions. A buffered-input regression exposed stale bytes before flush and
was fixed in the supervisor. The crash probe initially depended on a missing
kernel observation file and was corrected to observe the actual child identity.
These process tests establish no media, HTTP/PG/MinIO, GPU, multi-host or Gate
qualification. Those require the next slice above.

The first CI failed 14 new cases; the diagnostic head then reported 18 fixture
errors with `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`.
That locates failure at loopback address setup, not at decoding or dependency
resolution. Ubuntu's installed tool was 0.9.0, versus local 0.12.0; both upstream
versions perform this address operation, so version difference alone is not a
root-cause finding or a packaging fix. The CI job only reads fixed namespace/LSM
context after failure. No global sysctl, capability, runner or network-isolation restriction was
relaxed. The separately approved one-time policy experiment below exposed an
additional crash-lifecycle failure. CI and target qualification remain open.

### Consumed one-time CI experiment

Installing `bubblewrap` does not install its AppArmor policy. The official
Noble `apparmor-profiles_4.0.1really4.0.1-0ubuntu0.24.04.9_all.deb` contains the
otherwise disabled `usr/share/apparmor/extra-profiles/bwrap-userns-restrict`.
The approved package SHA256 was
`90b02aa006eea7702cd4e851343e469e41365dda42145a3cb035de1d6c773b8c`;
the extracted rule SHA256 was
`11d39094f044f0cda0febb3ad517b830301da6b2ce929664af09ee9e4dd264f9`.

The user approved exactly one temporary Python job VM experiment. Its immutable
source was `f8b592fd732babe41788e64f9de80151037c0a0c`, based on main
`58bc51b3e592eadfcc11fdac948bb2d2b6ad4ff4` and the then-current supervisor.
[Run 40186938755, attempt 1](https://github.com/hrygo/SpeechNexus/actions/runs/40186938755)
used Python job `121149688551` and runner `1000056216`. The immutable approval
commit `83a77770a779cfa5bb41fb683a81e7a191a8a119` bound those exact identities.
No experiment workflow or policy-loader implementation enters this PR.

At 10:59:53 UTC the job loaded only the verified `bwrap` and `unpriv_bwrap`
rules. The real startup probe passed, and frozen-baseline compatibility checks
passed. The full non-integration suite had **4139 passed and 1 failed**, coverage
81.66%; the failure was the original API-crash stop assertion. Media smoke and
wheel build did not run. This is a failed full validation, not successful CI.
At 11:02:29 UTC cleanup recorded `remaining_new_uid_processes=[]` and
`profiles_after_cleanup={}`. Only the two newly loaded rules were removed.
Artifact `12597160172` preserves the receipt, exact rule and consumed marker;
its ZIP SHA256 is
`246f141141cf683dd25d932476da3b0b54133e69b4bac66258f9a5ca3e12d388`.

The authorization was consumed by this VM. It does not permit another dispatch,
rerun, rule load or permanent on-push policy step. Further policy experiments
or lasting CI configuration need explicit approval. The source branch and
failure/cleanup receipts remain historical evidence; later code cannot inherit
that run's result.

The logs did not distinguish the surviving numeric PID. Upstream
[Linux v6.17 AppArmor exec logic](https://github.com/torvalds/linux/blob/v6.17/security/apparmor/lsm.c#L961-L976)
clears `pdeath_signal` on some label changes, supporting a concrete failure
mechanism without proving the exact Ubuntu binary's register state. A local
controlled exec-clear regression reproduced the old actual-worker survival.
The non-exec guardian and original API pidfd remove reliance on this signal;
local RED→GREEN and ordinary namespace tests remain separate from validation
under the CI AppArmor rules. That CI validation is still pending.
