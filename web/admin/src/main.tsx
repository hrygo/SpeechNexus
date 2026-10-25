import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppEntry } from "./AppEntry";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <AppEntry />
  </StrictMode>,
);
