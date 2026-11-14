/** Private, tab-scoped intent barrier. Never log the command or infer a remote rollback. */
export type JournalStorage = Pick<Storage, "getItem" | "setItem">;

/**
 * What a refusal settles for the key that carried it (#755), reduced to the one property a
 * reload has to know (#798).
 *
 * `retryable` is a refusal whose own next step is to dispatch the same intent again (the
 * 429 and the cleanup wait); `terminal` is one that repeating the same request cannot get
 * past. The journal keeps only the classification, not the vocabulary that states it: the
 * refusal codes and their copy live in the consumer that owns them
 * (`books/render-refusals.ts`), and copying them here would give the two a way to
 * disagree. Keeping the classification is what makes a reload offer the action the tab
 * offered before it, instead of guessing from a lone "was refused" bit (#798).
 */
export type RefusalCategory = "retryable" | "terminal";

/**
 * The refusal a record carries: its category, or the fact that a refusal was recorded
 * without one.
 *
 * `unclassified` is what the records this build no longer writes carry:
 * - every record written before #798 held only `rejected`, which says a refusal was
 *   observed but not whether the same key may be re-sent;
 * - every record written before #803 by the export and assistant panels held
 *   `unclassified` for every refusal. Those two wrote no category because nothing had
 *   told them one -- not because their refusals meant something else (#803): their
 *   refusals are the same "this key may / may not be re-sent" answer, stated by the
 *   contract's own `error.retryable`.
 *
 * It is read -- never `retryable` -- because the lost information cannot be recovered:
 * neither source can say whether the key was spent. Reading it as `retryable` would let
 * a reload re-send a key a terminal refusal had already spent, so `refusedReplayable`
 * takes the one direction that cannot.
 *
 * `unclassified` is also final once written: `save(load())` writes back exactly the value
 * the one determinate reading produced, so the migration loses nothing further -- but no
 * later reader can turn it into a category. The only way out of that record is the
 * panel's explicit end (dismiss), not a better guess.
 */
export type RecordedRefusal = RefusalCategory | "unclassified";

/**
 * The classification one answer writes down, from the two facts its consumer holds (#803).
 *
 * `settled` is the caller's own "this answer settles the attempt" bit: the server refused
 * and created nothing, so no Job can exist under the retained key. It is false for an
 * unobserved answer (a transport failure or a body-receive timeout, which may have
 * crossed the network) and for the two answers that are explicitly *not* settlements: a
 * 5xx, and the 429 whose own step is "wait, then dispatch again".
 *
 * `retryable` is the contract's retry permission for that refusal (`error.retryable`,
 * required on every `ErrorBody`) and is only read when `settled` is true. It is never a
 * code table copied here: the render panel mirrors the one published dispatch vocabulary
 * per code because it also has to render the operator step for each code
 * (`books/render-refusals.ts`), while a panel with no such vocabulary reads the field
 * itself. `tests/contract/test_render_refusal_vocabulary.py` keeps those two equal for
 * the codes that have both, and `tests/contract` keeps the field itself required, so the
 * three consumers classify one refusal from one promise instead of three tables (#803).
 */
export function refusedCategory(settled: boolean, retryable: boolean): RecordedRefusal | null {
  if (!settled) return null;
  return retryable ? "retryable" : "terminal";
}

/**
 * The single reading of a record's refusal: may the retained key be re-sent? (#803)
 *
 * The three consumers -- `RenderUnitPanel` through `books/recovered-intent.ts`,
 * `BookExportSession` and `AssistantSession` -- all read this answer, so one refusal
 * cannot offer a replay in one panel and withhold it in another.
 *
 * - `null` (no refusal observed): **yes**. The key is the only way to find a Job the
 *   request may have created, so withholding it would strand the operator.
 * - `retryable`: **yes**. The refusal's own next step is to dispatch the same intent
 *   again, so the retained key is still the right request.
 * - `terminal`: **no**. The refusal says repeating the request cannot change the outcome;
 *   a replay would only re-refuse while looking like recovery.
 * - `unclassified`: **no**, and that direction is final rather than "unknown". A refusal
 *   *was* observed, so the key may have been spent; the record merely cannot say whether
 *   it was. Sending it again on that basis is the one mistake this journal exists to
 *   prevent, while withholding it costs only the operator's explicit end and a fresh
 *   request. This is the same direction #798's migration chose, now stated once.
 */
export function refusedReplayable(refused: RecordedRefusal | null): boolean {
  return refused === null || refused === "retryable";
}

/**
 * The persisted record. `refused` is the whole knowledge the next page has about the last
 * answer: `null` when no refusal was observed (the outcome may be unknown), a category
 * when one was classified, `unclassified` when one was recorded without a category.
 *
 * The version stays `1` through #798 on purpose: a record written by the previous bundle
 * carries `rejected` instead of `refused`, and it is *restored* (see `#parse`) rather than
 * discarded, so a tab that upgrades mid-flight keeps its key. What this build writes is
 * only the shape below; the legacy shape is accepted on read and never written.
 *
 * The migration is one-way, and the down direction fails closed. A previous bundle reads
 * *every* record this build writes -- `retryable`, `terminal`, `unclassified`, `refused:
 * null`, even the empty record -- as an invalid shape, because its key check demands the
 * four keys it knew; the consumer then takes its storage-error path, reports
 * "恢复信息未能保存，已暂停提交" and locks submissions until the record goes away. Nothing
 * is silently dropped and nothing is misread as replayable, and `sessionStorage` is
 * tab-scoped, so closing the tab is the whole recovery. Bumping `version` would not change
 * that: the older reader rejects the shape before it ever looks at the number, which is
 * why there is no writer that emits both shapes -- the answer to a downgrade is the new
 * bundle, not a record two versions can read.
 */
