import { lazy, Suspense, type ComponentType } from "react";
import { App } from "./App";

/**
 * Dev-only scenario pages. They are resolved through Vite's glob transform instead
 * of a static `import()`, so the production TypeScript program (`tsconfig.build.json`,
 * the program the Web image build compiles) never follows them: their contract
 * fixtures live under `tests/fixtures/contracts`, which is deliberately outside the
 * Web image build context (`deploy/images/web.Dockerfile`). `import.meta.env.DEV`
 * empties the map in a production build, so neither the pages nor their fixtures
 * reach the bundle. Keep this a glob: a static `import()` here puts a test-fixture
 * consumer back into the image build graph, and `pnpm verify:web-image` fails.
 */
type DevPage = () => Promise<{ default: ComponentType }>;
const devPages: Record<string, DevPage> = import.meta.env.DEV
  ? import.meta.glob<{ default: ComponentType }>("./file-workflow/dev/*Demo.tsx")
  : {};

const scenario = new URLSearchParams(location.search).get("dev-scenario");
const fileTaskPage = import.meta.env.DEV
  ? devPages["./file-workflow/dev/FileTaskDemo.tsx"]
  : undefined;
const ttsTaskPage = import.meta.env.DEV
  ? devPages["./file-workflow/dev/TtsTaskDemo.tsx"]
  : undefined;

const Page =
  scenario === "file-task" && fileTaskPage !== undefined
    ? lazy(fileTaskPage)
    : scenario === "tts-task" && ttsTaskPage !== undefined
      ? lazy(ttsTaskPage)
      : App;

export function AppEntry() {
  return (
    <Suspense fallback={<p role="status">正在加载…</p>}>
      <Page />
    </Suspense>
  );
}
