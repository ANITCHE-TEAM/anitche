import { fileURLToPath } from "node:url";

export const testConfig = {
  environment: "jsdom",
  globals: false,
  setupFiles: [fileURLToPath(new URL("./setup.ts", import.meta.url))],
};
