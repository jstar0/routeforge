import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// PAGES_BASE is set only for the static GitHub Pages replay build (output: web/dist).
const pagesBase = process.env.PAGES_BASE;

export default defineConfig({
  base: pagesBase ?? "/",
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:5203" } },
  build: { outDir: pagesBase ? "dist" : "../src/routeforge/static", emptyOutDir: true },
});
