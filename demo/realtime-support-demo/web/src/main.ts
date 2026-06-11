import { Conversation } from "@elevenlabs/client";
import { startSearchPlaceholderTyper, type SearchPlaceholderTyperHandle } from "./search-placeholder-typer";
import "./support.css";

type SiteCopy = Record<string, string>;
type UiState = "idle" | "connecting" | "live" | "error";
type SupportMode = "chat" | "voice";
type TicketSubmitState = "idle" | "submitting" | "success" | "error";
type SupportNoticeTone = "resolved" | "followup" | "error";

type TicketFormData = {
  dealership: string;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  issue_category: string;
  message: string;
};

// Mirrors the category options on https://www.hammertime.com/help
const TICKET_CATEGORIES: readonly string[] = [
  "AI responses",
  "Facebook / TikTok Advertising",
  "CRM / Lead Integrations",
  "Billing",
  "Inventory",
  "Help with logging in",
  "Cancellation Request",
  "Sales / Demo",
  "Other",
];

type ChatResponse = {
  reply?: string;
  session_id?: string;
  ticket_created?: boolean;
  resolved?: boolean;
  escalated?: boolean;
  hubspot_ticket_id?: string;
};

let siteCopy: SiteCopy = {};
let uiState: UiState = "idle";
let supportMode: SupportMode = "chat";
let statusText = "";
let errorDetail = "";
let transcript: { role: "user" | "agent"; text: string }[] = [];
// Follow the live conversation: stay pinned to the newest line unless the user
// scrolls up to read history, in which case we preserve their position.
let transcriptStickToBottom = true;
let transcriptSavedScrollTop = 0;
let chatMessages: { role: "user" | "assistant"; content: string }[] = [];
let chatSessionId = "";
let chatBusy = false;
let chatInputDraft = "";
let focusChatInput = false;
let voiceConv: Conversation | null = null;
let voiceCallEpoch = 0;
let searchPlaceholderTyper: SearchPlaceholderTyperHandle | null = null;
let showTicketForm = false;
let ticketSubmitState: TicketSubmitState = "idle";
let ticketSubmitMessage = "";
let manualTicketSessionId = "";
let supportNotice: { tone: SupportNoticeTone; text: string } | null = null;
let ticketForm: TicketFormData = {
  dealership: "",
  first_name: "",
  last_name: "",
  email: "",
  phone: "",
  issue_category: "",
  message: "",
};

