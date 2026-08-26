async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    throw new Error(`${path} ${res.status}`);
  }
  return res.json();
}

export function getState() {
  return request("/api/state");
}

export function getSettings() {
  return request("/api/settings");
}

export function saveSettings(payload) {
  return request("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function setControl(payload) {
  return request("/api/control", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
