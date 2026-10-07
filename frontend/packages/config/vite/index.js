import { fileURLToPath } from "node:url";
import { loadEnv } from "vite";
import { testConfig } from "../vitest/index.js";

const RACINE_FRONTEND = fileURLToPath(new URL("../../..", import.meta.url));

/**
 * Réglages Vite communs aux quatre portails : port fixe, proxy de dev, tests.
 * Les cibles du proxy viennent de VITE_PROXY_DJANGO / VITE_PROXY_FASTAPI
 * (environnement du process, sinon `.env` à la racine de frontend/).
 */
export function configVite({ mode, port }) {
  const env = { ...loadEnv(mode, RACINE_FRONTEND, "VITE_PROXY_"), ...process.env };
  return {
    envDir: RACINE_FRONTEND,
    server: {
      port,
      strictPort: true,
      proxy: {
        "/api": { target: env.VITE_PROXY_DJANGO ?? "http://localhost:8000" },
        "/fast": {
          target: env.VITE_PROXY_FASTAPI ?? "http://localhost:8001",
          ws: true,
          rewrite: (chemin) => chemin.replace(/^\/fast/, ""),
        },
      },
    },
    test: testConfig,
  };
}
