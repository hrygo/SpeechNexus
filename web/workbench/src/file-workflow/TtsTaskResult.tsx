import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FileTaskSession } from "./file-task-session";

/**
 * The narrow part of the workbench client this result needs: list destinations, then copy a
 * committed artifact into one. Structural, so the file workflow never imports the workbench
 * slice, and a deployment without a workbench simply passes nothing.
 */
export interface WorkbenchSaveClient {
  projects(
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<{ data: { id: string; name: string }[] }>;
  saveProjectAsset(
    projectId: string,
    body: { source_artifact_id: string; source_receipt_id: string },
    key: string,
  ): Promise<{ id: string }>;
}

export function TtsTaskResult({
  session,
  workbench,
}: {
  session: FileTaskSession;
  workbench?: WorkbenchSaveClient | undefined;
}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const player = useRef<HTMLAudioElement>(null);
  const owner = useRef({ generation: 0, alive: true, url: undefined as string | undefined });
  const [notice, setNotice] = useState("");
  const audioId =
    state.job?.result?.kind === "tts" ? state.job.result.audio_artifact_id : undefined;
  const receiptId = state.job?.result?.kind === "tts" ? state.job.result.receipt_id : undefined;
  const [projects, setProjects] = useState<{ id: string; name: string }[] | null>(null);
  const [target, setTarget] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedId, setSavedId] = useState("");
  const [saveProblem, setSaveProblem] = useState("");
  const saveKey = useRef("");
  function stop() {
    owner.current.generation++;
    const audio = player.current;
    audio?.pause();
    audio?.removeAttribute("src");
    audio?.load();
    if (owner.current.url) URL.revokeObjectURL(owner.current.url);
    owner.current.url = undefined;
  }
  useEffect(() => {
    if (!workbench || !audioId) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await workbench.projects();
        if (cancelled) return;
        setProjects(page.data);
        setSaveProblem("");
      } catch {
        if (cancelled) return;
        // No project list means no destination: report the gap instead of inventing a
        // project or claiming a copy that was never written.
        setProjects(null);
        setSaveProblem("无法读取项目列表，独立保存暂不可用。");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workbench, audioId]);
  useEffect(() => {
    const lifecycle = owner.current;
    lifecycle.alive = true;
    const audio = player.current;
    const release = () => {
      lifecycle.generation++;
      audio?.pause();
      audio?.removeAttribute("src");
      audio?.load();
      if (lifecycle.url) URL.revokeObjectURL(lifecycle.url);
      lifecycle.url = undefined;
    };
    const hidden = () => {
      if (document.hidden) release();
    };
    window.addEventListener("pagehide", release);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      lifecycle.alive = false;
      release();
      window.removeEventListener("pagehide", release);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [session]);
  async function save() {
    if (!workbench || !audioId || !receiptId || target === "" || saving) return;
    // One idempotency key per source pair: a retry after an unknown outcome replays the
    // original attempt instead of creating a second copy.
    if (saveKey.current === "") saveKey.current = crypto.randomUUID();
    setSaving(true);
    setSaveProblem("");
    try {
      const asset = await workbench.saveProjectAsset(
        target,
        { source_artifact_id: audioId, source_receipt_id: receiptId },
        saveKey.current,
      );
      setSavedId(asset.id);
      saveKey.current = "";
    } catch (error) {
      const status = (error as { status?: unknown }).status;
      setSaveProblem(
        typeof status === "number" && status < 500
          ? "保存被拒绝，未写入副本。请核对项目与制品后重试。"
          : "保存结果未确认。请按同一幂等键重试，不要重复新建。",
      );
    } finally {
      setSaving(false);
    }
  }

  async function load(download: boolean) {
    if (!audioId || document.hidden) return;
    stop();
    const generation = owner.current.generation;
    setNotice("");
    const result = await session.download(audioId);
    const current = () =>
      owner.current.alive && generation === owner.current.generation && !document.hidden;
    if (!result || !current()) return;
    if (result.artifact.kind !== "audio" || result.artifact.media_type !== "audio/wav") {
      setNotice("音频格式与本次 WAV 请求不一致，请重新读取任务。");
      return;
    }
    const url = URL.createObjectURL(result.blob);
    if (download) {
      const anchor = document.createElement("a");
      try {
        anchor.href = url;
        anchor.download = "speech.wav";
        document.body.append(anchor);
        anchor.click();
        setNotice("已交给浏览器下载 WAV。");
      } finally {
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }
      return;
    }
    const audio = player.current;
    if (!audio) {
      URL.revokeObjectURL(url);
      return;
    }
    owner.current.url = url;
    audio.src = url;
    try {
      await audio.play();
      if (current()) setNotice("正在试听任务音频。");
    } catch {
      if (current()) setNotice("浏览器未开始播放，可使用播放器控件或下载 WAV。");
    }
  }
  return (
    <section aria-label="任务配音结果">
      <h3>配音音频</h3>
      <p>试听或下载本任务已提交的 WAV，单个制品上限 16 MiB。离开页面或隐藏时停止试听。</p>
      {!audioId && <p>尚无已提交音频。</p>}
      <audio
        ref={player}
        aria-label="任务配音播放器"
        controls
        preload="none"
        onEnded={() => setNotice("试听已结束。")}
        onError={() => setNotice("音频无法播放，请重新读取任务或下载核对。")}
      />
      <div className="file-task-actions">
        <button type="button" disabled={!audioId || state.busy} onClick={() => void load(false)}>
          试听音频
        </button>
        <button
          type="button"
          onClick={() => {
            stop();
            setNotice("已停止试听。");
          }}
        >
          停止试听
        </button>
        <button type="button" disabled={!audioId || state.busy} onClick={() => void load(true)}>
          下载 WAV
        </button>
      </div>
      {workbench && (
        <div className="file-task-save">
          <label>
            目标项目
            <select
              value={target}
              disabled={saving || projects === null}
              onChange={(event) => setTarget(event.target.value)}
            >
              <option value="">请选择项目</option>
              {(projects ?? []).map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={saving || !audioId || !receiptId || target === ""}
            onClick={() => void save()}
          >
            保存到工作台
          </button>
          {savedId && <p role="status">{`已保存副本 ${savedId}`}</p>}
          {saveProblem && <p role="alert">{saveProblem}</p>}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
