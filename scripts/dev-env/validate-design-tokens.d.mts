export interface GuardrailViolation {
  rule: string;
  message: string;
}

export interface ValidateLineOptions {
  isExemptColorFile?: boolean;
  isTestFile?: boolean;
  isMarkupOrStyleOnly?: boolean;
}

export interface ValidationError {
  file: string;
  line: number;
  message: string;
  snippet?: string;
}

export const FORBIDDEN_COPY: string[];
export const VALID_CAPABILITY_STATES: Set<string>;

export function extractDefinedCssVariables(cssContent: string): Set<string>;
export function extractReferencedCssVariables(content: string): Set<string>;
export function validateSourceLine(
  line: string,
  options?: ValidateLineOptions,
): GuardrailViolation[];
export function runValidation(rootDir?: string): ValidationError[];
