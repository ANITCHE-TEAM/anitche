import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["eslint/**/*.test.js"], environment: "node" },
});
