// Receives Athena's push messages (the `notify` tool) when the page is closed or the phone is locked.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = {body: event.data ? event.data.text() : ""}; }
    event.waitUntil(self.registration.showNotification(data.title || "Athena", {
        body: data.body || "",
        icon: "/favicon.ico",
        tag: "athena",
        data: {url: data.url || "/"},
    }));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || "/";
    event.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then((windows) => {
        for (const w of windows) if ("focus" in w) return w.focus();
        return self.clients.openWindow(url);
    }));
});
