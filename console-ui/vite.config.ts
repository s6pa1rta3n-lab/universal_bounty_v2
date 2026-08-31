import { resolve } from "path";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/console/",
  build: {
    outDir: "../static/console",
    emptyOutDir: true,
    assetsDir: "assets",
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        pipeline: resolve(__dirname, "pipeline.html"),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/health": "http://127.0.0.1:8080",
    },
  },
});
