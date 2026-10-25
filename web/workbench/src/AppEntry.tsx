import { lazy, Suspense } from "react";
import { App } from "./App";

const scenario = import.meta.env.DEV
  ? new URLSearchParams(location.search).get("dev-scenario")
  : null;
const Page =
  scenario === "file-task"
    ? lazy(() => import("./file-workflow/dev/FileTaskDemo"))
    : scenario === "tts-task"
      ? lazy(() => import("./file-workflow/dev/TtsTaskDemo"))
      : App;

export function AppEntry() {
  return (
    <Suspense fallback={<p role="status">正在加载…</p>}>
      <Page />
    </Suspense>
  );
}
