#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
text = p.read_text(encoding="utf-8")

if "type ChatTurn" not in text:
    text = text.replace(
        'let openNavPanel: "how" | "leads" | "integrations" | null = null;\n',
        'let openNavPanel: "how" | "leads" | "integrations" | null = null;\n\n'
        'type ChatTurn = { role: "user" | "assistant"; text: string };\n'
        "let chatMessages: ChatTurn[] = [];\n"
        "let chatBusy = false;\n"
        'let chatDraft = "";\n',
    )

if "function renderGlassChatHtml" not in text:
    fn = r'''
function renderGlassChatHtml(): string {
  const hint =
    chatMessages.length === 0
      ? `<p class="hero-glass__chat-hint">${escapeHtml(copy("rt_glass_chat_hint", "Talk to our Hammer AI sales assistant — ask about leads, follow-up, Facebook AIA, or integrations."))}</p>`
      : "";
  const msgs = chatMessages
    .map(
      (m) => `
          <article class="glass-chat-msg glass-chat-msg--${m.role}">
            <span class="glass-chat-msg__who">${m.role === "user" ? "You" : "Hammer"}</span>
            <p>${escapeHtml(m.text)}</p>
          </article>`,
    )
    .join("");
  const typing = chatBusy
    ? `
          <article class="glass-chat-msg glass-chat-msg--assistant glass-chat-msg--typing" aria-busy="true">
            <span class="glass-chat-msg__who">Hammer</span>
            <p>${escapeHtml(copy("rt_glass_chat_thinking", "Thinking…"))}</p>
          </article>`
    : "";
  return `${hint}<motion class="hero-glass__chat-thread" role="log" aria-live="polite" aria-relevant="additions">${msgs}${typing}</motion>`;
}

'''
    fn = fn.replace("<motion ", "<" + "div ").replace("</motion>", "</" + "motion>").replace("</" + "motion>", "</motion>")
    fn = fn.replace("</" + "motion>", "</div>")
    text = text.replace(
        "async function loadSiteCopy(): Promise<void> {",
        fn + "async function loadSiteCopy(): Promise<void> {",
    )

old_input = (
    '              <div class="hero-glass__input-row">\n'
    '                <div class="hero-glass__input" role="presentation">\n'
    '                  <span class="hero-glass__input-icon" aria-hidden="true">\n'
    '                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 8h10M7 12h6"/></svg>\n'
    "                  </span>\n"
    '                  <span class="hero-glass__input-text">${escapeHtml(copy("rt_glass_input", "A lead asks if the 2022 F-150 is still available — Hammer answers in seconds"))}</span>\n'
    '                  <span class="hero-glass__input-cursor" aria-hidden="true"></span>\n'
    '                  <button type="button" class="hero-glass__input-plus" tabindex="-1" aria-hidden="true">+</button>\n'
    "                </div>\n"
    "              </div>"
)

new_input = (
    '              <motion class="hero-glass__input-row">\n'
    '                <form class="hero-glass__input hero-glass__input--live" id="glassChatForm">\n'
    '                  <span class="hero-glass__input-icon" aria-hidden="true">\n'
    '                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>\n'
    "                  </span>\n"
    '                  <input type="text" class="hero-glass__input-field" id="glassChatInput" name="message"\n'
    '                    placeholder="${escapeHtml(copy("rt_glass_input_placeholder", "Talk to our AI — ask about Hammer Drive, leads, or follow-up"))}"\n'
    '                    value="${escapeHtml(chatDraft)}"\n'
    '                    ${chatBusy ? "disabled" : ""}\n'
    '                    autocomplete="off" />\n'
    '                  <button type="submit" class="hero-glass__input-send" id="glassChatSend" ${chatBusy ? "disabled" : ""}\n'
    '                    aria-label="${escapeHtml(copy("rt_glass_send_aria", "Send message"))}">\n'
    '                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>\n'
    "                  </button>\n"
    "                </form>\n"
    "              </motion>\n"
    '              <motion class="hero-glass__chat" id="glassChat">\n'
    "                ${renderGlassChatHtml()}\n"
    "              </motion>"
)
new_input = new_input.replace("<motion ", "<div ").replace("</motion>", "</div>")

if old_input not in text:
    raise SystemExit("old input block not found")
text = text.replace(old_input, new_input, 1)

wire_old = (
    '    root.querySelector("#callBtnInner")?.addEventListener("click", onCallClick);\n'
    "    wireTryButtons();"
)
wire_new = wire_old.replace("wireTryButtons();", "wireGlassChat();\n    wireTryButtons();")
if "wireGlassChat();" not in text:
    if wire_old not in text:
        raise SystemExit("wire block not found")
    text = text.replace(wire_old, wire_new)

if "function wireGlassChat" not in text:
    helpers = r'''
  async function sendGlassChat() {
    const input = root.querySelector<HTMLInputElement>("#glassChatInput");
    const msg = (input?.value ?? chatDraft).trim();
    if (!msg || chatBusy) return;
    chatDraft = "";
    chatMessages = [...chatMessages, { role: "user", text: msg }];
    chatBusy = true;
    render();
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) throw new Error(await readHttpErrorBody(res));
      const data = (await res.json()) as { reply?: string };
      const reply = data.reply?.trim() || copy("rt_glass_chat_empty", "I could not reply to that. Try another question.");
      chatMessages = [...chatMessages, { role: "assistant", text: reply }];
    } catch (e) {
      const err = e instanceof Error ? e.message : copy("rt_glass_chat_error", "Could not reach the assistant.");
      chatMessages = [...chatMessages, { role: "assistant", text: err }];
    } finally {
      chatBusy = false;
      render();
      requestAnimationFrame(() => {
        root.querySelector<HTMLInputElement>("#glassChatInput")?.focus();
        const thread = root.querySelector(".hero-glass__chat-thread");
        if (thread) thread.scrollTop = thread.scrollHeight;
      });
    }
  }

  function wireGlassChat() {
    const form = root.querySelector<HTMLFormElement>("#glassChatForm");
    const input = root.querySelector<HTMLInputElement>("#glassChatInput");
    if (input) {
      input.value = chatDraft;
      input.oninput = () => {
        chatDraft = input.value;
      };
    }
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      void sendGlassChat();
    });
  }

'''
    text = text.replace("  function wireTryButtons() {", helpers + "  function wireTryButtons() {")

p.write_text(text, encoding="utf-8")
print("ok")
