import { parse as parseAdmin, type components } from "../src/admin";
import { parse } from "../src/scenario_profiles";

declare const request: components["schemas"]["ProfileRevisionCreateRequest"];
const prepared = parseAdmin("ProfileRevisionCreateRequest", request);
const latest: string | null = prepared.expected_latest_revision;
const revision = parse("ScenarioProfileRevision", {});
const identity: string = revision.profile_id;
const fixed: string = revision.revision;
void [latest, identity, fixed];
