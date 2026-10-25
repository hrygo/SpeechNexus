import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FileTaskSession } from "./file-task-session";

export function FileTaskInput({
  session,
  initialModel,
  focusNew,
}: {
  session: FileTaskSession;
  initialModel: string;
  focusNew: boolean;
}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [model, setModel] = useState(initialModel);
  const [file, setFile] = useState<File | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const modelInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusNew) modelInput.current?.focus();
  }, [focusNew]);
  const needsFile = !state.inputFixed || state.requiresInput;
  return (
    <section className="file-task-input" aria-labelledby="file-input-title">
      <h2 id="file-input-title">文件转写</h2>
      <p>选择音频并填写当前部署已发布的转写模型。请求只发送到本部署。</p>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!file || !needsFile || state.busy || !model.trim() || model.length > 128) return;
          if (state.requiresInput) await session.resumeFile(file);
          else await session.start(file, model);
          setFile(null);
          if (input.current) input.current.value = "";
        }}
      >
        <label htmlFor="file-task-model">转写模型</label>
        <input
          ref={modelInput}
          id="file-task-model"
          value={model}
          maxLength={128}
          required
          disabled={state.inputFixed || state.busy}
          onChange={(event) => setModel(event.target.value)}
        />
        <label htmlFor="file-task-input">音频文件</label>
        <input
          ref={input}
          id="file-task-input"
          type="file"
          accept="audio/*"
          required
          disabled={!needsFile || state.busy}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        {state.requiresInput && <p>请重新选择原音频，校验通过后按原上传键恢复；模型保持原选择。</p>}
        <button
          type="submit"
          disabled={!needsFile || state.busy || !file || !model.trim() || model.length > 128}
        >
          {state.requiresInput ? "按原输入恢复上传" : "上传并转写"}
        </button>
      </form>
      <p>本标签页仅保存任务恢复标识，不保存音频或文件名。关闭标签页可能丢失恢复信息。</p>
      <p>当前提供原始 JSON 制品下载；校对、版本保存和字幕导出将随工作台业务接口接入。</p>
    </section>
  );
}
