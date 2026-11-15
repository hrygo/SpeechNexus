export function createProfileParser(
  validators: Record<string, (value: unknown) => boolean>,
): (name: string, value: unknown) => unknown;
