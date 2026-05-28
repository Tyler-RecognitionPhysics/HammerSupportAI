import { Conversation } from "@elevenlabs/client";
import "./support.css";

type SiteCopy = Record<string, string>;
type UiState = "idle" | "connecting" | "live" | "error";

let siteCopy: SiteCopy = {};
let uiState: UiState = "idle";
let statusText = "";
let errorDetail = "";
let transcript: { role: "user" | "agent"; text: string }[] = [];
let chatMessages: { role: "user" | "assistant"; content: string }[] = [];
let chatBusy = false;
let chatInputDraft = "";
let focusChatInput = false;
let voiceConv: Conversation | null = null;
let voiceCallEpoch = 0;

function voiceConnectErrorMessage(raw: string): string {
  const msg = raw.trim();
  if (!msg) {
    return copy(
      "rt_error_voice_connect",
      "Voice disconnected before the call started. Check that ngrok is running (port 8781) and your ElevenLabs agent Custom LLM URL matches it.",
    );
  }
  if (/NotAllowedError|Permission denied|microphone/i.test(msg)) {
    return copy(
      "rt_error_mic_denied",
      "Microphone access was blocked. Allow the mic for this site in your browser, then try again.",
    );
  }
  return msg;
}

function patchVoiceChrome(): void {
  const btn = document.getElementById("btn-hero-voice");
  const label = btn?.querySelector(".hero-voice-btn__label");
  const live = uiState === "live";
  const connecting = uiState === "connecting";
  if (!btn || !label) return;

  btn.classList.toggle("hero-voice-btn--live", live);
  btn.toggleAttribute("disabled", connecting);

  label.textContent = connecting
    ? copy("rt_status_connecting", "Connecting…")
    : live
      ? copy("rt_end_call", "End call")
      : copy("rt_hero_voice_cta", "Talk to Hannah (Voice AI)");

  const statusEl = document.querySelector(".hero-chat__voice-status");
  if (statusEl) {
    statusEl.textContent = statusText;
  }
}

function copy(key: string, fallback: string): string {
  return siteCopy[key]?.trim() || fallback;
}

