import react from "@vitejs/plugin-react";
import { configVite } from "@anitche/config/vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  ...configVite({ mode, port: 5173 }),
}));
