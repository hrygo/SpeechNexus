/**
 * The two production preview servers every browser gate drives.
 *
 * Both Playwright configurations import this one list, so the build the tests
 * exercise (a real `vite preview` of the built bundles, not a dev server) and its
 * fixed ports stay single-sourced: a change can never make one gate drive a
 * different origin than the other.
 *
 * `--strictPort` plus `reuseExistingServer: false` means the two gates cannot run
 * concurrently: the accessibility and behavior configurations both own 4173/4174, so
 * starting them in parallel fails fast with "port already used". CI therefore runs
 * them as sequential steps of one job; do not move either into a workflow matrix or
 * a parallel job without giving it dedicated ports first.
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
