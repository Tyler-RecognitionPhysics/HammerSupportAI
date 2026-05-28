#!/usr/bin/env python3
from pathlib import Path

TAG = "d" + "iv"

def el(cls, inner="", close=True):
    o = f"<{TAG} class=\"{cls}\">"
    c = f"</{TAG}>" if close else ""
    return o + inner + c

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
text = p.read_text(encoding="utf-8")

start = text.find('                  <div class="resolution-lane__panel">')
end = text.find(f'                          <{TAG} class="voice-body voice-body--resolution">')
if start == -1 or end == -1:
    raise SystemExit(f"markers not found start={start} end={end}")

D = TAG
new = f"""                  <{D} class="resolution-lane__stack">
                    <section class="resolution-block resolution-block--sms" aria-label="${{escapeHtml(copy("rt_preview_aria", "Example text conversation"))}}">
                      <header class="resolution-block__head">
                        <{D} class="resolution-block__titles">
                          <h2 class="resolution-block__title">${{escapeHtml(copy("rt_sms_block_title", "Text follow-up"))}}</h2>
                          <p class="resolution-block__sub">${{escapeHtml(copy("rt_sms_block_sub", "What prospects see on their phone — not this voice session."))}}</p>
                        </{D}>
                      </header>
                      <{D} class="conversation-preview conversation-preview--standalone">
                        <{D} class="conversation-preview__thread" role="list">
                          <article class="chat-msg from-buyer" role="listitem">
                            <span class="chat-msg__who">Prospect</span>
                            <p>${{escapeHtml(copy("rt_preview_msg_1", "Hey, is the 2022 F-150 XLT still on the lot?"))}}</p>
                          </article>
                          <article class="chat-msg from-agent" role="listitem">
                            <span class="chat-msg__who">Hammer</span>
                            <p>${{escapeHtml(copy("rt_preview_msg_2", "Hey! Yes, it's still here. Are you looking to finance, or did you have a trade-in in mind?"))}}</p>
                          </article>
                          <article class="chat-msg from-buyer" role="listitem">
                            <span class="chat-msg__who">Prospect</span>
                            <p>${{escapeHtml(copy("rt_preview_msg_3", "I'd want to trade in my 2019 Ram 1500."))}}</p>
                          </article>
                          <article class="chat-msg from-agent" role="listitem">
                            <span class="chat-msg__who">Hammer</span>
                            <p>${{escapeHtml(copy("rt_preview_msg_4", "Got it. We handle trade-ins right here. Want to swing by today at 5pm or Saturday at 11am?"))}}</p>
                          </article>
                        </{D}>
                      </{D}>
                    </section>

                    <section class="resolution-block resolution-block--voice" aria-label="${{escapeHtml(copy("rt_voice_block_aria", "Live voice demo"))}}">
                      <header class="resolution-block__head resolution-block__head--voice">
                        <{D} class="resolution-block__titles">
                          <h2 class="resolution-block__title">${{escapeHtml(copy("rt_chat_title", "Live voice demo"))}}</h2>
                          <p class="resolution-block__sub">${{escapeHtml(copy("rt_voice_block_sub", "Talk to the sales agent here — grounded in Hammer product knowledge."))}}</p>
                        </{D}>
                        <span class="chat-live${{live ? " is-on" : ""}}">
                          <span class="pulse-dot" aria-hidden="true"></span>
                          <span class="chat-live__label">${{live ? escapeHtml(copy("rt_chat_live", "Live")) : escapeHtml(copy("rt_chat_ready", "Ready"))}}</span>
                        </span>
                      </header>
                      <{D} class="agent-stage agent-stage--resolution">
                        <{D} class="chat-window chat-window--hero chat-window--resolution chat-window--voice-only">
                          <{D} class="voice-body voice-body--resolution">"""

voice_open = f'                          <{D} class="voice-body voice-body--resolution">'
text = text[:start] + new + text[end + len(voice_open):]

old_close = (
    "                        </div>\n"
    "                      </div>\n"
    "                    </div>\n"
    "                  </motion>\n"
    "                </motion>\n"
)
old_close = (
    f"                        </{D}>\n"
    f"                      </{D}>\n"
    f"                    </{D}>\n"
    f"                  </{D}>\n"
    f"                </{D}>\n"
)
new_close = (
    f"                        </{D}>\n"
    f"                      </{D}>\n"
    "                    </section>\n"
    f"                  </{D}>\n"
    f"                </{D}>\n"
)

if old_close not in text:
    raise SystemExit("close block not found")
text = text.replace(old_close, new_close, 1)

p.write_text(text, encoding="utf-8")
print("patched ok")
