import { validators } from "./speechnexus_contracts/file.validators.generated.mjs";
import { ContractValidationError } from "./errors.mjs";

export { ContractValidationError } from "./errors.mjs";

/**
 * The file-workflow contracts (`@speechnexus/contracts` itself).
 *
 * Validators are precompiled at generation time; see `profile-runtime.mjs` for why
 * the runtime must not call `ajv.compile()` (it needs `'unsafe-eval'`, which the
 * deployment CSP forbids).
 *
 * Type generators cannot encode if/then, oneOf exclusivity, or numeric bounds.
 * The generated validators keep the schema authoritative and disable all
 * data-changing Ajv features (`coerceTypes`, `useDefaults`, `removeAdditional`).
 */
export function parse(name, value) {
  if (!Object.hasOwn(validators, name)) {
    throw new Error(`Unknown file contract: ${name}`);
  }
  if (!validators[name](value)) {
    throw new ContractValidationError(`Invalid ${name}`);
  }
  return value;
}
