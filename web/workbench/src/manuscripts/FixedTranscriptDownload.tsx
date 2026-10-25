import { useState } from "react";
import type { Contracts } from "./workbench-client";

/** Export only the exact, validated server snapshot, never the editable draft. */
export function FixedTranscriptDownload({
  view,
  disabled,
}: {
  view: Contracts["TranscriptView"];
  disabled: boolean;
}) {
  const identity = `${view.transcript.id}@${view.revision.revision}`;
  const [notice, setNotice] = useState<{ identity: string; text: string } | null>(null);
  function download() {
    if (disabled) return;
    if (
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
        new TextEncoder().encode(view.revision.text),
      ) !== view.revision.text
    ) {
      setNotice({ identity, text: "原文无法无损编码为 UTF-8，下载未开始。" });
      return;
    }
    const url = URL.createObjectURL(
      new Blob([view.revision.text], { type: "text/plain;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    try {
      anchor.href = url;
      anchor.download = `transcript-r${view.revision.revision}.txt`;
      document.body.append(anchor);
      anchor.click();
      setNotice({ identity, text: `已交给浏览器下载固定版本 ${view.revision.revision}。` });
    } finally {
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
  }
  return (
    <div>
      <p>下载服务端已读取的版本 {view.revision.revision}，不包含未保存草稿。</p>
      <button type="button" disabled={disabled} onClick={download}>
        下载固定版本 TXT
      </button>
      {notice?.identity === identity && <p role="status">{notice.text}</p>}
    </div>
  );
}
