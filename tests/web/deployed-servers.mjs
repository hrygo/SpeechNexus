/**
 * The two deployed-header servers every assertion in this gate drives.
 *
 * Dedicated ports, separate from `tests/web/preview-servers.mjs` (4173/4174), so this
 * gate can run beside the preview-based suites instead of queueing behind them and
 * neither gate can accidentally assert against the other's origin.
 */
export const deployedOrigins = {
  admin: "http://127.0.0.1:4280",
  workbench: "http://127.0.0.1:4281",
};

/**
 * The same two bundles behind an API that reports another contract revision.
 *
 * The pair is what the alert exists for; driving it here keeps the gate proving the
 * alert is wired into each entry, not just that an absent component stays quiet.
 */
export const mixedOrigins = {
  admin: "http://127.0.0.1:4282",
  workbench: "http://127.0.0.1:4283",
};

/** A valid digest that no bundle in this repository was built from. */
export const MIXED_CONTRACT_DIGEST = `sha256:${"b".repeat(64)}`;

export const deployedServers = [
  {
    command: "node tests/web/deployed-server.mjs web/admin/dist 4280",
    url: deployedOrigins.admin,
    reuseExistingServer: false,
  },
  {
    command: "node tests/web/deployed-server.mjs web/workbench/dist 4281",
    url: deployedOrigins.workbench,
    reuseExistingServer: false,
  },
  {
    command: `node tests/web/deployed-server.mjs web/admin/dist 4282 ${MIXED_CONTRACT_DIGEST}`,
    url: mixedOrigins.admin,
    reuseExistingServer: false,
  },
  {
    command: `node tests/web/deployed-server.mjs web/workbench/dist 4283 ${MIXED_CONTRACT_DIGEST}`,
    url: mixedOrigins.workbench,
    reuseExistingServer: false,
  },
];
