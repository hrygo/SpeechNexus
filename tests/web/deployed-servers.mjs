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
];
