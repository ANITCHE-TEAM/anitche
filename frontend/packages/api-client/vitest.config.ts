import { testConfigPaquet } from "@anitche/config/vitest";
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { ...testConfigPaquet({ environment: "node" }), setupFiles: ["./src/testing/setup.ts"] },
});
