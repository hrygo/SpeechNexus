import { unitJournal, type UnitCommand } from "./book-journal";
import type { CommandJournal } from "../recovery/command-journal";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";
import {
  catalogueCopy,
  catalogueProblemOf,
  catalogueRows,
  type CatalogueProblem,
  type CatalogueRead,
} from "../voices/catalogue-state";
import "./books.css";

const problems = {
  unavailable: "当前部署尚未启用工作台，或连接不可用。",
  conflict: "该单元已被修改（修订冲突）。请刷新后按新修订重做，本页不会自动重试。",
  rejected: "请求被拒绝。请核对单元文本或声音后再重做。",
  missing: "该章节或单元已不可读取。",
  unknown: "结果尚未确认。请核对连接后按原键重试，不要重新创建。",
  storage: "恢复信息未能保存，已暂停提交。请修复浏览器存储后重试。",
} as const;

export type RenderUnitProblem = keyof typeof problems;

export type VoiceProblem = CatalogueProblem;

const revisionProblems = {
  missing: "该音色修订不存在（404）。",
  unavailable: "音色目录不可用：当前部署未配置语音底座，或底座未提供该修订。",
  unknown: "音色修订读取结果未确认，请稍后重试。",
} as const;

export type RevisionProblem = keyof typeof revisionProblems;

function revisionProblemOf(error: unknown): RevisionProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.status === 503) return "unavailable";
    return "unknown";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

const copyProblems = {
  missing: "该已保存副本已不可读取（404）。",
  unavailable: "部署或连接不可用，副本读取结果未确认。",
  unknown: "副本读取结果未确认，请稍后重试。",
} as const;

export type CopyProblem = keyof typeof copyProblems;

const saveProblems: Record<"unavailable" | "rejected" | "unknown" | "missing", string> = {
  unavailable: "保存服务暂不可用，副本未写入。",
  rejected: "保存被拒绝，未写入副本。请核对项目与来源后重试。",
  unknown: "保存结果尚未确认。请按原键重试，不要重复保存。",
  missing: "来源制品或回执不存在，无法保存副本。",
};

function saveProblemOf(error: unknown): keyof typeof saveProblems {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404 || error.status === 410) return "missing";
    if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status))
      return "rejected";
    return "unknown";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

function copyProblemOf(error: unknown): CopyProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    return "unavailable";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

/** A pin is identified by voice and revision together; `@` is outside both alphabets. */
function voiceKey(voiceId: string, revision: string) {
  return `${voiceId}@${revision}`;
}

function splitVoiceKey(key: string): { voiceId: string; voiceRevision: string } {
  const at = key.lastIndexOf("@");
  return { voiceId: key.slice(0, at), voiceRevision: key.slice(at + 1) };
}

function problemOf(error: unknown): RenderUnitProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.code === "outcome_unknown") return "unknown";
    if (error.code === "revision_conflict") return "conflict";
    if (error.code === "invalid_change") return "rejected";
    if (error.status >= 500 || error.code === "store_unavailable") return "unavailable";
    return "unknown";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

interface Draft {
  text: string;
  voiceId: string;
  voiceRevision: string;
}

function draftOf(drafts: Record<string, Draft>, unit: Contracts["RenderUnitV2"]): Draft {
  return (
    drafts[unit.id] ?? {
      text: unit.text,
      voiceId: unit.voice.id,
      voiceRevision: unit.voice.revision,
    }
  );
}

/**
 * A unit is revised, never silently regenerated: the panel sends the pins the user
 * changed with the revision they saw, and a conflict is reported instead of retried.
 */
