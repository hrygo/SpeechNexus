/**
 * The two production preview servers every browser gate drives.
 *
 * Both Playwright configurations import this one list, so the build the tests
 * exercise (a real `vite preview` of the built bundles, not a dev server) and its
 * fixed ports stay single-sourced: a change can never make one gate drive a
 * different origin than the other.
 */
export const previewServers = [
  {
    command: "pnpm --filter @speechnexus/admin preview --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
  },
  {
    command:
      "pnpm --filter @speechnexus/workbench preview --host 127.0.0.1 --port 4174 --strictPort",
    url: "http://127.0.0.1:4174",
    reuseExistingServer: false,
  },
];
