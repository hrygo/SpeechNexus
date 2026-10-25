/** Local application state, not a public realtime DTO or server Transcript version. */
export type ReadingScript = Readonly<{ id: string; revision: number; text: string }>;
export type FollowContext = Readonly<{
  scriptId: string;
  scriptRevision: number;
  streamEpoch: string;
  generation: number;
}>;
export type MatchDecision =
  "advanced" | "held" | "ambiguous" | "unmatched" | "insufficient" | "stale";
type Token = { word: string; start: number; end: number };
const SCRIPT_LIMIT = 20_000;
const WINDOW = 160;

function integer(
  value: unknown,
  min: number,
  max = Number.MAX_SAFE_INTEGER,
): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < min || (value as number) > max)
    throw new Error("Invalid reading position or version");
}
function text(value: unknown, limit: number): asserts value is string {
  if (
    typeof value !== "string" ||
    value.length > limit * 2 ||
    /[\uD800-\uDFFF]/u.test(value) ||
    value.includes("\0") ||
    Array.from(value).length > limit
  )
    throw new Error("Invalid or oversized reading text");
}
function identifier(value: unknown): asserts value is string {
  text(value, 128);
  if (!value.trim()) throw new Error("Missing reading identity");
}
function record(value: unknown, keys: string[]): asserts value is Record<string, unknown> {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).length !== keys.length ||
    !keys.every((key) => Object.hasOwn(value, key))
  )
    throw new Error("Invalid reading checkpoint");
}
function scriptValue(value: unknown): ReadingScript {
  record(value, ["id", "revision", "text"]);
  identifier(value.id);
  integer(value.revision, 1);
  text(value.text, SCRIPT_LIMIT);
  if (!value.text.trim()) throw new Error("Reading text is empty");
  return Object.freeze({ id: value.id, revision: value.revision, text: value.text });
}
function tokenize(value: string): Token[] {
  const tokens: Token[] = [];
  let codepoints = 0;
  for (const part of new Intl.Segmenter("und", { granularity: "word" }).segment(value)) {
    const start = codepoints;
    codepoints += Array.from(part.segment).length;
    if (part.isWordLike)
      tokens.push({ word: part.segment.normalize("NFKC").toLowerCase(), start, end: codepoints });
  }
  return tokens;
}

export class FollowSession {
  readonly #script: ReadingScript;
  readonly #length: number;
  readonly #tokens: Token[];
  #position = 0;
  #resumePosition = 0;
  #generation = 0;
  #context: FollowContext | null = null;
  #sequence = 0;

  constructor(script: ReadingScript) {
    this.#script = scriptValue(script);
    this.#length = Array.from(this.#script.text).length;
    this.#tokens = tokenize(this.#script.text);
  }
  snapshot() {
    return Object.freeze({
      script: this.#script,
      position: this.#position,
      resumePosition: this.#resumePosition,
      mode: this.#context ? ("following" as const) : ("manual" as const),
      streamEpoch: this.#context?.streamEpoch ?? null,
      generation: this.#generation,
    });
  }
  /** Keep this exact context in the subscription/computation closure; never retag an old result. */
  beginFollowing(streamEpoch: string): FollowContext {
    identifier(streamEpoch);
    this.#generation++;
    this.#sequence = 0;
    this.#context = Object.freeze({
      scriptId: this.#script.id,
      scriptRevision: this.#script.revision,
      streamEpoch,
      generation: this.#generation,
    });
    return this.#context;
  }
  pause() {
    this.#generation++;
    this.#context = null;
  }
  seek(position: number) {
    integer(position, 0, this.#length);
    if (this.#context) this.#resumePosition = this.#position;
    this.pause();
    this.#position = position;
  }
  restoreResumePoint() {
    this.seek(this.#resumePosition);
  }

  acceptHypothesis(
    context: FollowContext,
    input: { sequence: number; text: string },
  ): MatchDecision {
    integer(input.sequence, 1);
    text(input.text, 256);
    // Reference identity also fences another session with coincidentally identical local counters.
    if (
      context !== this.#context ||
      context.scriptId !== this.#script.id ||
      context.scriptRevision !== this.#script.revision ||
      context.streamEpoch !== this.#context?.streamEpoch ||
      context.generation !== this.#generation ||
      input.sequence <= this.#sequence
    )
      return "stale";
    this.#sequence = input.sequence;
    const words = tokenize(input.text).map((token) => token.word);
    if (Array.from(words.join("")).length < 4) return "insufficient";
    const ends: number[] = [];
    const lower = Math.max(0, this.#position - WINDOW);
    const upper = Math.min(this.#length, this.#position + WINDOW);
    for (let index = 0; index < this.#tokens.length; index++) {
      const start = this.#tokens[index];
      if (!start) break;
      if (start.start < lower) continue;
      if (start.start >= upper) break;
      const end = this.#tokens[index + words.length - 1];
      if (!end || end.end > upper) continue;
      if (words.every((word, offset) => this.#tokens[index + offset]?.word === word))
        ends.push(end.end);
    }
    if (ends.length > 1) return "ambiguous";
    const end = ends[0];
    if (end === undefined) return "unmatched";
    if (end <= this.#position) return "held";
    this.#position = end;
    return "advanced";
  }
  checkpoint(): string {
    return JSON.stringify({
      version: 1,
      script: this.#script,
      position: this.#position,
      resumePosition: this.#resumePosition,
    });
  }
}

/** Untrusted same-origin local storage, with no automatic stream resumption. */
export function restoreCheckpoint(raw: string): FollowSession {
  if (raw.length > 200_000) throw new Error("Reading checkpoint is too large");
  const value: unknown = JSON.parse(raw);
  record(value, ["version", "script", "position", "resumePosition"]);
  if (value.version !== 1) throw new Error("Unsupported reading checkpoint");
  const session = new FollowSession(scriptValue(value.script));
  const length = Array.from(session.snapshot().script.text).length;
  integer(value.position, 0, length);
  integer(value.resumePosition, 0, length);
  // Seed the takeover point using the same transition as a live takeover, then retire it.
  session.seek(value.resumePosition);
  session.beginFollowing("checkpoint-restore");
  session.seek(value.position);
  return session;
}
