import { lazy, Suspense, useState } from "react";
import { AppShell } from "@speechnexus/shared";
import { FileTaskHost } from "./file-workflow/FileTaskHost";
import { TeleprompterPanel } from "./teleprompter/TeleprompterPanel";
import { WorkbenchClient, type Contracts } from "./manuscripts/workbench-client";

const ManuscriptHost = lazy(() => import("./manuscripts/ManuscriptHost"));
const MeetingHost = lazy(() => import("./meetings/MeetingHost"));
const DeletionHost = lazy(() => import("./deletions/DeletionHost"));
const RealtimeHost = lazy(() => import("./realtime/RealtimeHost"));
const RealtimeTtsHost = lazy(() => import("./realtime/RealtimeTtsHost"));
const VoiceCreateHost = lazy(() => import("./voices/VoiceCreateHost"));
const BooksHost = lazy(() => import("./books/BooksHost"));
const AssistantHost = lazy(() => import("./assistant/AssistantHost"));

export function App() {
  const [speechSource, setSpeechSource] = useState<
    Extract<Contracts["TranscriptCreateRequest"]["source"], { kind: "speech_artifact" }> | undefined
  >();
  const [kind, setKind] = useState<
    | "asr"
    | "tts"
    | "prompter"
    | "manuscripts"
    | "meetings"
    | "deletions"
    | "realtime"
    | "realtime-tts"
    | "voices"
    | "books"
    | "assistant"
  >("asr");
  const [meetingOpened, setMeetingOpened] = useState(false);
  const [deletionOpened, setDeletionOpened] = useState(false);
  const [realtimeOpened, setRealtimeOpened] = useState(false);
  const [realtimeTtsOpened, setRealtimeTtsOpened] = useState(false);
  const [voiceOpened, setVoiceOpened] = useState(false);
  const [booksOpened, setBooksOpened] = useState(false);
  const [assistantOpened, setAssistantOpened] = useState(false);
  // The dubbing result can copy its committed audio into a project as the workbench's own
  // saved copy; the client is the same fixed same-origin one the manuscript hosts use.
  const [workbench] = useState(() => new WorkbenchClient());
  return (
    <AppShell
      surface="个人语音工作台"
      tone="workbench"
      eyebrow="SpeechNexus / Workbench"
      title="语音任务集中完成"
      description="上传音频、输入文本配音、实时字幕、创作音色任务、校对封存会议稿、手动提词或预检删除内容。真实语音服务与有声书继续联调。"
      statusTitle={
        kind === "assistant"
          ? "语音助手消费者已接入"
          : kind === "books"
            ? "有声书导出消费者已接入"
            : kind === "voices"
              ? "音色创建任务消费者已接入"
              : kind === "realtime-tts"
                ? "实时配音消费者已接入"
                : kind === "realtime"
                  ? "实时字幕消费者已接入"
                  : kind === "asr"
                    ? "文件转写入口已接入"
                    : kind === "tts"
                      ? "文本配音入口已接入"
                      : kind === "manuscripts"
                        ? "项目与稿件入口已接入"
                        : kind === "deletions"
                          ? "删除预检入口已接入"
                          : kind === "meetings"
                            ? "会议记录入口已接入"
                            : "手动提词阅读可用"
      }
      statusDetail={
        kind === "prompter"
          ? "本地阅读；自动语音跟随待接入"
          : kind === "assistant"
            ? "按部署能力三态呈现；缺少语音底座或内网模型时保持关闭"
            : "公开 API 消费已接入，真实部署待联调"
      }
      statusState={kind === "prompter" ? "ready" : "contract"}
      boundaryNote="“个人”描述任务体验，不代表服务端按用户私有保存。当前部署中的项目、音色与结果由所有工作台使用者共享，浏览器本地标识不能充当身份。"
    >
      <nav className="file-task-nav" aria-label="工作台功能">
        <button
          type="button"
          aria-pressed={kind === "assistant"}
          onClick={() => {
            setAssistantOpened(true);
            setKind("assistant");
          }}
        >
          语音助手
        </button>
        <button
          type="button"
          aria-pressed={kind === "books"}
          onClick={() => {
            setBooksOpened(true);
            setKind("books");
          }}
        >
          有声书导出
        </button>
        <button
          type="button"
          aria-pressed={kind === "voices"}
          onClick={() => {
            setVoiceOpened(true);
            setKind("voices");
          }}
        >
          音色创作
        </button>
        <button
          type="button"
          aria-pressed={kind === "realtime-tts"}
          onClick={() => {
            setRealtimeTtsOpened(true);
            setKind("realtime-tts");
          }}
        >
          实时配音
        </button>
        <button
          type="button"
          aria-pressed={kind === "realtime"}
          onClick={() => {
            setRealtimeOpened(true);
            setKind("realtime");
          }}
        >
          实时字幕
        </button>
        <button
          type="button"
          aria-pressed={kind === "deletions"}
          onClick={() => {
            setDeletionOpened(true);
            setKind("deletions");
          }}
        >
          删除与恢复
        </button>
        <button
          type="button"
          aria-pressed={kind === "meetings"}
          onClick={() => {
            setMeetingOpened(true);
            setKind("meetings");
          }}
        >
          会议记录
        </button>
        <button
          type="button"
          aria-pressed={kind === "manuscripts"}
          onClick={() => setKind("manuscripts")}
        >
          项目与稿件
        </button>
        <button type="button" aria-pressed={kind === "asr"} onClick={() => setKind("asr")}>
          文件转写
        </button>
        <button type="button" aria-pressed={kind === "tts"} onClick={() => setKind("tts")}>
          文本配音
        </button>
        <button
          type="button"
          aria-pressed={kind === "prompter"}
          onClick={() => setKind("prompter")}
        >
          提词阅读
        </button>
      </nav>
      <p>
        切换会停止实时采集和播放并中止实时会话，其他已提交的语音任务继续运行。稿件、会议、配音文本与最终字幕草稿保留在当前标签页；文件任务未提交的输入及未保存的提词稿会清空。
      </p>
      <div hidden={kind !== "books"}>
        {booksOpened && (
          <Suspense fallback={<p role="status">正在加载有声书导出工作台…</p>}>
            <BooksHost active={kind === "books"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "assistant"}>
        {assistantOpened && (
          <Suspense fallback={<p role="status">正在加载语音助手工作台…</p>}>
            <AssistantHost active={kind === "assistant"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "voices"}>
        {voiceOpened && (
          <Suspense fallback={<p role="status">正在加载音色创作工作台…</p>}>
            <VoiceCreateHost active={kind === "voices"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "realtime-tts"}>
        {realtimeTtsOpened && (
          <Suspense fallback={<p role="status">正在加载实时配音工作台…</p>}>
            <RealtimeTtsHost active={kind === "realtime-tts"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "realtime"}>
        {realtimeOpened && (
          <Suspense fallback={<p role="status">正在加载实时字幕工作台…</p>}>
            <RealtimeHost active={kind === "realtime"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "meetings"}>
        {meetingOpened && (
          <Suspense fallback={<p role="status">正在加载会议工作台…</p>}>
            <MeetingHost active={kind === "meetings"} />
          </Suspense>
        )}
      </div>
      <div hidden={kind !== "deletions"}>
        {deletionOpened && (
          <Suspense fallback={<p role="status">正在加载删除工作台…</p>}>
            <DeletionHost active={kind === "deletions"} />
          </Suspense>
        )}
      </div>
      {kind === "voices" ||
      kind === "books" ||
      kind === "assistant" ||
      kind === "realtime-tts" ||
      kind === "realtime" ||
      kind === "meetings" ||
      kind === "deletions" ? null : kind === "manuscripts" ? (
        <Suspense fallback={<p role="status">正在加载稿件工作台…</p>}>
          <ManuscriptHost speechSource={speechSource} />
        </Suspense>
      ) : kind === "prompter" ? (
        <TeleprompterPanel />
      ) : (
        <FileTaskHost
          key={kind}
          kind={kind}
          workbench={workbench}
          onImportTranscript={(result) => {
            setSpeechSource({
              kind: "speech_artifact",
              artifact_id: result.transcript_artifact_id,
              receipt_id: result.receipt_id,
            });
            setKind("manuscripts");
          }}
        />
      )}
    </AppShell>
  );
}
