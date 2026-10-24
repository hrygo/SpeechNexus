import { parse, type Contracts } from "../src/binding_defaults";
import type { Contracts as Profiles } from "../src/scenario_profiles";

type Snapshot = Contracts["BindingParameterDefaultsSnapshot"];
type SparseAsr = NonNullable<Snapshot["parameters"]["asr"]>;
type CompleteAsr = NonNullable<Profiles["ScenarioProfileRevision"]["parameters"]["asr"]>;
const sparse: SparseAsr = { max_segment_ms: 15000 };
const explicitNull: Snapshot["parameters"] = { tts: { instructions: null } };
// @ts-expect-error a complete profile still requires its full ASR configuration
const incompleteProfile: CompleteAsr = {};
// @ts-expect-error canonical default references include resource ID and revision
const shorthand: SparseAsr = { vocabulary_set_revision: "2" };
declare const profileAsr: CompleteAsr;
const requiredLanguage: string = profileAsr.language;
const requiredDeadline: number = profileAsr.auxiliary_deadline_ms;
const snapshot = parse("BindingParameterDefaultsSnapshot", {});
const fixedRevision: string = snapshot.binding_revision;
// @ts-expect-error format-only entrypoint provides no publication operation
parse("publishBindingDefaults", {});
void [
  sparse,
  explicitNull,
  incompleteProfile,
  shorthand,
  requiredLanguage,
  requiredDeadline,
  fixedRevision,
];