async function loadSiteCopy(): Promise<void> {
  try {
    const res = await fetch(`/api/site_copy?_=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return;
    siteCopy = (await res.json()) as SiteCopy;
    if (siteCopy.rt_site_title) document.title = siteCopy.rt_site_title;
  } catch {
    /* keep fallbacks */
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const ICON_VOICE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="23"/><line x1="8" x2="16" y1="23" y2="23"/></svg>`;

const SEND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>`;

const SEARCH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;

function renderChat(): string {
  return chatMessages
    .map((m) => `<div class="chat__msg chat__msg--${m.role}">${escapeHtml(m.content)}</div>`)
    .join("");
}

function renderHeroVoiceButton(live: boolean, connecting: boolean): string {
  const voiceLabel = connecting
    ? copy("rt_status_connecting", "Connecting…")
    : live
      ? copy("rt_end_call", "End call")
      : copy("rt_hero_voice_cta", "Talk to Hannah (Voice AI)");

  const btnClass = live ? "hero-voice-btn hero-voice-btn--live" : "hero-voice-btn";
  const disabled = connecting;

  return `
    <div class="hero-chat__voice-row">
      <button
        type="button"
        id="btn-hero-voice"
        class="${btnClass}"
        aria-label="${escapeHtml(live ? copy("rt_end_call", "End call") : copy("rt_hero_voice_aria", "Start voice conversation with Hannah"))}"
        ${disabled ? "disabled" : ""}
      >
        <span class="hero-voice-btn__icon">${ICON_VOICE}</span>
        <span class="hero-voice-btn__label">${escapeHtml(voiceLabel)}</span>
      </button>
      ${live && statusText ? `<p class="hero-chat__voice-status" role="status">${escapeHtml(statusText)}</p>` : ""}
    </div>`;
}

function renderHeroChat(live: boolean, connecting: boolean): string {
  const chatActive = chatMessages.length > 0 || chatBusy;
  const assistantName = escapeHtml(copy("rt_assistant_name", "Hannah"));
  const disabled = live || connecting || chatBusy;

  return `
    <div class="hero-chat${chatActive ? " hero-chat--active" : ""}" data-hero-chat>
      <div class="hero-chat__shell">
        ${chatActive ? `
        <div class="hero-chat__thread" id="hero-chat-thread" role="log" aria-live="polite" aria-relevant="additions">
          ${renderChat()}
          ${chatBusy ? `<p class="hero-chat__typing" role="status">${assistantName} is thinking…</p>` : ""}
        </div>` : ""}
        <form id="hero-chat-form" class="hero-chat__composer help-search" role="form" aria-label="Chat with ${assistantName}">
          <input
            id="help-search-input"
            class="help-search__input"
            type="text"
            name="message"
            placeholder="${escapeHtml(copy("rt_search_placeholder", "Ask a question about Hammer…"))}"
            value="${escapeHtml(chatInputDraft)}"
            autocomplete="off"
            ${disabled ? "disabled" : ""}
          />
          <button class="help-search__btn help-search__btn--send" type="submit" aria-label="Send message" ${disabled ? "disabled" : ""}>
            ${chatActive || chatInputDraft.trim() ? SEND_ICON : SEARCH_ICON}
          </button>
        </form>
        <footer class="hero-chat__footer">
          ${renderHeroVoiceButton(live, connecting)}
          ${!live && !connecting ? `<p class="hero-chat__voice-hint">${escapeHtml(copy("rt_hero_voice_hint", "Speak with Hannah for instant, wiki-grounded help."))}</p>` : ""}
        </footer>
      </div>
    </div>`;
}

function renderTranscript(): string {
  if (!transcript.length) return `<p class="muted">Listening…</p>`;
  return transcript
    .map(
      (t) =>
        `<div class="transcript__line"><span class="transcript__role">${t.role === "user" ? "You" : "Hannah"}</span>${escapeHtml(t.text)}</div>`,
    )
    .join("");
}

function renderSessionPanel(live: boolean): string {
  if (!live) return "";

  return `
    <section class="help-session" aria-label="Live with Hannah">
      ${statusText ? `<p class="help-session__status" role="status">${escapeHtml(statusText)}</p>` : ""}
      <div class="hero-glass">
        <div class="hero-glass__head">${escapeHtml(copy("rt_transcript_title", "Live conversation"))}</div>
        <div class="hero-glass__body" id="transcript-body">${renderTranscript()}</div>
        <div class="hero-glass__foot">
          <button id="btn-end" class="landing-cta landing-cta--end" type="button">${escapeHtml(copy("rt_end_call", "End call"))}</button>
        </div>
      </div>
    </section>`;
}

function renderHelpHeader(phone: string, phoneTel: string): string {
  const phonePrefix = escapeHtml(copy("rt_phone_label", "Call"));
  const phoneDisplay = escapeHtml(phone);
  const phoneAria = escapeHtml(`${copy("rt_phone_label", "Call")} ${phone}`);

  return `
      <header class="help-header">
        <div class="help-header__inner">
          <a class="help-header__brand-link" href="/" aria-label="${escapeHtml(copy("rt_help_center_label", "Help Center"))}">
            <span class="logo-img logo-img--hammer" role="img" aria-label="${escapeHtml(copy("rt_logo_text", "HAMMER"))}"></span>
          </a>
          <a class="help-header__phone" href="tel:+1${phoneTel}" aria-label="${phoneAria}">
            <span class="help-header__phone-prefix">${phonePrefix}</span>
            <span class="help-header__phone-number">${phoneDisplay}</span>
          </a>
        </div>
      </header>`;
}

function render(): void {
  const app = document.getElementById("app");
  if (!app) return;

  const live = uiState === "live";
  const connecting = uiState === "connecting";
  const phone = copy("rt_phone_display", "(512) 883-1336");
  const phoneTel = phone.replace(/\D/g, "");
  const showError = uiState === "error" && !!errorDetail;

  if (document.activeElement?.id === "help-search-input") {
    focusChatInput = true;
  }

  app.innerHTML = `
    <div class="app-shell app-shell--landing">
      ${renderHelpHeader(phone, phoneTel)}

      <main class="landing-main">
        <section class="help-hero" aria-label="Chat with Hannah">
          <header class="help-hero__intro">
            <h1 class="help-hero__title">${escapeHtml(copy("rt_hero_headline_simple", "We're here to help"))}</h1>
            <p class="help-hero__lede">${escapeHtml(copy("rt_hero_chat_hint", "Ask Hannah a question — get wiki-grounded answers right here."))}</p>
          </header>
          ${renderHeroChat(live, connecting)}
          ${showError ? `<p class="help-session__error" role="alert">${escapeHtml(errorDetail)}</p>` : ""}
        </section>

        ${renderSessionPanel(live)}
      </main>

      <footer class="site-footer" role="contentinfo">
        <nav class="site-footer__nav" aria-label="Legal and account">
          <div class="site-footer__legal">
            <a class="site-footer__link" href="https://www.hammertime.com/terms-of-service" target="_blank" rel="noopener noreferrer">${escapeHtml(copy("rt_site_footer_terms", "Terms of Service"))}</a>
            <a class="site-footer__link" href="https://www.hammertime.com/privacy-policy" target="_blank" rel="noopener noreferrer">${escapeHtml(copy("rt_site_footer_privacy", "Privacy Policy"))}</a>
          </div>
          <a class="site-footer__link" href="https://office.hammer-corp.com" target="_blank" rel="noopener noreferrer">${escapeHtml(copy("rt_site_footer_login", "Login"))}</a>
        </nav>
      </footer>
    </div>
  `;

  document.getElementById("hero-chat-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    void sendHeroChat();
  });

  const chatInput = document.getElementById("help-search-input") as HTMLInputElement | null;
  chatInput?.addEventListener("input", () => {
    chatInputDraft = chatInput.value;
  });

  document.getElementById("btn-hero-voice")?.addEventListener("click", () => {
    if (live) void endVoice();
    else if (!connecting) void startVoice();
  });
  document.getElementById("btn-end")?.addEventListener("click", () => void endVoice());

  const transcriptBody = document.getElementById("transcript-body");
  if (transcriptBody) transcriptBody.scrollTop = transcriptBody.scrollHeight;

  const chatThread = document.getElementById("hero-chat-thread");
  if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;

  if (focusChatInput && chatInput) {
    chatInput.focus();
    const end = chatInput.value.length;
    chatInput.setSelectionRange(end, end);
    focusChatInput = false;
  }
}

function voiceOpeningGreeting(): string {
  return copy(
    "rt_hero_voice_greeting",
    "Hi it's Hannah with Hammer — how can I help you today?",
  );
}

async function getConversationToken(): Promise<{ token: string; greeting: string }> {
  const res = await fetch("/api/elevenlabs/token");
  if (!res.ok) throw new Error(await res.text());
  const data = (await res.json()) as {
    token?: string;
    conversation_token?: string;
    voice_greeting?: string;
  };
  const tok = (data.token ?? data.conversation_token ?? "").trim();
  if (!tok) throw new Error("No conversation token returned");
  const greeting = (data.voice_greeting ?? voiceOpeningGreeting()).trim() || voiceOpeningGreeting();
  return { token: tok, greeting };
}

async function startVoice(): Promise<void> {
  if (uiState === "connecting" || uiState === "live") return;
  const callEpoch = ++voiceCallEpoch;
  uiState = "connecting";
  errorDetail = "";
  statusText = copy("rt_status_connecting", "Connecting…");
  transcript = [];
  render();
  try {
    const { token, greeting } = await getConversationToken();
    if (callEpoch !== voiceCallEpoch) return;

    const conv = await Conversation.startSession({
      conversationToken: token,
      connectionType: "webrtc",
      connectionDelay: { android: 0, ios: 0, default: 0 },
      overrides: {
        agent: {
          firstMessage: greeting,
        },
      },
      onConnect: ({ conversationId }) => {
        if (callEpoch !== voiceCallEpoch) {
          void conv.endSession();
          return;
        }
        uiState = "live";
        statusText = copy("rt_status_live", "Live — speak anytime.");
        void fetch("/api/voice/browser-call-start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ call_id: conversationId, conversation_id: conversationId }),
        });
        patchVoiceChrome();
        render();
      },
      onDisconnect: (details) => {
        if (callEpoch !== voiceCallEpoch) return;
        voiceConv = null;
        const detailMsg =
          details && typeof details === "object" && "message" in details
            ? String((details as { message?: string }).message ?? "")
            : typeof details === "string"
              ? details
              : "";
        if (uiState === "connecting") {
          uiState = "error";
          errorDetail = voiceConnectErrorMessage(detailMsg);
        } else if (uiState === "live") {
          uiState = "idle";
          errorDetail = "";
          statusText = copy("rt_status_call_ended", "Call ended.");
        } else {
          uiState = "idle";
          errorDetail = "";
          statusText = "";
        }
        console.warn("ElevenLabs voice disconnected", details);
        render();
      },
      onMessage: (msg) => {
        if (callEpoch !== voiceCallEpoch) return;
        const role = msg.source === "user" ? "user" : "agent";
        const text = (msg.message || "").trim();
        if (!text) return;
        transcript.push({ role, text });
        render();
      },
      onError: (message) => {
        console.error("ElevenLabs session error:", message);
        if (callEpoch !== voiceCallEpoch) return;
        uiState = "error";
        errorDetail = voiceConnectErrorMessage(String(message));
        statusText = "";
        voiceConv = null;
        render();
      },
      onDebug: (info) => {
        console.debug("[EL voice]", info);
      },
    });

    if (callEpoch !== voiceCallEpoch) {
      void conv.endSession();
      return;
    }
    voiceConv = conv;
  } catch (err) {
    console.error(err);
    if (callEpoch !== voiceCallEpoch) return;
    uiState = "error";
    errorDetail = voiceConnectErrorMessage(err instanceof Error ? err.message : String(err));
    statusText = "";
    voiceConv = null;
    render();
  }
}

async function endVoice(): Promise<void> {
  voiceCallEpoch += 1;
  try {
    await voiceConv?.endSession();
  } catch {
    /* ignore */
  }
  voiceConv = null;
  uiState = "idle";
  statusText = "";
  errorDetail = "";
  render();
}

async function sendChatWithText(text: string): Promise<void> {
  if (!text || chatBusy) return;
  chatBusy = true;
  chatMessages.push({ role: "user", content: text });
  chatInputDraft = "";
  focusChatInput = true;
  render();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatMessages }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = (await res.json()) as { reply?: string };
    chatMessages.push({ role: "assistant", content: data.reply || "No response." });
  } catch (err) {
    chatMessages.push({
      role: "assistant",
      content: err instanceof Error ? err.message : "Chat failed.",
    });
  } finally {
    chatBusy = false;
    render();
  }
}

async function sendHeroChat(): Promise<void> {
  const input = document.getElementById("help-search-input") as HTMLInputElement | null;
  const text = input?.value.trim() ?? "";
  if (!text || chatBusy) return;
  chatInputDraft = "";
  await sendChatWithText(text);
}

void loadSiteCopy().then(() => {
  document.title = copy("rt_site_title", "Hammer Support");
  render();
});
