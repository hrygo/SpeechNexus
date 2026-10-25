import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { parse } from "../manuscripts/workbench-client";
export function sample(operation: string) {
  return structuredClone(
    fixtures.examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === "response" &&
        e.valid &&
        typeof e.status === "number" &&
        [200, 201, 202].includes(e.status),
    )!.value.body,
  );
}
export const meeting = () => parse("Meeting", sample("createMeeting"));
export const job = () => parse("MinutesJob", sample("createMeetingMinutes"));
export function source(revision = "1", text = "原文") {
  const v = parse("TranscriptView", sample("getTranscript"));
  v.transcript.head_revision = revision;
  Object.assign(v.revision, {
    revision,
    text,
    status: "sealed",
    source_revision: revision === "1" ? null : "1",
  });
  return v;
}
