# web/ — public website (browser)

What customers see: landing page, **Start call**, voice UI.

**Vercel builds this folder** (`npm run build` → `dist/`).  
**Local:** run `..\2-START-LOCAL-WEB.ps1` after the API is up.

## Main source files

| File | What it does |
|------|----------------|
| **`src/main.ts`** | Voice agent, tools, signup flow (PHASE A/B/C) |
| **`src/pen-challenge-close.ts`** | Closing / signup prompts |
| **`src/pen-challenge-instructions.ts`** | Sales instructions |
| **`src/landing-hero.css`** | Landing page layout and styling |
| **`src/style.css`** | Shared UI styles |

## Safe env vars (optional, public in build)

Only use **`VITE_*`** for non-secret UI settings (voice name, sign-in URL).  
**Never** put `OPENAI_API_KEY` or Hammer passwords in `VITE_*`.

## API calls

The browser talks to **`/api/*`** on the same host (Vercel) or via Vite proxy to port **8780** locally.
