import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "src/halocli/web_static",
    emptyOutDir: true
  },
  test: {
    environment: "jsdom",
    setupFiles: ["frontend/src/test-setup.ts"]
  }
});
