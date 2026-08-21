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
  server: {
    port: 5273,
    // The URL rule is identical in development and in production: the page
    // always talks to its own origin, and the bundle -- which ships inside the
    // Python wheel -- never carries a host. `npm run dev` on its own still runs
    // the simulated console; `?source=ws` opts into this proxy.
    // A different address is a runtime question, not a build-time one, so it
    // is `?ws=wss://bench:8765/ws` in the page rather than an env var here --
    // which also keeps the test suite free of @types/node.
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      "/api": { target: "http://127.0.0.1:8765" },
    },
  },
});