export type PendingRecord<C> = {
  version: 1;
  command: C | null;
  uncertain: boolean;
  refused: RecordedRefusal | null;
};

const RECORD_KEYS = ["version", "command", "uncertain", "refused"];
/** The shape written before #798: one refusal bit, no category. Restored only. */
const LEGACY_KEYS = ["version", "command", "uncertain", "rejected"];

export function fields(value: unknown, names: string[]): Record<string, unknown> {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).length !== names.length ||
    !names.every((name) => Object.hasOwn(value, name))
  )
    throw new Error("Invalid private recovery record");
  return value as Record<string, unknown>;
}

/**
 * Which shape a restored record has. Exactly one of the two refusal fields must be
 * present: the current shape names the refusal, the legacy one only counts it, and a
 * record with neither (or with both) states nothing this build can read.
 */
function recordShape(input: unknown): "current" | "legacy" {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new Error("Invalid private recovery record");
  const current = Object.hasOwn(input, "refused"),
    legacy = Object.hasOwn(input, "rejected");
  if (current === legacy) throw new Error("Invalid private recovery record");
  return current ? "current" : "legacy";
}

function refusedField(value: unknown): RecordedRefusal | null {
  if (value === "retryable" || value === "terminal" || value === "unclassified") return value;
  if (value === null) return null;
  throw new Error("Invalid private recovery state");
}

export class CommandJournal<C> {
  #raw: string | null;
  #record: PendingRecord<C>;
  constructor(
    private readonly name: string,
    private readonly validateCommand: (input: unknown) => C,
    private readonly storage: JournalStorage = window.sessionStorage,
  ) {
    this.#raw = storage.getItem(name);
    this.#record =
      this.#raw === null
        ? { version: 1, command: null, uncertain: false, refused: null }
        : this.#parse(JSON.parse(this.#bounded(this.#raw)) as unknown, true);
    // A prepared command may have crossed the network before the previous page stopped,
    // and a record that observed no refusal never saw an answer: it is the unknown outcome.
    if (this.#record.command && this.#record.refused === null) this.#record.uncertain = true;
  }
  #bounded(raw: string) {
    if (new TextEncoder().encode(raw).byteLength > 8 * 1024 * 1024)
      throw new Error("Private recovery record too large");
    return raw;
  }
  /**
   * Validate one record, restoring the shape this build no longer writes when
   * `allowLegacy` says the record came from storage.
   *
   * The only determinate reading of a legacy `rejected: true` is `unclassified`: that
   * record says a refusal was observed and cannot say whether it was retryable, so no
   * reader may assume it was. This is a migration assumption, not a design rule -- the bit
   * is gone, and a reader takes the direction that cannot re-send a spent key (#798).
   */
  #parse(input: unknown, allowLegacy: boolean): PendingRecord<C> {
    const shape = recordShape(input);
    // The current shape is the only thing `save` writes, so a legacy object handed to it
    // is a caller that built a record by hand instead of reading one.
    if (shape === "legacy" && !allowLegacy) throw new Error("Invalid private recovery state");
    const value = fields(input, shape === "current" ? RECORD_KEYS : LEGACY_KEYS);
    let refused: RecordedRefusal | null;
    if (shape === "current") refused = refusedField(value.refused);
    else if (typeof value.rejected !== "boolean")
      // A legacy bit that is not a boolean states nothing at all.
      throw new Error("Invalid private recovery state");
    else refused = value.rejected ? "unclassified" : null;
    if (
      value.version !== 1 ||
      typeof value.uncertain !== "boolean" ||
      (value.uncertain && refused !== null) ||
      (value.command === null && (value.uncertain || refused !== null))
    )
      throw new Error("Invalid private recovery state");
    return {
      version: 1,
      command: value.command === null ? null : this.validateCommand(value.command),
      uncertain: value.uncertain,
      refused,
    };
  }
  load() {
    return structuredClone(this.#record);
  }
  save(record: PendingRecord<C>) {
    const next = this.#parse(record, false);
    const raw = this.#bounded(JSON.stringify(next));
    if (this.storage.getItem(this.name) !== this.#raw)
      throw new Error("Private recovery record changed outside this owner");
    this.storage.setItem(this.name, raw);
    this.#raw = raw;
    this.#record = next;
  }
  begin(command: C) {
    this.save({ version: 1, command, uncertain: false, refused: null });
  }
  /**
   * Record what one attempt's answer settled.
   *
   * `refusal` is `null` when no answer was observed (a transport failure or a body-receive
   * timeout: the request may have crossed the network) and the classification otherwise --
   * the category when the caller's table has one, `unclassified` when it does not. An
   * earlier unobserved attempt dominates every later answer: its key may already have
   * created a Job, so the record keeps the unknown outcome instead of being settled by a
   * refusal that only says the server would not start another one (#754).
   */
  failure(refusal: RecordedRefusal | null) {
    const old = this.load();
    const refused = old.uncertain ? null : refusal;
    this.save({
      version: 1,
      command: old.command,
      uncertain: refused === null,
      refused,
    });
  }
  clear() {
    this.save({ version: 1, command: null, uncertain: false, refused: null });
  }
}
