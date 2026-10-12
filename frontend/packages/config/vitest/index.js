import { fileURLToPath } from "node:url";

const SETUP_NAVIGATEUR = fileURLToPath(new URL("./setup.ts", import.meta.url));

/** Tests d'un portail : jsdom + Testing Library. */
export const testConfig = {
  environment: "jsdom",
  globals: false,
  setupFiles: [SETUP_NAVIGATEUR],
};

/**
 * Tests d'un paquet de packages/. `environment: "node"` pour un paquet sans DOM (utils) :
 * ni jsdom ni Testing Library ne sont alors chargés.
 * @param {{ environment?: "jsdom" | "node" }} options
 */
export function testConfigPaquet({ environment = "jsdom" } = {}) {
  return {
    environment,
    globals: false,
    setupFiles: environment === "jsdom" ? [SETUP_NAVIGATEUR] : [],
  };
}
