import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** The browser exposes a save only after its own full byte/digest verification. */
type Props = {
  identity: string;
  filename: string;
  label: string;
  load: (signal: AbortSignal) => Promise<Blob>;
};

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
  const restoreFocus = useRef(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(false);
  const [saved, setSaved] = useState(false);
  useLayoutEffect(() => {
    if (!busy && restoreFocus.current) {
      restoreFocus.current = false;
      action.current?.focus();
    }
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
    <div>
      <button ref={action} type="button" disabled={busy} onClick={() => void download()}>
        {busy ? "正在校验下载…" : label}
      </button>
      {busy && (
        <button
          type="button"
          aria-label={`${label}，取消下载`}
          onClick={() => {
            restoreFocus.current = true;
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
