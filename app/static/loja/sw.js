// Service Worker da LOJA (escopo /loja/). Separado do SW da gestão pra evitar
// conflito de cache (versões/assets diferentes).
//
// Estratégia conservadora:
// - Navegação (HTML): NETWORK FIRST. Online sempre serve a versão fresca; sem
//   rede, cai pra última versão em cache. Evita PWA "preso" com versão velha.
// - Assets estáticos (/static/...): CACHE FIRST. Carrega instantâneo; quando
//   a versão muda (bump da `VERSION` abaixo), o cache antigo é descartado.
// - POST/PUT/DELETE: NUNCA cacheia (SW só responde GET).
//
// Atualizar pra forçar refresh: bumpar VERSION + clientes recarregam.

const VERSION = 'loja-v2';
const CACHE_STATIC = `opao-loja-static-${VERSION}`;
const CACHE_RUNTIME = `opao-loja-runtime-${VERSION}`;

const ASSETS_BASE = [
  '/loja/static/loja/loja.css',
  '/loja/static/loja/carrinho.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_STATIC)
      .then((cache) => cache.addAll(ASSETS_BASE).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((nomes) => Promise.all(
        nomes
          .filter((n) => n.startsWith('opao-loja-')
                         && n !== CACHE_STATIC
                         && n !== CACHE_RUNTIME)
          .map((n) => caches.delete(n))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Fora da loja (ex: /admin) NÃO mexe — quem responde é o SW da gestão se
  // estiver registrado, ou a rede direto.
  if (!url.pathname.startsWith('/loja') && !url.pathname.startsWith('/static')) {
    return;
  }
  // Webhook/api de pagamento NUNCA cacheia (resposta sensível a tempo).
  if (url.pathname.includes('/webhook') || url.pathname.includes('/api/')) {
    return;
  }

  // Navegação HTML: network first (rede fresca; cache fallback offline).
  if (request.mode === 'navigate'
      || (request.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(request)
        .then((resp) => {
          // Só cacheia respostas OK (não cacheia 404/500).
          if (resp && resp.ok) {
            const copia = resp.clone();
            caches.open(CACHE_RUNTIME).then((c) => c.put(request, copia));
          }
          return resp;
        })
        .catch(() => caches.match(request).then((c) => c || caches.match('/loja/')))
    );
    return;
  }

  // Assets estáticos: cache first com atualização em background.
  if (url.pathname.startsWith('/static') || url.pathname.startsWith('/loja/static')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const fresca = fetch(request).then((resp) => {
          if (resp && resp.ok) {
            const copia = resp.clone();
            caches.open(CACHE_STATIC).then((c) => c.put(request, copia));
          }
          return resp;
        }).catch(() => null);
        return cached || fresca;
      })
    );
  }
});
