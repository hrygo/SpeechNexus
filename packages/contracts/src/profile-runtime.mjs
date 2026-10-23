import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { ContractValidationError } from "./errors.mjs";

// Constructing/importing a profile never compiles unrelated public/internal schemas.
export function createProfileParser(bundle) {
  const entrypoints = new Set(bundle["x-nexus-entrypoints"]);
  const validators = new Map();
  const ajv = new Ajv2020({
    strict: false,
    coerceTypes: false,
    useDefaults: false,
    removeAdditional: false,
    validateFormats: true,
  });
  addFormats(ajv);
  return (name, value) => {
    if (!entrypoints.has(name)) throw new Error("Unknown profile contract");
    let validate = validators.get(name);
    if (!validate) {
      validate = ajv.compile({ $ref: `#/$defs/${name}`, $defs: bundle.$defs });
      validators.set(name, validate);
    }
    if (!validate(value)) {
      throw new ContractValidationError(`Invalid ${name}`);
    }
    return value;
  };
}
