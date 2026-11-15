import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AssistantHost } from "../assistant/AssistantHost";
import { BookExportPanel } from "../books/BookExportPanel";
import { RenderUnitPanel } from "../books/RenderUnitPanel";
import { WorkbenchClient, parse } from "../manuscripts/workbench-client";
import type { RecordedRefusal } from "./command-journal";

/**
 * One settled refusal, one offer -- in all three panels that retain a command (#803).
 *
 * The render dispatch, the book export and the assistant turn are the same kind of
 * request: an Idempotency-Key-pinned create that either commits one durable resource or
 * is refused before committing anything, with the operator's original request kept in a
 * tab-scoped journal. #794/#798 taught the render panel to offer "replay under the
 * original key" only for a refusal whose own next step is to dispatch the same intent
 * again; the other two panels kept offering it for every 4xx, which was invisible until
 * #798 persisted the category and made the disagreement a stored fact.
 *
 * The three refusals are one semantic, so they get one answer. The evidence that the
 * export and assistant refusals mean the same thing is not "they are also 4xx":
 * - all three are answered by the workbench's own error envelope, and every `ErrorBody`
 *   carries `error.retryable` (`contracts/shared/errors.schema.json`, required);
 * - the workbench publishes exactly two retryable dispositions, the 429 admission refusal
 *   and the 409 cleanup wait, and both are raised by `generateRenderUnitV2` alone
 *   (`backend/.../workbench/interfaces/http_versions.py` defaults `retryable=False` and
 *   only `http_render_units.py` passes `refusal_retryable`); the contract states the field
 *   as "whether the same request can succeed without an out-of-band change";
 * - the render panel's per-code mirror of that promise (`books/render-refusals.ts`) is
 *   compared against the backend on all three sides by
 *   `tests/contract/test_render_refusal_vocabulary.py`, so "the panel's table" and "the
 *   envelope's field" are already the same answer, not two.
 *
 * What this file adds is the consumer half: for each category the journal can hold, every
 * panel must offer the same controls and must not claim more than its record knows. It is
 * deliberately one table driving three real mounted panels instead of three per-panel
 * expectations, because three separate cases is exactly how the panels drifted apart.
 */

const unitJournal = "speechnexus.workbench.units.chapter_demo.pending.v1";
const exportJournal = "speechnexus.workbench.exports.book_demo.pending.v1";
const assistantJournal = "speechnexus.workbench.assistant.pending.v1";

const chapter = parse("Chapter", {
  object: "nexus.chapter",
  id: "chapter_demo",
  book_id: "book_demo",
  title: "第一章",
  ordinal: 0,
  text_revision: "1",
  created_at: "2026-10-24T12:00:00Z",
});

const book = parse("Book", {
  object: "nexus.book",
  id: "book_demo",
  project_id: "project_demo",
  title: "合成有声书",
  status: "draft",
  casting_revision: "3",
  created_at: "2026-10-24T12:00:00Z",
  updated_at: "2026-10-24T12:00:00Z",
});

/** The record the panels restore: the intent, plus the refusal its answer settled. */
function retained(storageKey: string, command: unknown, refused: RecordedRefusal | null): void {
  sessionStorage.setItem(
    storageKey,
    JSON.stringify({ version: 1, command, uncertain: false, refused }),
  );
}

function emptyList() {
  return { object: "nexus.list", data: [], has_more: false, next_cursor: null };
}

type Panel = {
  readonly label: string;
  readonly aside: string;
  readonly replay: string;
  readonly dismiss: string;
  readonly seed: (refused: RecordedRefusal | null) => void;
  readonly mount: () => Promise<void> | void;
};

