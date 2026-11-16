import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local dev: /api is proxied to the booking service on :8000 (same path CloudFront uses in AWS).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
});
