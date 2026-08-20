import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands in the Python package's static directory and is committed, so
// installing ppk2lab from PyPI never needs Node. That package lives under the
// repository's src/ layout beside ppk2lab itself, which is why this reaches up
// out of the frontend project rather than into a sibling directory.
export default defineConfig({
  plugins: [react()],
  base: "./",
  // React is MIT, and MIT requires its copyright notice to travel with every
  // copy. Vite's default strips license banners during minification, which
  // would put an unlicensed copy of React inside the wheel. `eof` keeps the
  // notices in the file that is actually shipped.
  esbuild: { legalComments: "eof" },
  build: {
    outDir: "../src/ppk2lab_web/static",
    emptyOutDir: true,
    // One JS file and one CSS file keeps the served surface — and the diff of
    // the committed build — reviewable.
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "app-[name].js",
        assetFileNames: "app.[ext]",
      },
    },
  },
  server: { port: 5273 },
});
