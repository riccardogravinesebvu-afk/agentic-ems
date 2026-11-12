import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  base: "./", // relative URLs: the same build works on localhost and under any path
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  build: { chunkSizeWarningLimit: 800 }, // Recharts makes the one bundle about 680 kB; one page needs no code splitting
  // The demo server accepts only a Host of localhost or 127.0.0.1; without changeOrigin the page's own Host is sent.
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
})
