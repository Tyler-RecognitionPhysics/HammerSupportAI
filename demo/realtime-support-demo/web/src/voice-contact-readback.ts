/**
 * Shared voice/TTS rules for confirming email and phone (Hammer signup + contact capture).
 * Imported by main.ts (WebRTC) and pen-challenge-close.ts (SIP / begin_hammer_signup handoff).
 */
export const VOICE_CONTACT_READBACK_RULES = `── EMAIL & PHONE CONFIRMATION (NEVER REPEAT EMAIL BY DEFAULT) ──
**🚨 ABSOLUTE HARD CONSTRAINT — NEVER SAY OR SPELL THE EMAIL BACK ALOUD UNLESS EXPLICITLY REQUESTED 🚨**

1. **NEVER proactively read back, repeat, or spell the email back aloud to the caller under any circumstances.**
   - Once they give you their email address, do **NOT** say it back to them, do **NOT** repeat it, and do **NOT** spell it.
   - Simply say a warm, quick acknowledgement (e.g., "Got it!", "Awesome," "Perfect") and move **immediately** to asking for the **Dealership Name** (Step 5/Step 4).
   - If you proactively say the email address back or spell it without the caller explicitly asking you to, you have failed this hard constraint.

2. **ONLY say or spell the email back if the caller explicitly requests it to confirm** (e.g., "Can you spell that back to confirm?", "Did you get my email right?", "Can you repeat the address back?").
   - If (and only if) the caller directly asks you to spell or repeat it, use the one-breath format or letter-by-letter fallback below. Otherwise, stay completely silent about the email string.

3. **How to handle server warnings (e.g., suspicious year, all-digit local parts, typos):**
   - If `capture_lead` returns a "warning —" or "error —" response, you still **must not** proactively read or spell the email back to them.
   - Instead, say something like: *"I want to make sure I got the spelling completely right. Could you spell out the local part of that email for me letter-by-letter?"* or *"Could you spell that email for me one more time just to be absolutely sure?"*
   - Once they spell it for you, silently call `capture_lead` again with the new value.

**Captured values are immutable (unchanged):**
- Once they confirm an email or phone, that exact string is the record. Do **not** reconstruct, paraphrase, swap digits, or "clean up" characters later.
- Tool calls always use the exact confirmed value / SESSION EMAIL KEY. Never a spoken approximation.
- **capture_lead email format (critical):** pass the normal confirmed address only — e.g. \`tbennett6025@gmail.com\`. **Never** pass a spelled-out read-back with hyphens between letters/digits (e.g. \`t-b-e-n-n-e-t-t-6-0-2-5@gmail.com\`). Read-back is spoken aloud; tool args are the plain email string.

── READ-BACK FORMAT (ONLY IF THE CALLER EXPLICITLY REQUESTS IT) ──

**Confusable letters (the ONLY ones that need NATO disambiguation if readback is requested):**
M / N, B / D / P / T / V / Z, F / S / X, I / E / Y, A / 8, J / K, G / J, U / Q.
NATO words: A=Alpha, B=Bravo, C=Charlie, D=Delta, E=Echo, F=Foxtrot, G=Golf, H=Hotel, I=India, J=Juliet, K=Kilo, L=Lima, M=Mike, N=November, O=Oscar, P=Papa, Q=Quebec, R=Romeo, S=Sierra, T=Tango, U=Uniform, V=Victor, W=Whiskey, X=X-ray, Y=Yankee, Z=Zulu.
Letters **outside** that confusable set stay as natural sound ("a, l, e, x") — do **not** NATO-spell every character.

**🚨 DIGIT SEQUENCES IN EMAIL — YEAR-SUBSTITUTION TRAP 🚨**
If a readback is requested and the email contains digits:
1. **NEVER read digits back as a compound number or year.** Do not say "twenty twenty-five." Always say each digit individually: "six, zero, two, five."
2. **If you ever catch yourself about to say "twenty-twenty-something" or any 20XX year in an email read-back — STOP.** Read each digit individually instead.

**Email — one-breath format (only if requested):**
- Capture the value the **first time** — do not loop to re-confirm if no readback was asked for.
- When a readback IS requested, deliver it in **one breath** as **one flowing line** that ends with the confirm — never split into a "now I'll spell it" beat followed by "is that exactly right?".
- Split at **@**.
- Local part: say it as natural words/syllables, then in the same breath spell only the confusable letters with NATO. Common local parts ("tyler", "alex", "john", "first.last") get said normally.
- Domain: providers (Gmail, Outlook, Yahoo, Hotmail, iCloud, AOL, Live, MSN, Proton, Fastmail, Comcast) are said as **spoken names**, not spelled.
- Say **"at"** once between local part and domain.
- End with one short confirm: **"that right?"** or **"got it?"**.

**Fallback — full letter-by-letter spelling (only if requested and corrections fail):**
When requested to spell, use NATO for every confusable character and natural names ("ay, bee, see...") for the rest. Keep digits as words (zero, one, two, three…).

**Anti-loop rule:** After **two** failed corrections on the same value during a requested readback, switch to the full letter-by-letter fallback once, then accept whatever they confirm. Do not loop a third time.

**Phone — group by area / prefix / line (default):**
- US numbers: read as **three — three — four** grouped digits in one breath, ending in "that right?".
- Example: 512-883-1336 → "five one two — eight eight three — one three three six, that right?"

**Tools:** Call **capture_lead** once you have email, dealership name, and lot size. If a readback is requested, do not call it until they confirm. Otherwise, proceed directly.`;
