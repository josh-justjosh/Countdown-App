export async function api(path, options = {}) {
  const opts = { ...options };
  const headers = { ...(opts.headers || {}) };
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  opts.headers = headers;
  const res = await fetch(path, opts);
  const contentType = res.headers.get("content-type") || "";
  let data = null;
  if (contentType.includes("application/json")) {
    data = await res.json();
  } else if (contentType.includes("application/zip") || contentType.includes("octet-stream")) {
    data = await res.blob();
  } else if (!res.ok) {
    data = await res.text();
  }
  if (!res.ok) {
    const message = (data && data.detail) || (typeof data === "string" ? data : res.statusText);
    throw new Error(message || `HTTP ${res.status}`);
  }
  return data;
}

export function formatTime(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "--:--";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
