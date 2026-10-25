// Address of the chat server (chat.py).
// Locally it runs on port 8000; on GitHub Pages it is the Render service.
// After creating the Render service, replace the URL below with the one Render shows.
const RENDER_API_URL = "https://course-scheduler-api.onrender.com";

const IS_LOCAL = ["localhost", "127.0.0.1", ""].includes(window.location.hostname) || window.location.protocol === "file:";
window.API_BASE = IS_LOCAL ? "http://localhost:8000" : RENDER_API_URL;
