// The site's settings. These defaults suit a local preview (the API on http://localhost:8000).
// scripts/deploy-web.sh writes a fresh copy of this file from SITE_* variables at deploy time, so your own
// deployment's values never need to be committed. Everything here is public: never put a secret in this file.
window.SITE_CONFIG = {
  name: "VIGIL",           // the persona's name, as shown on the page
  xHandle: "",             // the bot's X account, without the @ ("" hides the handle)
  api: "http://localhost:8000",  // the backend's public URL (ctagent api / run-all)
  apiFallback: "",         // optional second URL, tried when the first can't be reached
  token: { ca: "", gmgn: "" },   // the project's own token, once launched: mint address and GMGN link (https://gmgn.ai/sol/token/<mint>)
};
