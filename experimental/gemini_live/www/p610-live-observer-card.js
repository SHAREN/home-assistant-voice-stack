import { emptyObserverState, mergeObserverHistory, reduceObserverEvent } from "./p610-live-observer-state.mjs?v=1.0.4-p610.4";
const EVENT_TYPE = "gemini_live_turn_event";
const HISTORY_COMMAND = "gemini_live/p610_history";
class P610LiveObserverCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this._state = emptyObserverState(); this._hass = null; this._unsubscribe = null; this._subscribing = false; this._subscriptionGeneration = 0; this._historyLoaded = false; this.render(); }
  setConfig(config) { this._config = config || {}; this.render(); }
  set hass(value) { if (this._hass !== value) { this._subscriptionGeneration += 1; if (this._unsubscribe) this._unsubscribe(); this._unsubscribe = null; this._subscribing = false; this._historyLoaded = false; } this._hass = value; this.ensureSubscription(); }
  connectedCallback() { this.ensureSubscription(); }
  disconnectedCallback() { this._subscriptionGeneration += 1; if (this._unsubscribe) this._unsubscribe(); this._unsubscribe = null; this._subscribing = false; }
  async ensureSubscription() {
    if (!this.isConnected || !this._hass || this._unsubscribe || this._subscribing) return;
    this._subscribing = true; const hass = this._hass; const generation = ++this._subscriptionGeneration;
    try {
      const unsubscribe = await hass.connection.subscribeEvents((event) => this.handleEvent(event?.data), EVENT_TYPE);
      if (!this.isConnected || this._hass !== hass || generation !== this._subscriptionGeneration) { unsubscribe(); return; }
      this._unsubscribe = unsubscribe;
      try {
        const limit = Math.max(1, Math.min(Number(this._config?.history_limit || 300), 500));
        const response = await hass.connection.sendMessagePromise({ type: HISTORY_COMMAND, limit });
        if (generation === this._subscriptionGeneration) { this._state = mergeObserverHistory(this._state, response?.turns || []); this._historyLoaded = true; this.renderTurns(true); }
      } catch (_historyError) { this._historyLoaded = false; this.renderTurns(); }
    } catch (_error) { if (generation === this._subscriptionGeneration) this._unsubscribe = null; }
    finally { if (generation === this._subscriptionGeneration) this._subscribing = false; }
  }
  handleEvent(payload) { const root = this.shadowRoot?.querySelector(".turns"); const nearBottom = root ? root.scrollHeight - root.scrollTop - root.clientHeight < 90 : true; const next = reduceObserverEvent(this._state, payload); if (next === this._state) return; this._state = next; this.renderTurns(nearBottom); }
  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = '<style>:host{display:block;color:var(--primary-text-color)}ha-card{min-height:420px;padding:18px;overflow:hidden;background:linear-gradient(145deg,var(--ha-card-background,var(--card-background-color)),rgba(32,108,255,.08))}.head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.title{font-size:20px;font-weight:650}.subtitle{color:var(--secondary-text-color);font-size:12px}.status{border-radius:999px;padding:5px 10px;background:rgba(32,108,255,.14);color:#4f8cff;font-size:12px}.turns{display:flex;flex-direction:column;gap:10px;max-height:72vh;overflow:auto;scrollbar-width:none}.turn{border:1px solid rgba(127,127,127,.18);border-radius:16px;padding:12px;background:rgba(127,127,127,.05)}.meta{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.chip{border-radius:999px;padding:3px 8px;background:rgba(127,127,127,.12);font-size:11px}.chip.direct_live{background:rgba(35,170,90,.16);color:#39b86b}.bubble{max-width:86%;border-radius:14px;padding:9px 11px;margin:7px 0;white-space:pre-wrap;overflow-wrap:anywhere}.user{margin-left:auto;background:rgba(32,108,255,.16)}.assistant{background:rgba(127,127,127,.14)}.role{display:block;opacity:.68;font-size:10px;margin-bottom:3px}.divider{display:flex;align-items:center;gap:8px;color:var(--secondary-text-color);font-size:11px;margin:8px 2px}.divider::before,.divider::after{content:"";height:1px;flex:1;background:rgba(127,127,127,.25)}.divider.end{margin-top:10px}.empty{color:var(--secondary-text-color);padding:48px 12px;text-align:center}.phases{color:var(--secondary-text-color);font-size:10px;margin-top:7px;line-height:1.45}.system{color:var(--secondary-text-color);font-size:12px;padding:6px 2px}</style><ha-card><div class="head"><div><div class="title"></div><div class="subtitle">Непрерывная локальная история P610: что услышано и что ответила колонка</div></div><div class="status">live + история</div></div><div class="turns"></div></ha-card>';
    this.shadowRoot.querySelector(".title").textContent = this._config?.name || "P610 Live"; this.renderTurns();
  }
  renderTurns(scrollToBottom = false) {
    const root = this.shadowRoot?.querySelector(".turns"); if (!root) return; root.replaceChildren(); const ids = [...this._state.order];
    if (!ids.length) { const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = this._historyLoaded ? "История пуста. Ожидание wake-слова." : "Загрузка истории и ожидание P610…"; root.appendChild(empty); return; }
    for (const id of ids) {
      const turn = this._state.turns[id]; if (turn.newDialog) root.appendChild(this.divider("Новый диалог · wake · " + this.formatTime(turn.startedAt)));
      const box = document.createElement("div"); box.className = "turn"; const meta = document.createElement("div"); meta.className = "meta"; const live = document.createElement("span"); live.className = "chip " + turn.gateAction; live.textContent = turn.gateAction === "direct_live" ? "Gemini Live" : turn.status; meta.appendChild(live);
      for (const toolName of turn.toolNames || []) { const tool = document.createElement("span"); tool.className = "chip"; tool.textContent = "инструмент: " + toolName; meta.appendChild(tool); } box.appendChild(meta);
      const userText = turn.userText || turn.partialText; if (userText) box.appendChild(this.bubble(userText, "user", turn.userText ? "Вы · распознано" : "Вы · слышу сейчас")); if (turn.assistantText) box.appendChild(this.bubble(turn.assistantText, "assistant", "Колонка"));
      if (turn.status === "failed" && !userText) { const system = document.createElement("div"); system.className = "system"; system.textContent = "Речь не распознана — ответ не был сформирован."; box.appendChild(system); }
      const phases = document.createElement("div"); phases.className = "phases"; phases.textContent = Object.entries(turn.phases || {}).map(([stage, elapsed]) => stage + ": " + (Number(elapsed) / 1000).toFixed(3) + "s").join(" · "); box.appendChild(phases); root.appendChild(box);
      if (turn.dialogEnded) { const label = turn.status === "failed" ? "Диалог завершён · речь не распознана" : turn.status === "stopped" ? "Диалог завершён · стоп" : "Диалог завершён"; root.appendChild(this.divider(label, true)); }
    }
    if (scrollToBottom) root.scrollTop = root.scrollHeight;
  }
  divider(text, end = false) { const element = document.createElement("div"); element.className = "divider" + (end ? " end" : ""); element.textContent = text; return element; }
  bubble(text, role, label) { const element = document.createElement("div"); element.className = "bubble " + role; const roleElement = document.createElement("span"); roleElement.className = "role"; roleElement.textContent = label; element.appendChild(roleElement); element.append(document.createTextNode(text)); return element; }
  formatTime(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  getCardSize() { return 7; }
}
if (!customElements.get("p610-live-observer-card")) customElements.define("p610-live-observer-card", P610LiveObserverCard);
window.customCards = window.customCards || []; window.customCards.push({ type: "p610-live-observer-card", name: "P610 Live Observer", description: "Persistent local P610 transcript and lifecycle timeline" });
