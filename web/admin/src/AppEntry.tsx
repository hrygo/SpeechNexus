import { lazy, Suspense } from "react";
import { App } from "./App";

const Page =
  import.meta.env.DEV && new URLSearchParams(location.search).get("dev-scenario") === "task-center"
    ? lazy(() => import("./task-center/dev/TaskCenterDemo"))
    : App;

export function AppEntry() {
  return (
    <Suspense fallback={<p role="status">正在加载…</p>}>
      <Page />
    </Suspense>
  );
}
