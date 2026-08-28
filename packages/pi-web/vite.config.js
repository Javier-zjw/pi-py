import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物直接落进 pi_server/web/，FastAPI 会托管它。
// 开发时 /api 转发到后端，前后端各跑各的，改前端不用重启后端。
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "../pi-server/pi_server/web",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8848",
        changeOrigin: true,
        // SSE 必须关掉缓冲，否则事件会被攒着一起发
        configure: (proxy) => {
          proxy.on("proxyRes", (res) => {
            res.headers["cache-control"] = "no-cache";
          });
        },
      },
    },
  },
});
