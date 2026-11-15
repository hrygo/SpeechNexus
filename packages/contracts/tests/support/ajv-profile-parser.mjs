import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import { ContractValidationError } from "../../src/errors.mjs";

/**
 * Test-only Ajv parser for bundles that have no generated TypeScript consumer.
 *
 * The shipped runtime only accepts precompiled validators (`profile-runtime.mjs`),
 * because runtime compilation needs `'unsafe-eval'` and the deployment CSP forbids
 * it. `coordination.schema.json` is a Python-side bundle: no web page ever parses
 * it, so it has no generated validator module, and these tests compile it here
 * instead of adding an artifact nothing ships.
 *
 * Every other profile and HTTP bundle is validated through its generated
 * validators, so those tests keep exercising the production path.
 */
export function createAjvProfileParser(bundle) {
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
