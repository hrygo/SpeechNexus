import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FileTaskSession } from "./file-task-session";
import type { TtsSelection } from "./task-journal";

const fields = [
  ["model", "配音模型"],
  ["voiceId", "声音标识"],
  ["voiceRevision", "声音版本"],
  ["profileId", "参数标识"],
  ["profileRevision", "参数版本"],
] as const;

export function TtsTaskInput({
  session,
  initialSelection,
  focusNew,
}: {
  session: FileTaskSession;
  initialSelection: TtsSelection;
  focusNew: boolean;
}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [selection, setSelection] = useState(initialSelection);
  const [text, setText] = useState("");
  const modelInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusNew) modelInput.current?.focus();
  }, [focusNew]);
  const needsText = !state.inputFixed || state.requiresInput;
  return (
    <section className="file-task-input" aria-labelledby="tts-input-title">
      <h2 id="tts-input-title">文本配音</h2>
      <p>填写本部署已发布的模型、声音与参数版本。请求只发送到本部署，输出 WAV。</p>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!needsText || state.busy || !text.length) return;
          if (state.requiresInput) await session.resumeText(text);
          else await session.startText(text, selection);
          const current = session.getSnapshot();
          if (current.inputFixed && !current.requiresInput) setText("");
        }}
      >
        {fields.map(([field, label], index) => (
          <div className="file-task-field" key={field}>
            <label htmlFor={`tts-${field}`}>{label}</label>
            <input
              id={`tts-${field}`}
              ref={index === 0 ? modelInput : undefined}
              value={selection[field]}
              required
              maxLength={128}
              disabled={state.inputFixed || state.busy}
              onChange={(event) => setSelection({ ...selection, [field]: event.target.value })}
            />
          </div>
        ))}
        <label htmlFor="tts-text">配音文本</label>
        <textarea
          id="tts-text"
          value={text}
          rows={8}
          maxLength={200000}
          required
          disabled={!needsText || state.busy}
          onChange={(event) => setText(event.target.value)}
        />
        {state.requiresInput && (
          <p>请提供原文本；指纹校验通过后按原上传键恢复，声音和参数保持原版本。</p>
        )}
        <button
          type="submit"
          disabled={
            !needsText ||
            state.busy ||
            !text.length ||
            fields.some(([field]) => !selection[field].trim())
          }
        >
          {state.requiresInput ? "按原文本恢复上传" : "创建配音任务"}
        </button>
      </form>
      <p>本标签页仅保存恢复标识与固定版本，不保存正文。任务开始后修改文本需另建任务。</p>
      <p>
        模型、声音与参数发现尚待接入，请向部署管理员核对真实引用。手填不表示服务可用或质量已验证。
      </p>
    </section>
  );
}
