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
const workspaceName: string = parseAdmin("WorkspaceCreateRequest", wire).display_name;
void [fence, identity, action, projectName, workspaceName];

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
