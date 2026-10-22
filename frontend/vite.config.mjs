import { defineConfig } from "vite";

export default defineConfig({
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../assets/web",
    emptyOutDir: false,
    sourcemap: false,
    cssCodeSplit: false,
    lib: {
      entry: "src/main.tsx",
      name: "GraphMindReader",
      formats: ["iife"],
      fileName: () => "graphmind.js",
      cssFileName: "graphmind",
    },
  },
});
