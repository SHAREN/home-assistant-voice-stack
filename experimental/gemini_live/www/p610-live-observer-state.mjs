export const MAX_TURNS = 500;

export function emptyObserverState() { return { order: [], turns: {} }; }
function asBoolean(value) { return value === true || value === 1 || value === "1" || value === "true"; }
function blankTurn(traceId, conversationId = "") {
  return { traceId, conversationId, lastSequence: 0, startedAt: "", updatedAt: "", userText: "", partialText: "", assistantText: "", gateAction: "pending", gateReason: "", continueConversation: null, newDialog: false, dialogEnded: false, endReason: "", status: "active", toolNames: [], phases: {} };
}
function copyTurn(turn, traceId, conversationId = "") {
  return turn ? { ...turn, phases: { ...turn.phases }, toolNames: [...(turn.toolNames || [])] } : blankTurn(traceId, conversationId);
}
function hasConversation(previous, conversationId) {
  if (!conversationId) return false;
  return previous.order.some((traceId) => previous.turns[traceId]?.conversationId === conversationId);
}
function capState(state) {
  while (state.order.length > MAX_TURNS) { const removed = state.order.shift(); delete state.turns[removed]; }
  return state;
}

export function reduceObserverEvent(previous, payload) {
  if (!payload || payload.schema_version !== 1 || payload.source !== "p610") return previous;
  const traceId = String(payload.trace_id || "");
  const conversationId = String(payload.conversation_id || "");
  const sequence = Number(payload.sequence || 0);
  if (!traceId || !Number.isFinite(sequence) || sequence <= 0) return previous;
  const existing = previous.turns[traceId];
  if (existing && sequence <= existing.lastSequence) return previous;
  const state = { order: [...previous.order], turns: { ...previous.turns } };
  const turn = copyTurn(existing, traceId, conversationId);
  if (!existing) {
    turn.newDialog = Boolean(conversationId && !hasConversation(previous, conversationId));
    turn.startedAt = String(payload.timestamp || "");
    state.order.push(traceId);
  }
  turn.conversationId = conversationId || turn.conversationId;
  turn.lastSequence = sequence;
  turn.updatedAt = String(payload.timestamp || turn.updatedAt || "");
  turn.phases[payload.stage] = Number(payload.elapsed_ms || 0);
  if (payload.stage === "first_input_transcription" && payload.text) turn.partialText = String(payload.text);
  if (payload.stage === "last_transcript_update") turn.partialText = payload.text ? String(payload.text) : "Текст скрыт (" + (payload.text_chars || 0) + " симв.)";
  if (payload.stage === "final_transcript") { turn.userText = payload.text ? String(payload.text) : "Текст скрыт (" + (payload.text_chars || 0) + " симв.)"; turn.partialText = ""; }
  if (payload.stage === "gate" || payload.stage === "conversation_gate") { turn.gateAction = String(payload.action || "pending"); turn.gateReason = String(payload.reason || ""); }
  if (payload.stage === "conversation_direct_live") { turn.gateAction = "direct_live"; turn.gateReason = "language_gate_bypassed"; }
  if ((payload.stage === "response_local" || payload.stage === "conversation_result") && payload.text && !String(payload.text).startsWith("-- gemini live --") && !turn.assistantText) turn.assistantText = String(payload.text);
  if (payload.stage === "assistant_delta" && payload.text) turn.assistantText += String(payload.text);
  if (payload.stage === "tool_call_boundary" && payload.tool_name) { const name = String(payload.tool_name); if (!turn.toolNames.includes(name)) turn.toolNames.push(name); }
  if (payload.stage === "conversation_result") {
    turn.continueConversation = asBoolean(payload.continue_conversation);
    if (!turn.continueConversation) { turn.dialogEnded = true; turn.endReason = "conversation_result"; turn.status = "ended"; }
  }
  if (payload.stage === "stt_failed") { turn.dialogEnded = true; turn.endReason = String(payload.reason || "stt_failed"); turn.status = "failed"; }
  if (payload.stage === "local_stop") { turn.dialogEnded = true; turn.endReason = "local_stop"; turn.status = "stopped"; }
  if (payload.stage === "direct_live_complete" && !turn.dialogEnded) turn.status = "complete";
  state.turns[traceId] = turn;
  return capState(state);
}

function fromHistory(record) {
  const traceId = String(record?.trace_id || "");
  const turn = blankTurn(traceId, String(record?.conversation_id || ""));
  turn.lastSequence = Number(record?.last_sequence || 0); turn.startedAt = String(record?.started_at || ""); turn.updatedAt = String(record?.updated_at || "");
  turn.userText = String(record?.user_text || ""); turn.partialText = String(record?.partial_text || ""); turn.assistantText = String(record?.assistant_text || "");
  turn.continueConversation = record?.continue_conversation === null || record?.continue_conversation === undefined ? null : asBoolean(record.continue_conversation);
  turn.newDialog = asBoolean(record?.new_dialog); turn.dialogEnded = asBoolean(record?.dialog_ended); turn.endReason = String(record?.end_reason || ""); turn.status = String(record?.status || "active");
  turn.toolNames = Array.isArray(record?.tool_names) ? record.tool_names.map(String) : []; turn.phases = { ...(record?.phases || {}) };
  if (turn.phases.conversation_direct_live !== undefined) { turn.gateAction = "direct_live"; turn.gateReason = "language_gate_bypassed"; }
  return turn;
}

export function mergeObserverHistory(previous, records) {
  if (!Array.isArray(records) || records.length === 0) return previous;
  const state = { order: [...previous.order], turns: { ...previous.turns } };
  for (const record of records) {
    const historical = fromHistory(record); if (!historical.traceId) continue;
    const live = state.turns[historical.traceId]; if (!live || historical.lastSequence > live.lastSequence) state.turns[historical.traceId] = historical;
    if (!state.order.includes(historical.traceId)) state.order.push(historical.traceId);
  }
  state.order.sort((a, b) => String(state.turns[a]?.startedAt || "").localeCompare(String(state.turns[b]?.startedAt || "")));
  return capState(state);
}
