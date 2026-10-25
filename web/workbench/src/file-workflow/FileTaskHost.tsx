import { useEffect, useRef, useState } from "react";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient } from "./speech-file-client";
import type { TtsSelection } from "./task-journal";
import { FileTaskInput } from "./FileTaskInput";
import { TtsTaskInput } from "./TtsTaskInput";
import { TtsTaskResult, type WorkbenchSaveClient } from "./TtsTaskResult";
import { SessionTaskJournal } from "./task-journal";
import { FileTaskPanel } from "./FileTaskPanel";
import { canFinishTask } from "./file-task-policy";
import "./file-task.css";

type TaskKind = "asr" | "tts";
type TaskStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function initialize(client: SpeechFileClient, kind: TaskKind, supplied?: TaskStorage) {
  const key = `speechnexus.workbench.${kind}-task.v1`;
  try {
    const storage = supplied ?? window.sessionStorage;
    let expected = storage.getItem(key);
    const journal = new SessionTaskJournal(
      {
        getItem: (name) => storage.getItem(name),
        setItem: (name, value) => {
          storage.setItem(name, value);
          expected = value;
        },
      },
      key,
    );
    const record = journal.load();
    // A corrupt or other-kind recovery record must never mean "new".
    if (record && !(kind === "asr" ? [1, 3, 5] : [2, 4, 6]).includes(record.version))
      throw new Error("Invalid task intent");
    const session = new FileTaskSession(client, journal);
    return {
      session,
      model: record?.model ?? "",
      savedAssetId: record?.assetId ?? null,
      kind,
      selection:
        record && "voiceId" in record
          ? {
              model: record.model,
              voiceId: record.voiceId,
              voiceRevision: record.voiceRevision,
              profileId: record.profileId,
              profileRevision: record.profileRevision,
            }
          : ({
              model: "",
              voiceId: "",
              voiceRevision: "",
              profileId: "",
              profileRevision: "",
            } satisfies TtsSelection),
      clearCompleted: () => {
        if (!canFinishTask(session.getSnapshot()))
          throw new Error("Task is not confirmed complete");
        session.stopEvents();
        if (storage.getItem(key) !== expected) throw new Error("Task owner changed");
        storage.removeItem(key);
      },
    };
  } catch {
    return null;
  }
}

function ActiveTask({
  host,
  onNew,
  focusNew,
  workbench,
}: {
  host: NonNullable<ReturnType<typeof initialize>>;
  onNew: () => void;
  focusNew: boolean;
  workbench?: WorkbenchSaveClient | undefined;
}) {
  const { session } = host;
  const mount = useRef({ generation: 0 });
  useEffect(() => {
    const owner = mount.current;
    const generation = ++owner.generation;
    const stop = () => session.stopEvents();
    const visibility = () => {
      if (document.hidden) stop();
    };
    window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("pagehide", stop);
      document.removeEventListener("visibilitychange", visibility);
      queueMicrotask(() => {
        if (owner.generation === generation) session.dispose();
      });
    };
  }, [session]);
  return (
    <div className="file-task-grid">
      {host.kind === "asr" ? (
        <FileTaskInput session={session} initialModel={host.model} focusNew={focusNew} />
      ) : (
        <TtsTaskInput session={session} initialSelection={host.selection} focusNew={focusNew} />
      )}
      <FileTaskPanel
        session={session}
        onNew={onNew}
        savedAssetId={host.savedAssetId}
        result={
          host.kind === "tts" ? (
            <TtsTaskResult session={session} workbench={workbench} />
          ) : undefined
        }
      />
    </div>
  );
}

/** Production composition root: fixed same-origin public client and separate recovery slots. */
export function FileTaskHost({
  kind = "asr",
  client,
  storage,
  workbench,
}: {
  kind?: TaskKind;
  client?: SpeechFileClient;
  storage?: TaskStorage;
  workbench?: WorkbenchSaveClient | undefined;
}) {
  const [api] = useState(() => client ?? new SpeechFileClient(location.origin));
  const [host, setHost] = useState(() => initialize(api, kind, storage));
  const [generation, setGeneration] = useState(0);
  const [notice, setNotice] = useState("");
  if (!host)
    return (
      <section className="file-task" aria-label="文件任务恢复">
        <p role="alert">
          恢复信息不可用。请修复浏览器存储后重新载入；本页已暂停提交，原记录未清除。
        </p>
      </section>
    );
  return (
    <section
      className="file-task"
      aria-label={kind === "asr" ? "当前部署文件转写" : "当前部署文本配音"}
    >
      {notice && <p role="alert">{notice}</p>}
      <ActiveTask
        key={generation}
        focusNew={generation > 0}
        host={host}
        workbench={workbench}
        onNew={() => {
          try {
            host.clearCompleted();
            host.session.dispose();
            setHost(initialize(api, kind, storage));
            setGeneration((value) => value + 1);
            setNotice("");
          } catch {
            setNotice("本地任务信息已变化或无法清理；保留原任务，请重新载入核对。");
          }
        }}
      />
    </section>
  );
}
