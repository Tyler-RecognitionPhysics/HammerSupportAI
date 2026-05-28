import type { ServerResponse } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const webRoot = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      "@elevenlabs/client": path.resolve(
        webRoot,
        "node_modules/@elevenlabs/client/dist/platform/web/index.js",
      ),
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8781",
        changeOrigin: true,
        configure(proxy) {
          proxy.on("error", (err, _req, res) => {
            const r = res as ServerResponse | undefined;
            if (!r || r.headersSent) return;
            const msg = err instanceof Error ? err.message : String(err);
            r.writeHead(502, { "Content-Type": "application/json" });
            r.end(
              JSON.stringify({
                detail:
                  `Dev proxy could not reach the API at http://127.0.0.1:8781 (${msg}). ` +
                  "Start uvicorn from demo/realtime-support-demo/server, then retry.",
              }),
            );
          });
        },
      },
      "/debug": { target: "http://127.0.0.1:8781", changeOrigin: true },
      "/admin": { target: "http://127.0.0.1:8781", changeOrigin: true },
    },
  },
});
