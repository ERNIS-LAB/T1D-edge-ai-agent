import { serve } from "bun";
import index from "./index.html";

const BACKEND_URL = process.env.BACKEND_URL || "http://0.0.0.0:8000";

const server = serve({
    port: Number(process.env.PORT || 3001),
    routes: {
        "/*": index,
    },
    fetch(req) {
        const url = new URL(req.url);

        // Proxy /api/* and /health to the Python backend
        if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
            const target = `${BACKEND_URL}${url.pathname}${url.search}`;
            return fetch(target, {
                method: req.method,
                headers: req.headers,
                body: req.method !== "GET" && req.method !== "HEAD" ? req.body : undefined,
            });
        }

        return new Response("Not Found", { status: 404 });
    },
    development: process.env.NODE_ENV !== "production" && {
        hmr: true,
        console: true,
    },
});

console.log(`Jetson web UI running at ${server.url}`);
console.log(`Proxying API requests to ${BACKEND_URL}`);
