import { parse, type Contracts } from "../src/zero_dispatch";

const original = parse("OriginalExecutionKey", {} as unknown);
const deadline: string = original.identity.deadline_at;
const voice: string | null | undefined = original.identity.voice_revision_id;
const outputStream: string | null = original.output_stream_id;
const cancellation = parse("ReceiveCancellationResult", {} as unknown);
const version: string = cancellation.cancellation.gate_version_at_receive;
const received: "recorded" | "replayed" = cancellation.outcome;
const snapshot = parse("OriginalRecoverySnapshot", {} as unknown);
const status: "found" | "missing" | "incomplete" | "unknown" = snapshot.status;

// @ts-expect-error A query cannot carry a success proof or authorize dispatch.
const permit: boolean = snapshot.may_send;
// @ts-expect-error No seal writer is exported by this candidate.
type Seal = Contracts["SealZeroRequest"];
// @ts-expect-error There is no network client or side-effect entrypoint.
import { createClient } from "../src/zero_dispatch";

void [deadline, voice, outputStream, version, received, status, permit, createClient];
export type { Seal };
