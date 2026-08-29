import assert from "node:assert/strict";
import test from "node:test";
import { emptyObserverState, MAX_TURNS, mergeObserverHistory, reduceObserverEvent } from "../experimental/gemini_live/www/p610-live-observer-state.mjs";
function event(trace, sequence, stage, extra = {}) { return { schema_version: 1, source: "p610", trace_id: trace, conversation_id: extra.conversation_id || "c1", sequence, stage, elapsed_ms: sequence * 100, timestamp: extra.timestamp || "2026-08-11T07:00:00.000Z", ...extra }; }

test("groups live transcript and assistant response", () => {
  let state = emptyObserverState();
  state = reduceObserverEvent(state, event("t1", 1, "last_transcript_update", { text: "Какая погода" }));
  state = reduceObserverEvent(state, event("t1", 2, "assistant_delta", { text: "Сейчас " }));
  state = reduceObserverEvent(state, event("t1", 3, "assistant_delta", { text: "21 градус." }));
  state = reduceObserverEvent(state, event("t1", 4, "conversation_direct_live"));
  assert.equal(state.turns.t1.partialText, "Какая погода");
  assert.equal(state.turns.t1.assistantText, "Сейчас 21 градус.");
  assert.equal(state.turns.t1.gateAction, "direct_live");
});

test("marks only first trace of conversation as new dialog", () => {
  let state = emptyObserverState();
  state = reduceObserverEvent(state, event("t1", 1, "stt_start"));
  state = reduceObserverEvent(state, event("t2", 1, "stt_start"));
  state = reduceObserverEvent(state, event("t3", 1, "stt_start", { conversation_id: "c2" }));
  assert.equal(state.turns.t1.newDialog, true);
  assert.equal(state.turns.t2.newDialog, false);
  assert.equal(state.turns.t3.newDialog, true);
});

test("marks failure and normal end", () => {
  let state = emptyObserverState();
  state = reduceObserverEvent(state, event("fail", 1, "stt_failed", { reason: "no_direct_live_response_audio" }));
  state = reduceObserverEvent(state, event("end", 1, "conversation_result", { conversation_id: "c2", continue_conversation: false }));
  assert.equal(state.turns.fail.dialogEnded, true);
  assert.equal(state.turns.fail.status, "failed");
  assert.equal(state.turns.end.dialogEnded, true);
  assert.equal(state.turns.end.status, "ended");
});

test("merges persistent old turns without overwriting newer live sequence", () => {
  let state = emptyObserverState();
  state = reduceObserverEvent(state, event("live", 5, "assistant_delta", { text: "новое" }));
  state = mergeObserverHistory(state, [
    { trace_id: "old", conversation_id: "oldc", last_sequence: 3, started_at: "2026-08-11T06:00:00.000Z", user_text: "Старый вопрос", assistant_text: "Старый ответ", new_dialog: true, dialog_ended: true, status: "ended", tool_names: [], phases: {} },
    { trace_id: "live", conversation_id: "c1", last_sequence: 2, started_at: "2026-08-11T07:00:00.000Z", assistant_text: "старое", tool_names: [], phases: {} },
  ]);
  assert.equal(state.order[0], "old");
  assert.equal(state.turns.old.userText, "Старый вопрос");
  assert.equal(state.turns.live.assistantText, "новое");
});

test("caps persistent timeline", () => {
  let state = emptyObserverState();
  for (let index = 0; index < MAX_TURNS + 3; index += 1) state = reduceObserverEvent(state, event("t" + index, 1, "stt_start", { conversation_id: "c" + index }));
  assert.equal(state.order.length, MAX_TURNS);
  assert.equal(state.turns.t0, undefined);
});
