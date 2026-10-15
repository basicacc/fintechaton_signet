import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev, API calls go to the backend on :8077. In production the backend serves this build.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8077' } },
  build: { outDir: '../backend/src/pca/static', emptyOutDir: true },
})
