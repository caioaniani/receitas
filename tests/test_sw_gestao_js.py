"""Service worker da gestão (/sw.js) — a página "Sem conexão" é SÓ para
navegação (caso 14/10/2026, print do dono em /pdv/: "HTTP 200 — resposta
não-JSON ... Sem conexão"). Uma queda de rede num fetch() de dados virava a
página offline com status 200 e a tela mostrava HTML no lugar do JSON.

Roda o JS REAL no node (padrão de test_painel_alertas_js), sem rede.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'sw.js'

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const handlers = {};
class Response { constructor(body, init) { this.body = body; this.status = (init && init.status) || 200; this.headers = (init && init.headers) || {}; } }
const ctx = {
  self: {addEventListener(n, f) { handlers[n] = f; }, skipWaiting() {}, clients: {claim() {}}},
  location: {origin: 'https://gestao.exemplo'},
  caches: {match: async () => undefined, open: async () => ({put() {}}), keys: async () => []},
  fetch: async () => { throw new TypeError('Load failed'); },
  Response, URL, Promise, console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

function disparar(url, mode) {
  let resposta = null;
  handlers.fetch({request: {method: 'GET', url, mode, headers: {get: () => ''}},
                  respondWith(p) { resposta = p; }});
  return resposta;
}

(async () => {
  // fetch() de dados (mode 'cors'/'same-origin') não é interceptado.
  assert.equal(disparar('https://gestao.exemplo/pdv/api/vendas?inicio=x', 'cors'), null);
  assert.equal(disparar('https://gestao.exemplo/padeiro/faltas.json', 'same-origin'), null);
  assert.equal(disparar('https://gestao.exemplo/entregas/api/painel', 'cors'), null);
  // Navegação sem rede ainda recebe a página "Sem conexão".
  const nav = disparar('https://gestao.exemplo/pdv/', 'navigate');
  assert.ok(nav, 'navegação deve ser respondida pelo SW');
  const r = await nav;
  assert.match(r.body, /Sem conexão/);
  // /api/ segue fora mesmo navegando.
  assert.equal(disparar('https://gestao.exemplo/api/claude/deploy', 'navigate'), null);
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node ausente')
def test_sw_so_mostra_sem_conexao_em_navegacao(tmp_path):
    harness = tmp_path / 'h.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    r = subprocess.run(['node', str(harness), str(_SCRIPT)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == 'ok'
