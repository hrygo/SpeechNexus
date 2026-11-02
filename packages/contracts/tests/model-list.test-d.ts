import { parse, type FileContracts, type components } from "@speechnexus/contracts";

declare const wire: unknown;
const catalogue = parse("ModelList", wire);
const typed: FileContracts["ModelList"] = catalogue;
const canonical: components["schemas"]["ModelList"] = typed;
const object: "list" = catalogue.object;
for (const model of catalogue.data) {
  const id: string = model.id;
  const created: number = model.created;
  if (model.nexus) {
    const revision: string = model.nexus.binding.revision;
    const ready: boolean = model.nexus.readiness.engine_ready;
    const task: components["schemas"]["SpeechTaskKind"] | undefined = model.nexus.tasks[0];
    void [revision, ready, task];
    // @ts-expect-error Resource revisions preserve their decimal string representation.
    const numericRevision: number = model.nexus.binding.revision;
    // @ts-expect-error Readiness must be boolean, never a truthy string.
    const stringReady: string = model.nexus.readiness.engine_ready;
    void [numericRevision, stringReady];
  }
  void [id, created];
}
// @ts-expect-error The root runtime selection does not expose individual Model entrypoints.
parse("Model", wire);
void [canonical, object];
