// Service Worker — Padaria O Pão
// Cache estratégia: Network First para HTML/JSON (dados sempre frescos),
// Cache First para assets estáticos (CSS, JS, fontes, imagens).

const VERSION = 'v2';
const CACHE_STATIC = `padaria-static-${VERSION}`;
const CACHE_RUNTIME = `padaria-runtime-${VERSION}`;

const ASSETS_ESTATICOS = [
    '/static/css/style.css',
    '/static/js/app.js',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_STATIC)
            .then((cache) => cache.addAll(ASSETS_ESTATICOS).catch(() => null))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((nomes) => Promise.all(
                nomes
                    .filter((n) => n.startsWith('padaria-') && n !== CACHE_STATIC && n !== CACHE_RUNTIME)
                    .map((n) => caches.delete(n))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;

    // Ignora métodos não-GET (POST, PUT, DELETE)
    if (request.method !== 'GET') return;

    const url = new URL(request.url);

    // Ignora requisições para outros domínios (CDN, APIs externas, etc.)
    if (url.origin !== location.origin) return;

    // Ignora rotas dinâmicas que precisam ser sempre frescas
    const rotasSemCache = [
        '/copilot/',
        '/api/',
        '/auth/login',
        '/auth/logout',
        '/slack/',
        '/handshake/',
        '/health',
    ];
    if (rotasSemCache.some((r) => url.pathname.startsWith(r))) return;

    // Assets estáticos = cache first
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(request).then((cached) =>
                cached || fetch(request).then((resp) => {
                    if (resp.ok) {
                        const clone = resp.clone();
                        caches.open(CACHE_RUNTIME).then((c) => c.put(request, clone));
                    }
                    return resp;
                })
            )
        );
        return;
    }

    // Páginas HTML = network first com fallback ao cache
    event.respondWith(
        fetch(request)
            .then((resp) => {
                if (resp.ok && resp.type === 'basic') {
                    const clone = resp.clone();
                    caches.open(CACHE_RUNTIME).then((c) => c.put(request, clone));
                }
                return resp;
            })
            .catch(() => caches.match(request).then((cached) =>
                cached || new Response(
                    '<!DOCTYPE html><html><body style="font-family:system-ui;padding:40px;text-align:center;color:#444">' +
                    '<h2>Sem conexão</h2><p>Não foi possível carregar esta página. Verifique sua internet.</p>' +
                    '<p><button onclick="location.reload()" style="padding:10px 20px;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer">Tentar de novo</button></p>' +
                    '</body></html>',
                    { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                )
            ))
    );
});