function voiceConnectErrorMessage(raw: string): string {
  const msg = raw.trim();
  if (!msg) {
    const onVercel = /vercel\.app/i.test(location.hostname);
    return copy(
      "rt_error_voice_connect",
      onVercel
        ? "Voice disconnected before Hannah could speak. Confirm ElevenLabs Custom LLM URL is https://hammer-support-ai-final.vercel.app/api/elevenlabs/llm and retry."
        : "Voice disconnected before the call started. Check that ngrok is running (port 8781) and your ElevenLabs agent Custom LLM URL matches it.",
    );
  }
  if (/custom_llm|failed to generate response/i.test(msg)) {
    return copy(
      "rt_error_custom_llm",
      "Voice could not reach the support AI server. In ElevenLabs, set Custom LLM URL to https://hammer-support-ai-final.vercel.app/api/elevenlabs/llm",
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

// Escape plain text, then turn links into clickable anchors. Handles
// markdown-style links the model emits ("[Password Reset](https://…)"),
// bare URLs, www. links, and email addresses. Matched tokens are escaped for
// safe use in href attributes; non-link text is escaped as usual, so this
// stays XSS-safe.
const LINKIFY_RE =
  /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"]+)|(\bwww\.[^\s<>"]+)|(\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|ca|co|us|dev|app|ai)\b(?:\/[^\s<>"]*)?)|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})/g;

// The voice agent reads web addresses aloud ("www dot hammer dash corp dot com
// slash session slash new"), so the transcript text contains no real URL for
// linkify() to pick up. Decode spoken-form addresses back into markdown links
// (clean label + https href) so they render as clickable anchors on screen,
// while the audio keeps the natural spoken wording.
const SPOKEN_URL_RE = /\b[a-z0-9]+(?:\s+(?:dot|dash|hyphen|slash|underscore)\s+[a-z0-9]+)+\b/gi;

const SPOKEN_TLDS = new Set(["com", "net", "org", "io", "co", "ca", "us", "dev", "app", "ai"]);

function decodeSpokenUrls(text: string): string {
  return text.replace(SPOKEN_URL_RE, (match) => {
    const tokens = match.trim().split(/\s+/).map((t) => t.toLowerCase());
    // Only treat it as a web address if it contains "dot <tld>".
    const hasTld = tokens.some((t, i) => t === "dot" && SPOKEN_TLDS.has(tokens[i + 1] ?? ""));
    if (!hasTld) return match;
    let url = "";
    for (const t of tokens) {
      if (t === "dot") url += ".";
      else if (t === "dash" || t === "hyphen") url += "-";
      else if (t === "slash") url += "/";
      else if (t === "underscore") url += "_";
      else url += t;
    }
    return `[${url}](https://${url})`;
  });
}

function linkify(raw: string): string {
  let out = "";
  let last = 0;
  let m: RegExpExecArray | null;
  LINKIFY_RE.lastIndex = 0;
  while ((m = LINKIFY_RE.exec(raw)) !== null) {
    out += escapeHtml(raw.slice(last, m.index));
    const token = m[0];
    if (m[2]) {
      // Markdown link: show the label, link to the URL.
      out += `<a href="${escapeHtml(m[2])}" target="_blank" rel="noopener noreferrer">${escapeHtml(m[1])}</a>`;
    } else if (m[6]) {
      const email = escapeHtml(token);
      out += `<a href="mailto:${email}">${email}</a>`;
    } else {
      // Trim trailing punctuation that is unlikely to be part of the URL.
      const trimmed = token.replace(/[.,;:!?)\]'"]+$/, "");
      const trailing = token.slice(trimmed.length);
      const safe = escapeHtml(trimmed);
      const href = m[3] ? safe : `https://${safe}`;
      out += `<a href="${href}" target="_blank" rel="noopener noreferrer">${safe}</a>`;
      out += escapeHtml(trailing);
    }
    last = m.index + token.length;
  }
  out += escapeHtml(raw.slice(last));
  return out;
}

const ICON_VOICE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="23"/><line x1="8" x2="16" y1="23" y2="23"/></svg>`;

const ICON_CHAT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"/></svg>`;

const SEND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>`;

const SEARCH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;

function renderChat(): string {
  return chatMessages
    .map((m) => `<div class="chat__msg chat__msg--${m.role}">${linkify(m.content)}</div>`)
    .join("");
}

function renderSupportNotice(): string {
  if (!supportNotice) return "";
  return `<p class="support-notice support-notice--${supportNotice.tone}" role="status">${escapeHtml(supportNotice.text)}</p>`;
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

function renderModeSwitch(activeMode: SupportMode, live: boolean, connecting: boolean): string {
  const lockToVoice = live || connecting;
  const option = (mode: SupportMode, icon: string, title: string, sub: string): string => {
    const isActive = activeMode === mode;
    const isLocked = lockToVoice && mode === "chat";
    return `
      <button
        type="button"
        role="tab"
        class="mode-switch__opt${isActive ? " is-active" : ""}"
        data-mode="${mode}"
        aria-selected="${isActive}"
        ${isLocked ? "disabled" : ""}
      >
        <span class="mode-switch__icon">${icon}</span>
        <span class="mode-switch__text">
          <span class="mode-switch__title">${escapeHtml(title)}</span>
          <span class="mode-switch__sub">${escapeHtml(sub)}</span>
        </span>
      </button>`;
  };

  return `
    <div class="mode-switch" role="tablist" aria-label="${escapeHtml(copy("rt_mode_switch_label", "Choose how to reach Hannah"))}">
      ${option("chat", ICON_CHAT, copy("rt_mode_chat_title", "Chat"), copy("rt_mode_chat_sub", "Type your question"))}
      ${option("voice", ICON_VOICE, copy("rt_mode_voice_title", "Voice"), copy("rt_mode_voice_sub", "Talk it through"))}
    </div>`;
}

function renderChatPanel(live: boolean, connecting: boolean): string {
  const chatActive = chatMessages.length > 0 || chatBusy;
  const assistantName = escapeHtml(copy("rt_assistant_name", "Hannah"));
  const disabled = live || connecting || chatBusy;

  return `
    <div class="hero-chat__panel" role="tabpanel">
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
    </div>`;
}

function renderVoicePanel(live: boolean, connecting: boolean): string {
  const headline = live
    ? copy("rt_voice_panel_live", "You're connected — speak anytime.")
    : connecting
      ? copy("rt_status_connecting", "Connecting…")
      : copy("rt_voice_panel_idle", "Start a live voice call with Hannah.");

  return `
    <div class="hero-chat__panel hero-chat__panel--voice" role="tabpanel">
      <p class="voice-panel__headline">${escapeHtml(headline)}</p>
      ${renderHeroVoiceButton(live, connecting)}
      ${!live && !connecting ? `<p class="hero-chat__voice-hint">${escapeHtml(copy("rt_hero_voice_hint", "Talk to Hannah, Hammer's support AI."))}</p>` : ""}
    </div>`;
}

function renderHeroChat(live: boolean, connecting: boolean): string {
  const activeMode: SupportMode = live || connecting ? "voice" : supportMode;
  const chatActive = activeMode === "chat" && (chatMessages.length > 0 || chatBusy);

  return `
    <div class="hero-chat${chatActive ? " hero-chat--active" : ""}" data-hero-chat>
      <div class="hero-chat__shell">
        ${renderModeSwitch(activeMode, live, connecting)}
        ${activeMode === "chat" ? renderChatPanel(live, connecting) : renderVoicePanel(live, connecting)}
      </div>
    </div>`;
}

function getManualTicketSessionId(): string {
  if (!manualTicketSessionId) {
    manualTicketSessionId = "manual_" + Math.random().toString(36).substring(2, 11) + "_" + Date.now();
  }
  return manualTicketSessionId;
}

function ticketField(name: keyof TicketFormData, label: string, attrs = ""): string {
  return `
    <label class="ticket-form__field">
      <span>${escapeHtml(label)}</span>
      <input
        name="${name}"
        value="${escapeHtml(ticketForm[name])}"
        ${attrs}
      />
    </label>`;
}

function renderFallbackTicket(): string {
  const opened = showTicketForm || ticketSubmitState === "success" || ticketSubmitState === "error";
  return `
    <section class="support-fallback" aria-label="${escapeHtml(copy("rt_ticket_fallback_label", "Support ticket fallback"))}">
      <div class="support-fallback__copy">
        <p class="support-fallback__eyebrow">${escapeHtml(copy("rt_ticket_fallback_eyebrow", "Prefer a form?"))}</p>
        <h2>${escapeHtml(copy("rt_ticket_fallback_title", "Submit a ticket instead"))}</h2>
        <p>${escapeHtml(copy("rt_ticket_fallback_desc", "Hannah is fastest, but you can leave the details and our support team will follow up."))}</p>
      </div>
      <button class="landing-cta landing-cta--secondary" id="btn-ticket-toggle" type="button" aria-expanded="${opened}">
        ${escapeHtml(opened ? copy("rt_ticket_toggle_hide", "Hide ticket form") : copy("rt_ticket_toggle_show", "Open ticket form"))}
      </button>
      ${
        opened
          ? `
        <form class="ticket-form" id="ticket-form">
          <div class="ticket-form__grid">
            ${ticketField("dealership", copy("rt_ticket_dealership", "Dealership name"), "required autocomplete=\"organization\"")}
            ${ticketField("first_name", copy("rt_ticket_first_name", "First name"), "required autocomplete=\"given-name\"")}
            ${ticketField("last_name", copy("rt_ticket_last_name", "Last name"), "required autocomplete=\"family-name\"")}
            ${ticketField("email", copy("rt_ticket_email", "Hammer login email"), "required type=\"email\" autocomplete=\"email\"")}
            ${ticketField("phone", copy("rt_ticket_phone", "Mobile number"), "required type=\"tel\" autocomplete=\"tel\"")}
          </div>
          <label class="ticket-form__field ticket-form__field--full">
            <span>${escapeHtml(copy("rt_ticket_category", "Category"))}</span>
            <select name="issue_category" required>
              <option value="" ${ticketForm.issue_category ? "" : "selected"} disabled>${escapeHtml(copy("rt_ticket_category_placeholder", "Select a category"))}</option>
              ${TICKET_CATEGORIES.map(
                (cat) =>
                  `<option value="${escapeHtml(cat)}" ${ticketForm.issue_category === cat ? "selected" : ""}>${escapeHtml(cat)}</option>`,
              ).join("")}
            </select>
          </label>
          <label class="ticket-form__field ticket-form__field--full">
            <span>${escapeHtml(copy("rt_ticket_message", "What do you need help with?"))}</span>
            <textarea name="message" required rows="4">${escapeHtml(ticketForm.message)}</textarea>
          </label>
          ${
            ticketSubmitMessage
              ? `<p class="ticket-form__status ticket-form__status--${ticketSubmitState}" role="status">${escapeHtml(ticketSubmitMessage)}</p>`
              : ""
          }
          <button class="landing-cta" type="submit" ${ticketSubmitState === "submitting" ? "disabled" : ""}>
            ${escapeHtml(ticketSubmitState === "submitting" ? copy("rt_ticket_submitting", "Submitting…") : copy("rt_ticket_submit", "Submit ticket"))}
          </button>
        </form>`
          : ""
      }
    </section>`;
}

function renderTranscript(): string {
  if (!transcript.length) return `<p class="muted">Listening…</p>`;
  const lastIndex = transcript.length - 1;
  return transcript
    .map((t, i) => {
      const roleClass = t.role === "user" ? "transcript__line--user" : "transcript__line--agent";
      const latestClass = i === lastIndex ? " transcript__line--latest" : "";
      const label = t.role === "user" ? "You" : "Hannah";
      return `<div class="transcript__line ${roleClass}${latestClass}"><span class="transcript__role">${label}</span>${linkify(decodeSpokenUrls(t.text))}</div>`;
    })
    .join("");
}

function renderSessionPanel(live: boolean): string {
  if (!live) return "";

  return `
    <section class="help-session" aria-label="Live with Hannah">
      ${statusText ? `<p class="help-session__status" role="status">${escapeHtml(statusText)}</p>` : ""}
      <div class="hero-glass">
        <div class="hero-glass__head"><span class="hero-glass__live-dot" aria-hidden="true"></span>${escapeHtml(copy("rt_transcript_title", "Live conversation"))}</div>
        <div class="hero-glass__body" id="transcript-body" role="log" aria-live="polite" aria-relevant="additions text" tabindex="0">${renderTranscript()}</div>
        <div class="hero-glass__foot">
          <button id="btn-end" class="landing-cta landing-cta--end" type="button">${escapeHtml(copy("rt_end_call", "End call"))}</button>
        </div>
      </div>
    </section>`;
}

function renderHelpHeader(): string {
  return `
      <header class="help-header">
        <div class="help-header__inner">
          <a class="help-header__brand-link" href="/" aria-label="${escapeHtml(copy("rt_header_brand_aria", "Hammer Support"))}">
            <span class="help-header__brand">
              <span class="logo-img logo-img--hammer" role="img" aria-label="${escapeHtml(copy("rt_logo_text", "HAMMER"))}"></span>
              <span class="help-header__support-label">${escapeHtml(copy("rt_header_support_label", "Support"))}</span>
            </span>
          </a>
        </div>
      </header>`;
}

function render(): void {
  const app = document.getElementById("app");
  if (!app) return;

  const live = uiState === "live";
  const connecting = uiState === "connecting";
  const showError = uiState === "error" && !!errorDetail;

  if (document.activeElement?.id === "help-search-input") {
    focusChatInput = true;
  }

  app.innerHTML = `
    <div class="app-shell app-shell--landing${live ? " app-shell--call" : ""}">
      ${renderHelpHeader()}

      <main class="landing-main">
        <section class="help-hero" aria-label="Chat with Hannah">
          <header class="help-hero__intro">
            <p class="help-hero__eyebrow">${escapeHtml(copy("rt_hero_eyebrow", "Current customer support"))}</p>
            <h1 class="help-hero__title">${escapeHtml(copy("rt_hero_headline_simple", "Get help with your Hammer account"))}</h1>
            <p class="help-hero__lede">${escapeHtml(copy("rt_hero_chat_hint", "Ask Hannah about login issues, lead delivery, billing, integrations, or product setup. If she cannot finish it, your ticket goes to Hammer support."))}</p>
          </header>
          ${renderHeroChat(live, connecting)}
          ${renderSupportNotice()}
          ${showError ? `<p class="help-session__error" role="alert">${escapeHtml(errorDetail)}</p>` : ""}
        </section>

        ${renderSessionPanel(live)}
        ${renderFallbackTicket()}
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
  document.querySelectorAll<HTMLButtonElement>("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode as SupportMode | undefined;
      if (!mode || mode === supportMode || live || connecting) return;
      supportMode = mode;
      if (mode === "chat") focusChatInput = true;
      render();
    });
  });
  document.getElementById("btn-ticket-toggle")?.addEventListener("click", () => {
    showTicketForm = !showTicketForm;
    if (ticketSubmitState === "success" || ticketSubmitState === "error") {
      ticketSubmitState = "idle";
      ticketSubmitMessage = "";
    }
    render();
  });
  document.getElementById("ticket-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    void submitTicketForm();
  });
  document
    .querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      "#ticket-form input, #ticket-form textarea, #ticket-form select",
    )
    .forEach((field) => {
      const sync = () => {
        const name = field.name as keyof TicketFormData;
        ticketForm = { ...ticketForm, [name]: field.value };
        if (ticketSubmitState === "error") {
          ticketSubmitState = "idle";
          ticketSubmitMessage = "";
        }
      };
      field.addEventListener("input", sync);
      field.addEventListener("change", sync);
    });

  const transcriptBody = document.getElementById("transcript-body");
  if (transcriptBody) {
    // The DOM is rebuilt on every render, so restore the right scroll position:
    // pin to the newest line when the user is following along, otherwise keep
    // them where they scrolled back to.
    if (transcriptStickToBottom) {
      transcriptBody.scrollTop = transcriptBody.scrollHeight;
    } else {
      transcriptBody.scrollTop = transcriptSavedScrollTop;
    }
    transcriptBody.addEventListener("scroll", () => {
      const distanceFromBottom =
        transcriptBody.scrollHeight - transcriptBody.scrollTop - transcriptBody.clientHeight;
      transcriptStickToBottom = distanceFromBottom <= 32;
      transcriptSavedScrollTop = transcriptBody.scrollTop;
    });
  }

  const chatThread = document.getElementById("hero-chat-thread");
  if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;

  if (focusChatInput && chatInput) {
    chatInput.focus();
    const end = chatInput.value.length;
    chatInput.setSelectionRange(end, end);
    focusChatInput = false;
  }

  searchPlaceholderTyper?.stop();
  searchPlaceholderTyper = null;
  const animateSearchPlaceholder =
    !live && !connecting && !chatBusy && !chatInputDraft && chatMessages.length === 0;
  if (chatInput && animateSearchPlaceholder) {
    searchPlaceholderTyper = startSearchPlaceholderTyper(chatInput, {
      idlePlaceholder: copy("rt_search_placeholder", "Ask a question about Hammer…"),
    });
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
  transcriptStickToBottom = true;
  transcriptSavedScrollTop = 0;
  render();
  try {
    const { token } = await getConversationToken();
    if (callEpoch !== voiceCallEpoch) return;

    const conv = await Conversation.startSession({
      conversationToken: token,
      connectionType: "webrtc",
      connectionDelay: { android: 0, ios: 0, default: 0 },
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
  supportMode = "chat";
  chatBusy = true;
  chatMessages.push({ role: "user", content: text });
  chatInputDraft = "";
  focusChatInput = true;
  render();

  if (!chatSessionId) {
    chatSessionId = "chat_" + Math.random().toString(36).substring(2, 11) + "_" + Date.now();
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatMessages, session_id: chatSessionId }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = (await res.json()) as ChatResponse;
    if (data.session_id) {
      chatSessionId = data.session_id;
    }
    chatMessages.push({ role: "assistant", content: data.reply || "No response." });
    if (data.ticket_created) {
      supportNotice =
        data.resolved && !data.escalated
          ? {
              tone: "resolved",
              text: copy("rt_ticket_logged_resolved", "This support session has been logged as resolved in HubSpot."),
            }
          : {
              tone: "followup",
              text: copy("rt_ticket_logged_followup", "A support ticket has been logged for Hammer follow-up."),
            };
    }
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

async function submitTicketForm(): Promise<void> {
  if (ticketSubmitState === "submitting") return;
  ticketSubmitState = "submitting";
  ticketSubmitMessage = copy("rt_ticket_submitting", "Submitting…");
  render();

  try {
    const res = await fetch("/api/support/ticket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...ticketForm,
        session_id: getManualTicketSessionId(),
        channel: "manual_form",
        resolved: false,
        issue_category: ticketForm.issue_category || "Other",
      }),
    });
    const raw = await res.text();
    let data: { ok?: boolean; error?: string } = {};
    try {
      data = raw ? (JSON.parse(raw) as { ok?: boolean; error?: string }) : {};
    } catch {
      data = {};
    }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || raw || "Ticket failed.");
    }
    ticketSubmitState = "success";
    ticketSubmitMessage = copy(
      "rt_ticket_success",
      "Thanks — your ticket is logged. A Hammer representative will follow up as soon as possible.",
    );
    supportNotice = {
      tone: "followup",
      text: copy("rt_ticket_logged_followup", "A support ticket has been logged for Hammer follow-up."),
    };
    ticketForm = {
      dealership: "",
      first_name: "",
      last_name: "",
      email: "",
      phone: "",
      issue_category: "",
      message: "",
    };
    manualTicketSessionId = "";
  } catch (err) {
    ticketSubmitState = "error";
    ticketSubmitMessage =
      err instanceof Error
        ? err.message
        : copy("rt_ticket_error", "Ticket submission failed. Please email support@hammertime.com.");
  } finally {
    render();
  }
}

void loadSiteCopy().then(() => {
  document.title = copy("rt_site_title", "Hammer Support");
  render();
});
