#!/usr/bin/env python3
from pathlib import Path

D = "d" + "iv"

def fix_tags(s: str) -> str:
    s = s.replace("<motion ", f"<{D} ").replace("</motion>", f"</{D}>")
    s = s.replace(f"<{D} class=\"hero-scene__sphere\"></{D}>", f"<{D} class=\"hero-scene__sphere\"></{D}>")
    return s

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
text = p.read_text(encoding="utf-8")

if 'import "./landing-hero.css"' not in text:
    text = text.replace(
        'import "./pplx-theme.css";',
        'import "./pplx-theme.css";\nimport "./landing-hero.css";',
    )

text = text.replace("app-shell--pplx", "app-shell--landing")

start = text.find('        <div class="underlay"')
if start == -1:
    raise SystemExit("underlay start not found")
end = text.find('        <motion class="resolution-strip resolution-strip--vr"')
if end == -1:
    end = text.find('        <div class="resolution-strip resolution-strip--vr"')
if end == -1:
    raise SystemExit("resolution-strip not found")

new = fix_tags(r'''        <motion class="hero-scene" aria-hidden="true">
          <motion class="hero-scene__sky"></motion>
          <motion class="hero-scene__sphere"></motion>
          <motion class="hero-scene__ground"></motion>
          <motion class="hero-scene__device"></motion>
        </motion>

        <motion class="underlay" aria-hidden="true">
          <motion class="underlay__base"></motion>
        </motion>

        <header class="chrome">
          <a class="chrome__brand-link" href="/" aria-label="${escapeHtml(copy("rt_brand_aria", "Hammer"))}">
            <img class="logo-img" src="/hammer-wordmark.png" width="132" height="28" alt="${escapeHtml(copy("rt_logo_text", "HAMMER"))}" />
          </a>
          <nav class="chrome__nav" aria-label="${escapeHtml(copy("rt_nav_aria", "Sections"))}">
            <button type="button" class="chrome__jump chrome__jump--panel${openNavPanel === "how" ? " is-active" : ""}" data-panel="how"
              aria-expanded="${openNavPanel === "how"}" aria-controls="navPanelHow">
              ${escapeHtml(copy("rt_nav_how", "How it works"))}
            </button>
            <button type="button" class="chrome__jump chrome__jump--panel${openNavPanel === "leads" ? " is-active" : ""}" data-panel="leads"
              aria-expanded="${openNavPanel === "leads"}" aria-controls="navPanelLeads">
              ${escapeHtml(copy("rt_nav_leads", "Leads and calls"))}
            </button>
            <button type="button" class="chrome__jump chrome__jump--panel${openNavPanel === "integrations" ? " is-active" : ""}" data-panel="integrations"
              aria-expanded="${openNavPanel === "integrations"}" aria-controls="navPanelIntegrations">
              ${escapeHtml(copy("rt_nav_integrations", "Integrations"))}
            </button>
          </nav>
          <button type="button" class="chrome__jump chrome__jump--solid chrome__get-agent" id="navCta" ${busy ? "disabled" : ""}>${escapeHtml(copy("rt_nav_cta", "Try now"))}</button>
        </header>

        <motion class="nav-panel-layer ${openNavPanel ? "is-open" : ""}" ${openNavPanel ? "" : "hidden"} aria-hidden="${openNavPanel ? "false" : "true"}">
          <motion class="nav-panel-backdrop" data-action="close" aria-hidden="true"></motion>
          <section class="nav-panel" role="dialog" aria-modal="false" aria-label="${escapeHtml(copy("rt_nav_panel_aria", "Navigation panel"))}">
            <span class="nav-panel__accent" aria-hidden="true"></span>
            <header class="nav-panel__head">
              <motion class="nav-panel__title">
                <span class="nav-panel__kicker">
                  <span class="nav-panel__kicker-dot" aria-hidden="true"></span>
                  ${escapeHtml(copy("rt_nav_panel_kicker", "Quick tour"))}
                </span>
                <span class="nav-panel__h">
                  ${
                    openNavPanel === "how"
                      ? escapeHtml(copy("rt_nav_how", "How it works"))
                      : openNavPanel === "leads"
                        ? escapeHtml(copy("rt_nav_leads", "Leads and calls"))
                        : escapeHtml(copy("rt_nav_integrations", "Integrations"))
                  }
                </span>
              </motion>
              <button type="button" class="nav-panel__close" data-action="close" aria-label="${escapeHtml(copy("rt_nav_close_aria", "Close"))}">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M1 1l12 12M13 1L1 13" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>
              </button>
            </header>
            <motion class="nav-panel__body">
              <motion id="navPanelHow" class="nav-panel__section ${openNavPanel === "how" ? "is-active" : ""}">
                <p class="nav-panel__lead">${escapeHtml(copy("rt_nav_panel_how_1", "A prospect reaches out. Hammer replies instantly and keeps following up until they book."))}</p>
                <motion class="nav-panel__features">
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">01</span><p>${escapeHtml(copy("rt_nav_panel_how_2", "Seconds-to-first-response, 24/7."))}</p></article>
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">02</span><p>${escapeHtml(copy("rt_nav_panel_how_3", "Human tone — short, natural, dealership language."))}</p></article>
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">03</span><p>${escapeHtml(copy("rt_nav_panel_how_4", "Hands off to your team with context."))}</p></article>
                </motion>
              </motion>
              <motion id="navPanelLeads" class="nav-panel__section ${openNavPanel === "leads" ? "is-active" : ""}">
                <p class="nav-panel__lead">${escapeHtml(copy("rt_nav_panel_leads_1", "Lead comes in after hours. Hammer engages, qualifies, and moves them toward an appointment."))}</p>
                <motion class="nav-panel__features">
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">01</span><p>${escapeHtml(copy("rt_nav_panel_leads_2", "Handles common objections quickly."))}</p></article>
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">02</span><p>${escapeHtml(copy("rt_nav_panel_leads_3", "One question at a time — no long scripts."))}</p></article>
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">03</span><p>${escapeHtml(copy("rt_nav_panel_leads_4", "Sets next steps clearly."))}</p></article>
                </motion>
              </motion>
              <motion id="navPanelIntegrations" class="nav-panel__section ${openNavPanel === "integrations" ? "is-active" : ""}">
                <p class="nav-panel__lead">${escapeHtml(copy("rt_nav_panel_integrations_1", "Keep your existing CRM process. Hammer works with your workflow — your team stays in their system."))}</p>
                <motion class="nav-panel__features nav-panel__features--duo">
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">01</span><p>${escapeHtml(copy("rt_nav_panel_integrations_2", "No new UI for salespeople to learn."))}</p></article>
                  <article class="nav-panel__feature"><span class="nav-panel__feature-n">02</span><p>${escapeHtml(copy("rt_nav_panel_integrations_3", "Richer notes and context in the handoff."))}</p></article>
                </motion>
              </motion>
            </motion>
            <footer class="nav-panel__foot">
              <div class="nav-panel__foot-card">
                <div class="nav-panel__foot-signal" aria-hidden="true">
                  <span class="nav-panel__foot-orbit"></span>
                  <span class="nav-panel__foot-core"></span>
                </div>
                <div class="nav-panel__foot-text">
                  <span class="nav-panel__foot-eyebrow">${escapeHtml(copy("rt_nav_panel_foot_tag", "Live preview"))}</span>
                  <p class="nav-panel__foot-hint">${escapeHtml(copy("rt_nav_panel_foot", "Tap this orb or the session below—ask about leads, follow-up, or integrations and hear each reply live."))}</p>
                </div>
              </div>
            </footer>
          </section>
        </motion>

        <main class="landing-main">
          <section class="landing-hero" aria-label="${escapeHtml(copy("rt_hero_section_aria", "Hammer Drive live voice demo"))}">
            <h1 class="landing-hero__title">${heroTitlePrimaryHtml("rt_hero_title_primary", "Agentic AI is here.")}</h1>
            <p class="landing-hero__sub">${escapeHtml(copy("rt_hero_sub_pills", "You're the buyer. Push back. See if Hannah closes the sale."))}</p>

            <motion class="hero-glass">
              <motion class="hero-glass__input-row">
                <motion class="hero-glass__input" role="presentation">
                  <span class="hero-glass__input-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 8h10M7 12h6"/></svg>
                  </span>
                  <span class="hero-glass__input-text">${escapeHtml(copy("rt_glass_input", "A lead asks if the 2022 F-150 is still available — Hammer answers in seconds"))}</span>
                  <span class="hero-glass__input-cursor" aria-hidden="true"></span>
                  <button type="button" class="hero-glass__input-plus" tabindex="-1" aria-hidden="true">+</button>
                </motion>
              </motion>
              <motion class="hero-glass__toolbar" role="toolbar" aria-label="${escapeHtml(copy("rt_glass_toolbar_aria", "Demo channels"))}">
                <button type="button" class="hero-glass__tool is-active" data-glass-tab="sms" aria-pressed="true" title="${escapeHtml(copy("rt_glass_tool_sms", "Text follow-up"))}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
                </button>
                <button type="button" class="hero-glass__tool" data-glass-tab="leads" aria-pressed="false" title="${escapeHtml(copy("rt_glass_tool_leads", "Leads"))}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                </button>
                <button type="button" class="hero-glass__tool" data-glass-tab="crm" aria-pressed="false" title="${escapeHtml(copy("rt_glass_tool_crm", "CRM handoff"))}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                </button>
                <button type="button" class="hero-glass__tool" data-glass-tab="inventory" aria-pressed="false" title="${escapeHtml(copy("rt_glass_tool_inventory", "Inventory"))}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
                </button>
                <button type="button" class="hero-glass__tool" data-glass-tab="attach" aria-pressed="false" title="${escapeHtml(copy("rt_glass_tool_attach", "Trade-in"))}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                </button>
                <span class="hero-glass__toolbar-divider" aria-hidden="true"></span>
                <button type="button" class="hero-glass__voice${live ? " is-live" : ""}${connecting ? " is-connecting" : ""}" id="callBtn"${connecting ? " disabled" : ""}
                  aria-label="${connecting ? escapeHtml(copy("rt_call_aria_connecting", "Connecting")) : live ? escapeHtml(copy("rt_call_aria_end", "End call")) : escapeHtml(copy("rt_call_aria_start", "Start voice call"))}">
                  <span class="hero-glass__voice-bar" aria-hidden="true"></span>
                  <span class="hero-glass__voice-bar" aria-hidden="true"></span>
                  <span class="hero-glass__voice-bar" aria-hidden="true"></span>
                  <span class="hero-glass__voice-bar" aria-hidden="true"></span>
                  <span class="hero-glass__voice-bar" aria-hidden="true"></span>
                </button>
              </motion>

              <motion class="hero-glass__sms" id="glassSms">
                <span class="hero-glass__sms-label">${escapeHtml(copy("rt_sms_block_sub", "What prospects see on their phone"))}</span>
                <motion class="conversation-preview conversation-preview--standalone">
                  <motion class="conversation-preview__thread" role="list">
                    <article class="chat-msg from-buyer" role="listitem">
                      <span class="chat-msg__who">Prospect</span>
                      <p>${escapeHtml(copy("rt_preview_msg_1", "Hey, is the 2022 F-150 XLT still on the lot?"))}</p>
                    </article>
                    <article class="chat-msg from-agent" role="listitem">
                      <span class="chat-msg__who">Hammer</span>
                      <p>${escapeHtml(copy("rt_preview_msg_2", "Hey! Yes, it's still here. Are you looking to finance, or did you have a trade-in in mind?"))}</p>
                    </article>
                    <article class="chat-msg from-buyer" role="listitem">
                      <span class="chat-msg__who">Prospect</span>
                      <p>${escapeHtml(copy("rt_preview_msg_3", "I'd want to trade in my 2019 Ram 1500."))}</p>
                    </article>
                    <article class="chat-msg from-agent" role="listitem">
                      <span class="chat-msg__who">Hammer</span>
                      <p>${escapeHtml(copy("rt_preview_msg_4", "Got it. We handle trade-ins right here. Want to swing by today at 5pm or Saturday at 11am?"))}</p>
                    </article>
                  </motion>
                </motion>
              </motion>

              <motion class="hero-glass__panel${busy ? " is-open" : ""}" id="glassVoice">
                <motion class="hero-glass__panel-inner">
                  <motion class="hero-glass__live voice-body voice-body--resolution">
                    <p class="voice-hint">${escapeHtml(copy("rt_voice_hint", "Tap the mic and ask about leads, follow-up, Facebook AIA, or integrations."))}</p>
                    <motion class="call-row">
                      <motion class="call-hero-zone" aria-hidden="true"></motion>
                      <motion class="call-stack">
                        <motion class="btn-call-shell${connecting ? " is-connecting" : ""}${live ? " is-live" : ""}">
                          <span class="btn-call-orbit btn-call-orbit--a" aria-hidden="true"></span>
                          <span class="btn-call-orbit btn-call-orbit--b" aria-hidden="true"></span>
                          <button type="button" class="btn-call${live ? " end is-live" : ""}${connecting ? " is-connecting" : ""}" id="callBtnInner"${connecting ? " disabled" : ""}
                            aria-label="${connecting ? escapeHtml(copy("rt_call_aria_connecting", "Connecting")) : live ? escapeHtml(copy("rt_call_aria_end", "End call")) : escapeHtml(copy("rt_call_aria_start", "Start voice call"))}">
                            ${connecting ? iconCallConnecting : live ? iconCallLive : iconCallIdle}
                          </button>
                        </motion>
                        ${!live ? `
                        <p class="call-prompt${connecting ? " is-connecting" : ""}" aria-live="polite">
                          <span class="call-prompt__main">${connecting ? escapeHtml(copy("rt_call_prompt_connecting", "Connecting")) : escapeHtml(copy("rt_call_prompt_tap", "Tap to talk"))}</span>
                          <span class="call-prompt__sub">${connecting ? escapeHtml(copy("rt_call_prompt_connecting_sub", "Securing your voice session")) : escapeHtml(copy("rt_call_prompt_mic_sub", "Allow microphone when prompted"))}</span>
                        </p>` : ""}
                        <motion class="listen-indicator${live ? " is-visible" : ""}" ${live ? 'role="status"' : 'aria-hidden="true"'}>
                          <span class="listen-indicator-label">${escapeHtml(copy("rt_listen_label", "Listening"))}</span>
                          <p class="listen-indicator-sub">${escapeHtml(copy("rt_listen_sub", ""))}</p>
                        </motion>
                      </motion>
                    </motion>
                  </motion>
                </motion>
              </motion>
            </motion>

            <button type="button" class="landing-cta" id="footerPrimary" ${busy ? "disabled" : ""}>
              ${live ? escapeHtml(copy("rt_footer_primary_live", "On call")) : escapeHtml(copy("rt_landing_cta", "Try live demo"))}
            </button>
            <p class="landing-hero__status ${uiState === "error" ? "err" : ""}" id="status" aria-live="polite">${escapeHtml(errorDetail || statusText)}</p>
          </section>
        </main>

''')

text = text[:start] + new + text[end:]

# Remove resolution strip block
for marker in (
    '        <div class="resolution-strip resolution-strip--vr"',
    '        <motion class="resolution-strip resolution-strip--vr"',
):
    strip_start = text.find(marker)
    if strip_start != -1:
        # find closing before `      </div>`;`
        needle = "        </div>\n      </motion>`;"
        needle2 = "        </div>\n      </div>`;"
        idx = text.find(needle, strip_start)
        if idx == -1:
            idx = text.find(needle2, strip_start)
        if idx != -1:
            text = text[:strip_start] + text[idx + len(needle2 if needle2 in text[idx:idx+40] else needle):]
        break

if '#callBtnInner' not in text:
    text = text.replace(
        '    root.querySelector("#callBtn")?.addEventListener("click", onCallClick);',
        '    root.querySelector("#callBtn")?.addEventListener("click", onCallClick);\n'
        '    root.querySelector("#callBtnInner")?.addEventListener("click", onCallClick);',
    )

text = text.replace(
    '    root.querySelector("#resTry")?.addEventListener("click", startIfIdle);\n',
    '',
)

p.write_text(text, encoding="utf-8")
print("patched ok")
