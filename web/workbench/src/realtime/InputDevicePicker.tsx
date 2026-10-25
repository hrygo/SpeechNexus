import { useEffect, useId, useRef, useState } from "react";

/** Device metadata stays in this mounted host; enumeration never requests a stream. */
export default function InputDevicePicker({
  value,
  onSelect,
  disabled,
}: {
  value: string | null;
  onSelect: (id: string | null) => void;
  disabled: boolean;
}) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [draft, setDraft] = useState<{ id: string | null } | null>(null);
  const choice = draft ? draft.id : value;
  const [status, setStatus] = useState<"idle" | "reading" | "loaded" | "changed" | "failed">(
    "idle",
  );
  const request = useRef({ generation: 0 }),
    help = useId(),
    input = useId();
  const media = navigator.mediaDevices;
  const supported = typeof media?.enumerateDevices === "function";
  useEffect(() => {
    if (!supported) return;
    const owner = request.current;
    const changed = () => {
      ++owner.generation;
      setStatus("changed");
    };
    media.addEventListener("devicechange", changed);
    return () => {
      ++owner.generation;
      media.removeEventListener("devicechange", changed);
    };
  }, [media, supported]);
  const read = async () => {
    if (disabled || !supported || status === "reading") return;
    const version = ++request.current.generation;
    setStatus("reading");
    try {
      const list = await media.enumerateDevices();
      if (version !== request.current.generation) return;
      setDevices([
        ...new Map(
          list.filter((d) => d.kind === "audioinput" && d.deviceId).map((d) => [d.deviceId, d]),
        ).values(),
      ]);
      setStatus("loaded");
    } catch {
      if (version === request.current.generation) setStatus("failed");
    }
  };
  const missing = choice !== null && !devices.some((d) => d.deviceId === choice);
  return (
    <section aria-label="本地麦克风选择">
      <h3>本地麦克风选择</h3>
      <p id={help}>
        读取列表不申请采集权限。选择后须点击应用，才会结束当前会话并保留缺口和已确认正文；释放确认后须创建新会话，再明确开始采集。设备信息只留在本标签页内存。
      </p>
      <label htmlFor={input}>输入麦克风</label>
      <select
        id={input}
        value={choice ?? ""}
        aria-describedby={help}
        disabled={disabled}
        onChange={(e) => setDraft({ id: e.target.value || null })}
      >
        <option value="">系统默认麦克风</option>
        {missing && <option value={choice ?? ""}>已选择的麦克风（当前列表未列出）</option>}
        {devices.map((d, i) => (
          <option key={d.deviceId} value={d.deviceId}>
            {d.label || `麦克风 ${i + 1}（浏览器未提供名称）`}
          </option>
        ))}
      </select>
      <div className="realtime-actions">
        <button
          type="button"
          disabled={disabled || choice === value}
          onClick={() => {
            onSelect(choice);
            setDraft(null);
          }}
        >
          应用麦克风选择
        </button>
        <button
          type="button"
          disabled={disabled || !supported}
          aria-disabled={status === "reading"}
          onClick={() => {
            void read();
          }}
        >
          读取麦克风列表
        </button>
      </div>
      {(!supported || status !== "idle" || choice !== value) && (
        <p role="status">
          {!supported
            ? "当前浏览器或安全上下文无法读取麦克风列表，请检查部署与浏览器权限。"
            : choice !== value
              ? "新的麦克风选择尚未应用，当前采集不变；应用会结束当前会话。"
              : status === "reading"
                ? "正在读取麦克风列表…"
                : status === "failed"
                  ? "无法读取麦克风列表，请检查浏览器权限后重试；本页未改动设备选择。"
                  : status === "changed"
                    ? "设备列表可能已变化，请重新读取；本页不会自动选择其他设备。"
                    : missing
                      ? "所选设备不在当前列表；未自动回退。请检查设备或明确选择其他麦克风。"
                      : devices.length
                        ? "麦克风列表已读取；未命名设备可在授权采集后重新读取名称。"
                        : "浏览器尚未列出麦克风，请检查设备和权限后重新读取。"}
        </p>
      )}
    </section>
  );
}