const PANELS: readonly Panel[] = [
  {
    label: "渲染单元",
    aside: "单元原请求恢复",
    replay: "按原键重放单元请求",
    dismiss: "结束已拒绝单元请求",
    seed: (refused) =>
      retained(
        unitJournal,
        {
          kind: "generate",
          chapterId: "chapter_demo",
          unitId: "unit_demo",
          expectedRevision: "1",
          key: "parity-key",
        },
        refused,
      ),
    mount: () => {
      const client = new WorkbenchClient(vi.fn());
      vi.spyOn(client, "renderUnits").mockResolvedValue(
        parse("RenderUnitListV2", {
          object: "nexus.list",
          data: [],
          has_more: false,
          next_cursor: null,
        }),
      );
      vi.spyOn(client, "voices").mockResolvedValue(
        parse("VoiceList", { object: "nexus.list", data: [], has_more: false, next_cursor: null }),
      );
      render(<RenderUnitPanel client={client} chapter={chapter} />);
    },
  },
  {
    label: "有声书导出",
    aside: "导出原请求恢复",
    replay: "按原键重放导出请求",
    dismiss: "结束已拒绝导出请求",
    seed: (refused) =>
      retained(
        exportJournal,
        {
          kind: "create",
          bookId: "book_demo",
          body: { expected_casting_revision: "3", format: "zip" },
          key: "parity-key",
        },
        refused,
      ),
    mount: () => {
      render(<BookExportPanel client={new WorkbenchClient(vi.fn())} book={book} />);
    },
  },
  {
    label: "语音助手",
    aside: "助手原请求恢复",
    replay: "按原键重放助手请求",
    dismiss: "结束已拒绝助手请求",
    seed: (refused) =>
      retained(
        assistantJournal,
        {
          kind: "create",
          projectId: "project_demo",
          body: { input_text: "合成问题", voice: { id: "voice_demo", revision: "1" } },
          key: "parity-key",
        },
        refused,
      ),
    mount: async () => {
      const client = new WorkbenchClient(vi.fn());
      vi.spyOn(client, "status").mockResolvedValue(
        parse("WorkbenchStatusV2", {
          object: "nexus.workbench_status",
          workbench_enabled: true,
          llm_enabled: false,
          shared_deployment_notice: "shared_deployment",
          capabilities: [{ name: "assistant", status: "available" }],
          minutes_profile: null,
        }),
      );
      vi.spyOn(client, "voices").mockResolvedValue(emptyList() as never);
      vi.spyOn(client, "projects").mockResolvedValue(emptyList() as never);
      render(<AssistantHost client={client} />);
      // The capability read owns the panel's `busy` bit, and a busy panel disables the
      // replay control whatever its record says: settle the read before reading controls.
      await screen.findByText(/本部署已配置文本模型与语音底座/);
    },
  },
];

/**
 * What one record commits a panel to, whatever wrote it.
 *
 * `replay` is the question `refusedReplayable` answers, `dismiss` is "a refusal is ended
 * explicitly", and `copy` is the one sentence the aside must carry: an unobserved answer
 * may not claim one, a retryable refusal names its wait, a terminal one says repeating the
 * request changes nothing, and an unclassified record may not call itself deterministic
 * because it does not know that.
 *
 * Keyed by the value the journal can hold, so a category added to `RecordedRefusal`
 * without a panel answer is a `pnpm typecheck` failure, not an untested state.
 */
type ScenarioKey = RecordedRefusal | "none";
const SCENARIOS: Record<
  ScenarioKey,
  {
    readonly refused: RecordedRefusal | null;
    readonly replay: boolean;
    readonly dismiss: boolean;
    readonly copy: string;
  }
> = {
  none: { refused: null, replay: true, dismiss: false, copy: "未确认" },
  retryable: { refused: "retryable", replay: true, dismiss: true, copy: "可重试" },
  terminal: { refused: "terminal", replay: false, dismiss: true, copy: "不会改变结果" },
  unclassified: {
    refused: "unclassified",
    replay: false,
    dismiss: true,
    copy: "没有保存该拒绝是否可重试",
  },
};

describe("one refusal, one offer across the three retained-command panels (#803)", () => {
  it.each(Object.entries(SCENARIOS))(
    "a %s record reads the same in the render, export and assistant panels",
    async (state, { refused, replay, dismiss, copy }) => {
      for (const panel of PANELS) {
        panel.seed(refused);
        await panel.mount();
        const aside = screen.getByLabelText(panel.aside);
        const actions = within(aside);
        const replayControl = actions.getByRole<HTMLButtonElement>("button", {
          name: panel.replay,
        });
        expect(replayControl.disabled, `${panel.label} replay (${state})`).toBe(!replay);
        // The explicit end is the only way out of a recorded refusal, in every panel; an
        // unobserved answer must not offer it, because the key may still find a Job.
        expect(
          actions.queryByRole("button", { name: panel.dismiss }) !== null,
          `${panel.label} dismiss (${state})`,
        ).toBe(dismiss);
        // The paragraph may not promise a handle its state withholds, and it may not claim
        // an answer its record does not hold.
        expect(aside.textContent, `${panel.label} copy (${state})`).toContain(copy);
        if (refused !== "terminal")
          expect(aside.textContent, `${panel.label} determinism`).not.toContain("不会改变结果");
      }
    },
  );
});

beforeEach(() => sessionStorage.clear());
