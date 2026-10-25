/** Private, tab-scoped intent barrier. Never log the command or infer a remote rollback. */
export type JournalStorage = Pick<Storage, "getItem" | "setItem">;
export type PendingRecord<C> = {
  version: 1;
  command: C | null;
  uncertain: boolean;
  rejected: boolean;
};
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
        ? { version: 1, command: null, uncertain: false, rejected: false }
        : this.#validate(JSON.parse(this.#bounded(this.#raw)) as unknown);
    // A prepared command may have crossed the network before the previous page stopped.
    if (this.#record.command && !this.#record.rejected) this.#record.uncertain = true;
  }
  #bounded(raw: string) {
    if (new TextEncoder().encode(raw).byteLength > 8 * 1024 * 1024)
      throw new Error("Private recovery record too large");
    return raw;
  }
  #validate(input: unknown): PendingRecord<C> {
    const value = fields(input, ["version", "command", "uncertain", "rejected"]);
    if (
      value.version !== 1 ||
      typeof value.uncertain !== "boolean" ||
      typeof value.rejected !== "boolean" ||
      (value.uncertain && value.rejected) ||
      (value.command === null && (value.uncertain || value.rejected))
    )
      throw new Error("Invalid private recovery state");
    return {
      version: 1,
      command: value.command === null ? null : this.validateCommand(value.command),
      uncertain: value.uncertain,
      rejected: value.rejected,
    };
  }
  load() {
    return structuredClone(this.#record);
  }
  save(record: PendingRecord<C>) {
    const next = this.#validate(record);
    const raw = this.#bounded(JSON.stringify(next));
    if (this.storage.getItem(this.name) !== this.#raw)
      throw new Error("Private recovery record changed outside this owner");
    this.storage.setItem(this.name, raw);
    this.#raw = raw;
    this.#record = next;
  }
  begin(command: C) {
    this.save({ version: 1, command, uncertain: false, rejected: false });
  }
  failure(rejected: boolean) {
    const old = this.load();
    this.save({
      ...old,
      uncertain: old.uncertain || !rejected,
      rejected: !old.uncertain && rejected,
    });
  }
  clear() {
    this.save({ version: 1, command: null, uncertain: false, rejected: false });
  }
}
