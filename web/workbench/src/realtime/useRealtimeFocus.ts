import { useLayoutEffect, useRef } from "react";

function unavailable(element: HTMLElement) {
  return (
    !element.isConnected ||
    element.matches(":disabled, [aria-disabled='true']") ||
    !!element.closest("[hidden], [inert], [aria-hidden='true']")
  );
}

/** Repair only focus lost by this host's state transition, never async completion alone. */
export function useRealtimeFocus(active: boolean) {
  const root = useRef<HTMLElement>(null),
    heading = useRef<HTMLHeadingElement>(null),
    focus = useRef<{ owned: HTMLElement | null }>({ owned: null });
  useLayoutEffect(() => {
    const owner = focus.current;
    const moved = (event: FocusEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement && root.current?.contains(target)) owner.owned = target;
      // Browsers may focus BODY when a focused control is disabled or removed.
      else if (!(target === document.body && owner.owned && unavailable(owner.owned)))
        owner.owned = null;
    };
    const pointed = (event: PointerEvent) => {
      if (!(event.target instanceof Node && root.current?.contains(event.target)))
        owner.owned = null;
    };
    const left = () => {
      owner.owned = null;
    };
    if (active) {
      document.addEventListener("focusin", moved);
      document.addEventListener("pointerdown", pointed);
      window.addEventListener("blur", left);
    }
    return () => {
      owner.owned = null;
      document.removeEventListener("focusin", moved);
      document.removeEventListener("pointerdown", pointed);
      window.removeEventListener("blur", left);
    };
  }, [active]);
  useLayoutEffect(() => {
    const owned = focus.current.owned,
      current = document.activeElement;
    if (
      active &&
      !document.hidden &&
      owned &&
      unavailable(owned) &&
      (current === owned || current === document.body) &&
      heading.current &&
      !unavailable(heading.current)
    )
      heading.current.focus();
  });
  return { root, heading };
}
