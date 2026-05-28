from pathlib import Path

D = "d" + "iv"
CLOSE = f"</{D}>"
OPEN = f"<{D} "

p = Path(__file__).resolve().parents[1] / "src" / "main.ts"
text = p.read_text(encoding="utf-8")

if 'id="navPanelReviews"' in text:
    print("already patched")
    raise SystemExit(0)

old = (
    f"                {CLOSE}\n"
    f"              {CLOSE}\n"
    f"            {CLOSE}\n"
    '            <footer class="nav-panel__foot">'
)

new = (
    f"                {CLOSE}\n"
    f"              {CLOSE}\n"
    f'              <{D} id="navPanelReviews" class="nav-panel__section ${{openNavPanel === "reviews" ? "is-active" : ""}}">\n'
    '                <p class="nav-panel__lead">${escapeHtml(copy("rt_nav_panel_reviews_1", "Dealers use Hammer to answer faster, follow up longer, and book more appointments without adding headcount."))}</p>\n'
    f'                <{D} class="nav-panel__features">\n'
    '                  <article class="nav-panel__feature nav-panel__feature--quote"><span class="nav-panel__feature-n">01</span><p>${escapeHtml(copy("rt_nav_panel_reviews_2", "8 in 10 leads text back when Hammer engages them quickly."))}</p></article>\n'
    '                  <article class="nav-panel__feature nav-panel__feature--quote"><span class="nav-panel__feature-n">02</span><p>${escapeHtml(copy("rt_nav_panel_reviews_3", "Persistent follow-up revives cold leads your team does not have time to chase."))}</p></article>\n'
    '                  <article class="nav-panel__feature nav-panel__feature--quote"><span class="nav-panel__feature-n">03</span><p>${escapeHtml(copy("rt_nav_panel_reviews_4", "Managers see cleaner CRM notes and handoffs when reps take over."))}</p></article>\n'
    f"                {CLOSE}\n"
    f"              {CLOSE}\n"
    f'              <{D} id="navPanelFaq" class="nav-panel__section ${{openNavPanel === "faq" ? "is-active" : ""}}">\n'
    '                <p class="nav-panel__lead">${escapeHtml(copy("rt_nav_panel_faq_1", "Quick answers before you try the live demo below."))}</p>\n'
    f'                <{D} class="nav-panel__faq">\n'
    '                  <article class="nav-panel__faq-item">\n'
    '                    <h3 class="nav-panel__faq-q">${escapeHtml(copy("rt_nav_panel_faq_q1", "What does Hammer do?"))}</h3>\n'
    '                    <p class="nav-panel__faq-a">${escapeHtml(copy("rt_nav_panel_faq_a1", "AI lead response and follow-up for dealerships. Instant text reply, ongoing nurture, and voice when you want it live."))}</p>\n'
    "                  </article>\n"
    '                  <article class="nav-panel__faq-item">\n'
    '                    <h3 class="nav-panel__faq-q">${escapeHtml(copy("rt_nav_panel_faq_q2", "Does it replace my BDC?"))}</h3>\n'
    '                    <p class="nav-panel__faq-a">${escapeHtml(copy("rt_nav_panel_faq_a2", "No. Hammer handles speed-to-lead and follow-up; your team stays in the CRM to close."))}</p>\n'
    "                  </article>\n"
    '                  <article class="nav-panel__faq-item">\n'
    '                    <h3 class="nav-panel__faq-q">${escapeHtml(copy("rt_nav_panel_faq_q3", "Will it work with our CRM?"))}</h3>\n'
    '                    <p class="nav-panel__faq-a">${escapeHtml(copy("rt_nav_panel_faq_a3", "Hammer fits your existing workflow and enriches leads in the systems you already use. Ask the demo about your stack."))}</p>\n'
    "                  </article>\n"
    '                  <article class="nav-panel__faq-item">\n'
    '                    <h3 class="nav-panel__faq-q">${escapeHtml(copy("rt_nav_panel_faq_q4", "How fast can we go live?"))}</h3>\n'
    '                    <p class="nav-panel__faq-a">${escapeHtml(copy("rt_nav_panel_faq_a4", "Many stores are live within about 72 hours after onboarding."))}</p>\n'
    "                  </article>\n"
    f"                {CLOSE}\n"
    f"              {CLOSE}\n"
    f"            {CLOSE}\n"
    '            <footer class="nav-panel__foot">'
)

idx = text.rfind(old)
if idx < 0:
    raise SystemExit("anchor not found")
text = text[:idx] + new + text[idx + len(old) :]
p.write_text(text, encoding="utf-8")
print("patched ok")
