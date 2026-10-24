// Compile consumer-facing overloads, client paths and public/internal separation.
import { parse as parseExecution, createClient } from "../src/execution";
import { parse as parseRuntime } from "../src/runtime";
import { parse as parseWorkbench } from "../src/workbench";
import { parse as parseAdmin } from "../src/admin";

declare const wire: unknown;
const execution = parseExecution("SubmitExecutionRequest", wire);
const fence: number = execution.fencing_token;
const identity: string = execution.command.resources.voice?.id ?? "synthetic";
const action: "prepare" | "stop" = parseRuntime("RuntimeInstanceIntentRequest", wire).action;
const projectName: string = parseWorkbench("ProjectCreateRequest", wire).name;
const projectRevision: number = parseWorkbench("Project", wire).metadata_revision;
const transcriptRevision: string = parseWorkbench("TranscriptRevision", wire).revision;
const deletionRevision: string = parseWorkbench(
  "DeletionCreateRequest",
  wire,
).expected_plan_revision;
const deletionState: "queued" | "running" | "blocked" | "succeeded" | "failed" = parseWorkbench(
  "Deletion",
  wire,
).status;
const workspaceName: string = parseAdmin("WorkspaceCreateRequest", wire).display_name;
void [fence, identity, action, projectName, workspaceName];
void [projectRevision, transcriptRevision, deletionRevision, deletionState];

// @ts-expect-error Project metadata revisions are numeric; transcript revisions are strings.
const wrongProjectRevision: string = parseWorkbench("Project", wire).metadata_revision;
// @ts-expect-error A deletion confirmation never substitutes a numeric revision.
const wrongDeletionRevision: number = parseWorkbench(
  "DeletionCreateRequest",
  wire,
).expected_plan_revision;
void [wrongProjectRevision, wrongDeletionRevision];

import { createValidatedClient, validateRequest } from "../src/workbench-http";
const validatedWorkbench = createValidatedClient("http://deployment.invalid", 8 * 1024 * 1024);
void validatedWorkbench.GET("/v1/workbench/projects");
validateRequest("getProject", { path: { project_id: "project-demo" } });
// @ts-expect-error Meeting HTTP entrypoints are delivered separately.
validateRequest("createMeeting", {});
// @ts-expect-error The validated client exposes only the selected HTTP surface.
void validatedWorkbench.POST("/v1/workbench/projects/{project_id}/meetings", {});
// @ts-expect-error No implicit JSON byte budget for network parsing.
createValidatedClient("http://deployment.invalid");

// @ts-expect-error Internal commands are absent from the workbench parser.
parseWorkbench("SubmitExecutionRequest", wire);
// @ts-expect-error No implicit deployment endpoint.
createClient();
const client = createClient("http://deployment.invalid");
// @ts-expect-error Unknown routes must not silently become typed requests.
client.POST("/not-a-route", {});

// Multipart binary transport retains the generated metadata shape.
import { createSpeechClient, uploadAsset } from "../src/index";
const speech = createSpeechClient("http://deployment.invalid");
void uploadAsset(speech, { purpose: "text_input", file: new Blob(["synthetic"]) });
// @ts-expect-error the helper never accepts a server path or URL instead of bytes
void uploadAsset(speech, { purpose: "text_input", file: "http://unapproved.test/content" });
