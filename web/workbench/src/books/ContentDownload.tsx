import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** The browser exposes a save only after its own full byte/digest verification. */
type Props = {
  identity: string;
  filename: string;
  label: string;
  load: (signal: AbortSignal) => Promise<Blob>;
};

/** True when the browser detached, disabled or hid the element that held focus. */
function unavailable(element: HTMLElement | null) {
  return (
    !element ||
    !element.isConnected ||
    element.matches(":disabled, [aria-disabled='true']") ||
    !!element.closest("[hidden], [inert], [aria-hidden='true']")
  );
}

export function ContentDownload(props: Props) {
  return (
    <DownloadAction
      key={props.identity}
      filename={props.filename}
      label={props.label}
      load={props.load}
    />
  );
}

function DownloadAction({ filename, label, load }: Omit<Props, "identity">) {
  const current = useRef<AbortController | null>(null);
  const action = useRef<HTMLButtonElement | null>(null);
  const cancel = useRef<HTMLButtonElement | null>(null);
  const root = useRef<HTMLDivElement | null>(null);
  const restore = useRef<{ cancelled: boolean; held: HTMLElement | null }>({
    cancelled: false,
    held: null,
  });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(false);
  const [saved, setSaved] = useState(false);
  // Only the focus this transition takes away is repaired: a control the user reached meanwhile, a
  // click outside, a blurred window and a hidden page keep their own focus when cancellation ends.
  useLayoutEffect(() => {
    if (!busy) return;
    const wanted = restore.current;
    const inside = (target: EventTarget | null) =>
      target instanceof HTMLElement && !!root.current?.contains(target);
    const moved = (event: FocusEvent) => {
      // Browsers move focus to BODY when they remove or disable the control that held it.
      if (inside(event.target)) wanted.held = event.target as HTMLElement;
      else if (!(event.target === document.body && wanted.held && unavailable(wanted.held)))
        wanted.held = null;
    };
    const pointed = (event: PointerEvent) => {
      if (!(event.target instanceof Node && root.current?.contains(event.target)))
        wanted.held = null;
    };
    const left = () => {
      wanted.held = null;
    };
    document.addEventListener("focusin", moved);
    document.addEventListener("pointerdown", pointed);
    window.addEventListener("blur", left);
    document.addEventListener("visibilitychange", left);
    return () => {
      document.removeEventListener("focusin", moved);
      document.removeEventListener("pointerdown", pointed);
      window.removeEventListener("blur", left);
      document.removeEventListener("visibilitychange", left);
    };
  }, [busy]);
  useLayoutEffect(() => {
    const wanted = restore.current;
    if (busy || !wanted.cancelled) return;
    wanted.cancelled = false;
    const held = wanted.held,
      active = document.activeElement;
    wanted.held = null;
    // Cancelling unmounted the control that held focus; repair exactly that loss, never a new focus.
    if (document.hidden || !held || !unavailable(held)) return;
    if (active !== held && active !== document.body) return;
    if (!unavailable(action.current)) action.current?.focus();
  }, [busy]);
  useEffect(() => {
    return () => {
      current.current?.abort();
      current.current = null;
    };
  }, []);
  async function download() {
    if (current.current) return;
    const owner = new AbortController();
    current.current = owner;
    setBusy(true);
    setProblem(false);
    setSaved(false);
    try {
      const blob = await load(owner.signal);
      owner.signal.throwIfAborted();
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        try {
          link.click();
        } finally {
          link.remove();
        }
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }
      setSaved(true);
    } catch {
      if (!owner.signal.aborted) setProblem(true);
    } finally {
      if (current.current === owner) {
        current.current = null;
        setBusy(false);
      }
    }
  }
  return (
    <div ref={root}>
      <button ref={action} type="button" disabled={busy} onClick={() => void download()}>
        {busy ? "正在校验下载…" : label}
      </button>
      {busy && (
        <button
          ref={cancel}
          type="button"
          aria-label={`${label}，取消下载`}
          onClick={() => {
            const wanted = restore.current;
            wanted.cancelled = true;
            wanted.held =
              document.activeElement instanceof HTMLElement &&
              root.current?.contains(document.activeElement)
                ? document.activeElement
                : cancel.current;
            current.current?.abort();
          }}
        >
          取消下载
        </button>
      )}
      {problem && (
        <p role="alert">
          下载未完成：内容不可用、校验失败或超过浏览器限制。请核对服务状态后重试；校验需要安全浏览器环境。
        </p>
      )}
      {saved && <p role="status">完整性校验通过，已交给浏览器保存。</p>}
    </div>
  );
}
