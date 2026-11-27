import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "./",
  build: { outDir: "dist", emptyOutDir: true, target: "es2023" },
  test: { environment: "node", include: ["tests/**/*.test.ts"] },
});
