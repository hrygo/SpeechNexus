import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/file.schema.json" with { type: "json" };

export { ContractValidationError } from "./errors.mjs";

// Type generators cannot encode if/then, oneOf exclusivity, or numeric bounds.
// Keep the schema authoritative and disable all data-changing Ajv features.
const ajv = new Ajv2020({
  strict: false,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
  validateFormats: true,
});
addFormats(ajv);
const validators = new Map(
  bundle["x-nexus-entrypoints"].map((name) => [
    name,
    ajv.compile({ $ref: `#/$defs/${name}`, $defs: bundle.$defs }),
  ]),
);

export function parse(name, value) {
  const validate = validators.get(name);
  if (!validate) throw new Error(`Unknown file contract: ${name}`);
  if (!validate(value)) throw new ContractValidationError(`Invalid ${name}`);
  return value;
}