function ChapterUnits({
  client,
  chapter,
}: {
  client: WorkbenchClient;
  chapter: Contracts["Chapter"];
}) {
  const [units, setUnits] = useState<Contracts["RenderUnitV2"][]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [problem, setProblem] = useState<RenderUnitProblem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [recovery, setRecovery] = useState<{
    journal: CommandJournal<UnitCommand> | null;
    pending: UnitCommand | null;
    rejected: boolean;
    blocked: boolean;
  }>(() => {
    try {
      const journal = unitJournal(chapter.id),
        record = journal.load();
      if (record.command && record.command.chapterId !== chapter.id)
        throw new Error("Unit scope mismatch");
      return { journal, pending: record.command, rejected: record.rejected, blocked: false };
    } catch {
      return { journal: null, pending: null, rejected: false, blocked: true };
    }
  });
  const owner = useRef({ generation: 0, alive: true });
  const flight = useRef(false);
  useEffect(() => {
    const current = owner.current,
      generation = ++current.generation;
    current.alive = true;
    return () => {
      queueMicrotask(() => {
        if (current.generation === generation) current.alive = false;
      });
    };
  }, []);
  const locked = busy || recovery.pending !== null || recovery.blocked;
  const generating = units
    .filter((unit) => unit.status === "generating")
    .map((unit) => unit.id)
    .join(",");
  useEffect(() => {
    if (!generating) return;
    const ids = generating.split(",");
    let cancelled = false;
    const timer = setInterval(() => {
      for (const id of ids) {
        void client
          .renderUnit(id)
          .then((next) => {
            if (cancelled || !owner.current.alive) return;
            setUnits((current) => current.map((item) => (item.id === next.id ? next : item)));
            if (next.status !== "generating")
              setNotice(
                `单元 ${next.id}：生成状态 ${next.status}${
                  next.saved_asset_id === null ? "" : `，已保存副本 ${next.saved_asset_id}`
                }。`,
              );
          })
          .catch((error: unknown) => {
            if (!cancelled) setProblem(problemOf(error));
          });
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, generating]);

  const [catalogue, setCatalogue] = useState<CatalogueRead>({ kind: "unread" });
  const voices = catalogueRows(catalogue);
  const [createProblem, setCreateProblem] = useState<RenderUnitProblem | null>(null);
  const [create, setCreate] = useState({
    ordinal: "",
    text: "",
    voiceId: "",
    voiceRevision: "",
    model: "",
    bindingId: "",
    bindingRevision: "",
    key: crypto.randomUUID(),
  });
  const setCreateField = (field: keyof typeof create, value: string) =>
    setCreate((current) => ({ ...current, [field]: value }));
  // A successful create clears the fields, which disables the submit button and would drop
  // keyboard focus to the document; it goes back to the first field instead.
  const firstField = useRef<HTMLInputElement>(null);
  // Focus must move in the same commit that re-enables the fieldset: the success path calls
  // focus() while the form is still disabled, so deferring to a passive effect can leave focus
  // on the document until a later task and lose the keyboard position.
  useLayoutEffect(() => {
    if (!busy && notice?.includes("已创建为修订")) firstField.current?.focus();
  }, [busy, notice]);
  const [check, setCheck] = useState<{
    voiceId: string;
    revision: string;
    record: Contracts["VoiceRevision"] | null;
    problem: RevisionProblem | null;
  }>({ voiceId: "", revision: "", record: null, problem: null });
  const [saving, setSaving] = useState<{
    unitId: string | null;
    problem: keyof typeof saveProblems | null;
  }>({ unitId: null, problem: null });
  const saveKeys = useRef(new Map<string, string>());
  const [history, setHistory] = useState<{
    unitId: string;
    items: Contracts["RenderUnitGenerationListV2"]["data"];
    hasMore: boolean;
    problem: "unavailable" | null;
  } | null>(null);
  const [copy, setCopy] = useState<{
    assetId: string | null;
    asset: Contracts["SavedAsset"] | null;
    problem: CopyProblem | null;
    busy: boolean;
  }>({ assetId: null, asset: null, problem: null, busy: false });

  async function execute(command: UnitCommand, fresh: boolean): Promise<boolean> {
    if (flight.current || (fresh && recovery.pending !== null)) return false;
    const journal = recovery.journal;
    try {
      if (!journal) throw new Error("Recovery unavailable");
      if (fresh) journal.begin(command);
      else journal.save(journal.load());
      setRecovery({ journal, pending: journal.load().command, rejected: false, blocked: false });
    } catch {
      setRecovery((r) => ({ ...r, blocked: true }));
      setProblem("storage");
      return false;
    }
    flight.current = true;
    setBusy(true);
    try {
      let accepted: Contracts["RenderUnitGenerationAcceptedV2"] | null = null;
      let updated: Contracts["RenderUnitV2"];
      if (command.kind === "generate") {
        accepted = await client.generateRenderUnit(
          command.unitId,
          command.expectedRevision,
          command.key,
        );
        updated = accepted.render_unit;
      } else if (command.kind === "create") {
        updated = await client.createRenderUnit(command.chapterId, command.body, command.key);
      } else {
        updated = await client.updateRenderUnit(command.unitId, command.body, command.key);
      }
      if (!owner.current.alive) return false;
      if (
        updated.chapter_id !== command.chapterId ||
        (command.kind !== "create" && updated.id !== command.unitId)
      )
        throw new Error("Unit response scope mismatch");
      try {
        journal.clear();
      } catch {
        setRecovery((r) => ({ ...r, blocked: true }));
        setProblem("storage");
        return false;
      }
      setRecovery({ journal, pending: null, rejected: false, blocked: false });
      setUnits((current) =>
        command.kind === "create"
          ? [...current.filter((item) => item.id !== updated.id), updated]
          : current.map((item) => (item.id === updated.id ? updated : item)),
      );
      if (command.kind === "create") {
        setCreateProblem(null);
        setCreate((current) => ({ ...current, ordinal: "", text: "", key: crypto.randomUUID() }));
        firstField.current?.focus();
        setNotice(`单元 ${updated.ordinal + 1} 已创建为修订 ${updated.revision}。`);
      } else if (command.kind === "patch") {
        setDrafts((current) => {
          const next = { ...current };
          delete next[command.unitId];
          return next;
        });
        setNotice(
          updated.revision === command.body.expected_revision
            ? `单元 ${updated.id}：没有变化，未生成新的修订。`
            : `单元 ${updated.id}：修订 ${command.body.expected_revision} → ${updated.revision}，状态 ${updated.status}，需要重新生成。`,
        );
      } else {
        setNotice(
          `单元 ${updated.id}：已提交生成，语音作业 ${accepted?.speech_job_id ?? ""}，状态 ${updated.status}。`,
        );
      }
      setProblem(null);
      return true;
    } catch (error) {
      if (!owner.current.alive) return false;
      const rejected =
        error instanceof WorkbenchResponseError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status);
      try {
        journal.failure(rejected);
        const record = journal.load();
        setRecovery({
          journal,
          pending: record.command,
          rejected: record.rejected,
          blocked: false,
        });
        const problem = record.uncertain ? "unknown" : problemOf(error);
        if (command.kind === "create") setCreateProblem(problem);
        else setProblem(problem);
      } catch {
        setRecovery((r) => ({ ...r, blocked: true }));
        setProblem("storage");
      }
      return false;
    } finally {
      flight.current = false;
      if (owner.current.alive) setBusy(false);
    }
  }
  function dismissRejected() {
    if (!recovery.rejected || busy || !recovery.journal) return;
    try {
      recovery.journal.clear();
      setRecovery((r) => ({ ...r, pending: null, rejected: false, blocked: false }));
      setCreateProblem(null);
      setProblem(null);
    } catch {
      setRecovery((r) => ({ ...r, blocked: true }));
      setProblem("storage");
    }
  }
  async function addUnit(input: {
    ordinal: number;
    text: string;
    voiceId: string;
    voiceRevision: string;
    model: string;
    bindingId: string;
    bindingRevision: string;
    key: string;
  }) {
    return execute(
      {
        kind: "create",
        chapterId: chapter.id,
        key: input.key,
        body: {
          ordinal: input.ordinal,
          text: input.text,
          voice: { id: input.voiceId, revision: input.voiceRevision },
          model: input.model,
          model_binding: { id: input.bindingId, revision: input.bindingRevision },
        },
      },
      true,
    );
  }

  /**
   * A saved copy is pinned to the producer receipt, not to the unit revision: the same
   * key replays an unknown save, and a fresh unit read carries the copy identity back.
   */
  async function saveCopy(target: Contracts["RenderUnitV2"]) {
    if (
      saving.unitId !== null ||
      target.source_artifact_id == null ||
      target.source_receipt_id == null
    )
      return;
    const key = saveKeys.current.get(target.id) ?? crypto.randomUUID();
    saveKeys.current.set(target.id, key);
    setSaving({ unitId: target.id, problem: null });
    try {
      const book = await client.book(chapter.book_id);
      const asset = await client.saveProjectAsset(
        book.project_id,
        {
          source_artifact_id: target.source_artifact_id,
          source_receipt_id: target.source_receipt_id,
        },
        key,
      );
      saveKeys.current.delete(target.id);
      const fresh = await client.renderUnit(target.id);
      if (!owner.current.alive) return;
      setUnits((current) => current.map((item) => (item.id === fresh.id ? fresh : item)));
      setSaving({ unitId: null, problem: null });
      setNotice(`单元 ${target.id}：已保存独立副本 ${asset.id}。`);
    } catch (error) {
      if (!owner.current.alive) return;
      setSaving({ unitId: target.id, problem: saveProblemOf(error) });
    }
  }

  /** Dispatch names the revision the operator saw; the key stays the recovery identity. */
  async function generate(target: Contracts["RenderUnitV2"]) {
    return execute(
      {
        kind: "generate",
        chapterId: chapter.id,
        unitId: target.id,
        expectedRevision: target.revision,
        key: crypto.randomUUID(),
      },
      true,
    );
  }

  /** A pinned voice is only trusted after its own revision has been read. */
  async function readVoiceRevision(voiceId: string, revision: string) {
    try {
      const record = await client.voiceRevision(voiceId, revision);
      setCheck({ voiceId, revision, record, problem: null });
    } catch (error) {
      setCheck({ voiceId, revision, record: null, problem: revisionProblemOf(error) });
    }
  }

  /** Read-back is explicit: the copy is fetched only when the operator asks for it. */
  async function showHistory(target: Contracts["RenderUnitV2"]) {
    setHistory({ unitId: target.id, items: [], hasMore: false, problem: null });
    try {
      // The contract caps the page at 100; load it whole so a unit with a long
      // retry history does not need a second click to become complete.
      const list = await client.renderUnitGenerations(target.id, 100);
      setHistory({
        unitId: target.id,
        items: list.data,
        hasMore: list.has_more,
        problem: null,
      });
    } catch {
      setHistory({ unitId: target.id, items: [], hasMore: false, problem: "unavailable" });
    }
  }

  async function readCopy(assetId: string) {
    setCopy({ assetId, asset: null, problem: null, busy: true });
    try {
      const asset = await client.savedAsset(assetId);
      setCopy({ assetId, asset, problem: null, busy: false });
    } catch (error) {
      setCopy({ assetId, asset: null, problem: copyProblemOf(error), busy: false });
    }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await client.renderUnits(chapter.id);
        if (cancelled) return;
        setUnits(page.data);
        setDrafts({});
        setProblem(null);
      } catch (error) {
        if (!cancelled) setProblem(problemOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, chapter, reload]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setCatalogue({ kind: "loading" });
      try {
        const page = await client.voices();
        if (cancelled) return;
        setCatalogue({ kind: "ready", voices: page.data });
      } catch (error) {
        // An absent catalogue is reported as absent: the panel never fills the picker
        // with invented voices to hide a missing or unconfigured speech base, and never
        // lets a failed read pass for a deployment that published nothing.
        if (cancelled) return;
        setCatalogue({ kind: "problem", problem: catalogueProblemOf(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, reload]);

  async function redo(unit: Contracts["RenderUnitV2"]) {
    const draft = draftOf(drafts, unit);
    const patch: Contracts["RenderUnitPatchV2"] = { expected_revision: unit.revision };
    if (draft.text !== unit.text) patch.text = draft.text;
    if (draft.voiceId !== unit.voice.id || draft.voiceRevision !== unit.voice.revision) {
      patch.voice = { id: draft.voiceId, revision: draft.voiceRevision };
    }
    if (patch.text === undefined && patch.voice === undefined) {
      setNotice(`单元 ${unit.id}：没有变化，未提交重做。`);
      return;
    }
    setNotice(null);
    await execute(
      {
        kind: "patch",
        chapterId: chapter.id,
        unitId: unit.id,
        body: patch,
        key: crypto.randomUUID(),
      },
      true,
    );
  }

  return (
    <section className="books-units" aria-label="渲染单元">
      <h3>渲染单元</h3>
      {(recovery.pending || recovery.blocked) && (
        <aside aria-label="单元原请求恢复">
          <p>
            {recovery.blocked
              ? problems.storage
              : "原请求已保存在此标签页。不会自动重放，结果未知时不能更换意图。"}
          </p>
          {recovery.pending && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void execute(recovery.pending!, false)}
            >
              按原键重放单元请求
            </button>
          )}
          {recovery.rejected && (
            <button type="button" disabled={busy} onClick={dismissRejected}>
              结束已拒绝单元请求
            </button>
          )}
        </aside>
      )}
      {problem !== null && (
        <p className="books-problem" role="alert">
          {problems[problem]}
        </p>
      )}
      {notice !== null && <p className="books-notice">{notice}</p>}
      {catalogue.kind === "loading" && <p aria-busy="true">{catalogueCopy.loading}</p>}
      {catalogue.kind === "ready" && voices.length === 0 && <p>{catalogueCopy.empty}</p>}
      {catalogue.kind === "problem" && (
        <aside className="books-problem" aria-label="音色目录不可用">
          <p role="alert">{catalogueCopy[catalogue.problem]}</p>
          <button type="button" disabled={busy} onClick={() => setReload((value) => value + 1)}>
            {catalogueCopy.retry}
          </button>
        </aside>
      )}
      <form
        aria-label="音色修订核对"
        onSubmit={(event) => {
          event.preventDefault();
          void readVoiceRevision(check.voiceId, check.revision);
        }}
      >
        <p>按 id 与修订号读取该已发布音色修订（退役与禁用原因只在修订上，不在目录摘要里）。</p>
        <label>
          核对音色 id
          <input
            required
            value={check.voiceId}
            onChange={(event) =>
              setCheck((current) => ({ ...current, voiceId: event.target.value }))
            }
          />
        </label>
        <label>
          核对音色修订
          <input
            required
            value={check.revision}
            onChange={(event) =>
              setCheck((current) => ({ ...current, revision: event.target.value }))
            }
          />
        </label>
        <button type="submit" disabled={busy || check.voiceId === "" || check.revision === ""}>
          读取该音色修订
        </button>
      </form>
      {check.record !== null && (
        <p role="status" aria-label="音色修订信息">
          {check.record.display_name}（修订 {check.record.revision}）· {check.record.status} ·
          {check.record.languages.join("、") || "未声明语言"} ·
          {check.record.audio_formats.join("、") || "未声明格式"} · 绑定
          {check.record.model_binding.id}@{check.record.model_binding.revision} · 发布
          {check.record.published_at} · 退役 {check.record.retired_at ?? "未退役"} · 禁用原因
          {check.record.disabled_reason ?? "无"}
        </p>
      )}
      {check.problem !== null && (
        <p role="alert" aria-label="音色修订错误">
          {revisionProblems[check.problem]}
        </p>
      )}
      <button type="button" onClick={() => setReload((value) => value + 1)} disabled={busy}>
        刷新单元
      </button>
      <form
        aria-label="新增渲染单元"
        onSubmit={(event) => {
          event.preventDefault();
          void addUnit({
            ordinal: Number(create.ordinal),
            text: create.text,
            voiceId: create.voiceId,
            voiceRevision: create.voiceRevision,
            model: create.model,
            bindingId: create.bindingId,
            bindingRevision: create.bindingRevision,
            key: create.key,
          });
        }}
      >
        <fieldset disabled={locked}>
          <p>
            新增渲染单元（固定音色与模型版本；语音底座不可用时仍可登记引用，能否生成由部署决定）
          </p>
          <label>
            新单元序号
            <input
              ref={firstField}
              type="number"
              min={0}
              required
              value={create.ordinal}
              onChange={(event) => setCreateField("ordinal", event.target.value)}
            />
          </label>
          <label>
            新单元文本
            <textarea
              required
              value={create.text}
              onChange={(event) => setCreateField("text", event.target.value)}
            />
          </label>
          {voices.length > 0 && (
            <label>
              从已发布音色填入
              <select
                value=""
                onChange={(event) => {
                  const pin = splitVoiceKey(event.target.value);
                  setCreate((current) => ({
                    ...current,
                    voiceId: pin.voiceId,
                    voiceRevision: pin.voiceRevision,
                  }));
                }}
              >
                <option value="">选择已发布音色</option>
                {voices.map((voice) => (
                  <option
                    key={voiceKey(voice.voice_id, voice.revision)}
                    value={voiceKey(voice.voice_id, voice.revision)}
                  >
                    {voice.display_name}（修订 {voice.revision}）
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            新单元音色 id
            <input
              required
              value={create.voiceId}
              onChange={(event) => setCreateField("voiceId", event.target.value)}
            />
          </label>
          <label>
            新单元音色修订
            <input
              required
              value={create.voiceRevision}
              onChange={(event) => setCreateField("voiceRevision", event.target.value)}
            />
          </label>
          <label>
            新单元模型
            <input
              required
              value={create.model}
              onChange={(event) => setCreateField("model", event.target.value)}
            />
          </label>
          <label>
            新单元绑定 id
            <input
              required
              value={create.bindingId}
              onChange={(event) => setCreateField("bindingId", event.target.value)}
            />
          </label>
          <label>
            新单元绑定修订
            <input
              required
              value={create.bindingRevision}
              onChange={(event) => setCreateField("bindingRevision", event.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={
              busy ||
              create.ordinal === "" ||
              create.text === "" ||
              create.voiceId === "" ||
              create.voiceRevision === "" ||
              create.model === "" ||
              create.bindingId === "" ||
              create.bindingRevision === ""
            }
          >
            新增渲染单元
          </button>
        </fieldset>
      </form>
      {createProblem !== null && (
        <p role="alert" aria-label="新增单元错误">
          {problems[createProblem]}
        </p>
      )}
      {units.length === 0 ? (
        <p className="books-empty">该章节还没有渲染单元。</p>
      ) : (
        <ul className="books-list" aria-label="单元列表">
          {[...units]
            .sort((left, right) => left.ordinal - right.ordinal)
            .map((unit) => {
              const draft = draftOf(drafts, unit);
              const label = `单元 ${unit.ordinal + 1}`;
              return (
                <li key={unit.id} className="books-unit">
                  <p>
                    {label} · {unit.status}
                    <span className="books-status">修订 {unit.revision}</span>
                    <span className="books-status">
                      {unit.saved_asset_id == null
                        ? "尚无已保存副本"
                        : `已保存副本 ${unit.saved_asset_id}`}
                    </span>
                  </p>
                  {(["pending", "stale", "failed"] as string[]).includes(unit.status) && (
                    <button type="button" disabled={locked} onClick={() => void generate(unit)}>
                      {`${label} 生成配音`}
                    </button>
                  )}
                  {unit.status === "ready" &&
                    unit.saved_asset_id == null &&
                    unit.source_artifact_id != null &&
                    unit.source_receipt_id != null && (
                      <button
                        type="button"
                        disabled={locked || saving.unitId !== null}
                        onClick={() => void saveCopy(unit)}
                      >
                        {`${label} 保存副本`}
                      </button>
                    )}
                  {saving.unitId === unit.id && saving.problem !== null && (
                    <p role="alert" aria-label={`${label} 保存副本错误`}>
                      {saveProblems[saving.problem]}
                    </p>
                  )}
                  {unit.saved_asset_id != null && (
                    <div aria-label={`${label} 已保存副本核对`}>
                      <button
                        type="button"
                        disabled={copy.busy}
                        onClick={() => void readCopy(unit.saved_asset_id as string)}
                      >
                        {`${label} 读取副本`}
                      </button>
                      {copy.assetId === unit.saved_asset_id && copy.asset !== null && (
                        <div>
                          <p role="status" aria-label={`${label} 副本信息`}>
                            {copy.asset.media_type} · {copy.asset.bytes} 字节 · {copy.asset.sha256}{" "}
                            · 来源 {copy.asset.source_artifact_id}
                          </p>
                          <ContentDownload
                            key={copy.asset.id + copy.asset.sha256}
                            identity={copy.asset.id + copy.asset.sha256}
                            filename={`${copy.asset.id}.${copy.asset.media_type === "audio/wav" ? "wav" : copy.asset.media_type === "audio/mpeg" ? "mp3" : "bin"}`}
                            label={`${label} 校验并下载副本`}
                            load={(signal) => client.downloadSavedAsset(copy.asset!, signal)}
                          />
                        </div>
                      )}
                      {copy.assetId === unit.saved_asset_id && copy.problem !== null && (
                        <p role="alert" aria-label={`${label} 副本错误`}>
                          {copyProblems[copy.problem]}
                        </p>
                      )}
                    </div>
                  )}
                  <button
                    type="button"
                    aria-expanded={history !== null && history.unitId === unit.id}
                    aria-controls={`history-${unit.id}`}
                    disabled={locked}
                    onClick={() => void showHistory(unit)}
                  >
                    {`${label} 生成历史`}
                  </button>
                  {history !== null && history.unitId === unit.id && (
                    <div id={`history-${unit.id}`} aria-label={`${label} 生成历史`}>
                      {history.problem !== null ? (
                        <p role="alert" aria-label={`${label} 生成历史错误`}>
                          {problems[history.problem]}
                        </p>
                      ) : history.items.length === 0 ? (
                        <p className="books-status">尚无生成记录。</p>
                      ) : (
                        <>
                          <ol aria-label={`${label} 生成记录`}>
                            {history.items.map((item) => (
                              <li key={item.id}>
                                修订 {item.revision} · 作业 {item.job_id} · 依赖{" "}
                                {item.dependencies_sha256.slice(0, 23)}…
                              </li>
                            ))}
                          </ol>
                          <p role="status" className="books-status">
                            共 {history.items.length} 条生成记录。
                          </p>
                          {history.hasMore && (
                            <p className="books-status">
                              仅显示最近 {history.items.length} 条记录。
                            </p>
                          )}
                        </>
                      )}
                    </div>
                  )}
                  <fieldset disabled={locked}>
                    <label>
                      {`${label} 文本`}
                      <textarea
                        value={draft.text}
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [unit.id]: { ...draft, text: event.target.value },
                          }))
                        }
                      />
                    </label>
                    <label>
                      {`${label} 已发布音色`}
                      <select
                        value={voiceKey(draft.voiceId, draft.voiceRevision)}
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [unit.id]: { ...draft, ...splitVoiceKey(event.target.value) },
                          }))
                        }
                      >
                        <option value={voiceKey(draft.voiceId, draft.voiceRevision)}>
                          {`当前固定：${voiceKey(draft.voiceId, draft.voiceRevision)}`}
                        </option>
                        {voices
                          .filter(
                            (voice) =>
                              voiceKey(voice.voice_id, voice.revision) !==
                              voiceKey(draft.voiceId, draft.voiceRevision),
                          )
                          .map((voice) => (
                            <option
                              key={voiceKey(voice.voice_id, voice.revision)}
                              value={voiceKey(voice.voice_id, voice.revision)}
                            >
                              {`${voice.display_name}（${voice.voice_id}@${voice.revision}，${voice.status}）`}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      {`${label} 声音 ID`}
                      <input
                        value={draft.voiceId}
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [unit.id]: { ...draft, voiceId: event.target.value },
                          }))
                        }
                      />
                    </label>
                    <label>
                      {`${label} 声音修订`}
                      <input
                        value={draft.voiceRevision}
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [unit.id]: { ...draft, voiceRevision: event.target.value },
                          }))
                        }
                      />
                    </label>
                    <button type="button" disabled={busy} onClick={() => void redo(unit)}>
                      {`重做${label}`}
                    </button>
                  </fieldset>
                </li>
              );
            })}
        </ul>
      )}
    </section>
  );
}

/** A chapter owns its in-flight results and recovery record. Switching never reuses its state. */
export function RenderUnitPanel(props: { client: WorkbenchClient; chapter: Contracts["Chapter"] }) {
  return <ChapterUnits key={props.chapter.id} {...props} />;
}
export default RenderUnitPanel;
import { ContentDownload } from "./ContentDownload";
