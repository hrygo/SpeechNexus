export class ContractValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "ContractValidationError";
  }
}
