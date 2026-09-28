"""A confirmação mostra os destinatários efetivos e evita cliques repetidos."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_previa_e_confirmacao_acompanham_email_digitado():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para o teste de interação')
    arquivo = Path(__file__).resolve().parents[1] / 'app/static/js/cobrancas.js'
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let input, submit, aceitar = false, mensagem = '', confirmacoes = 0;
const email = {value: 'principal@example.com', addEventListener: (_, fn) => {input = fn;}};
const button = {disabled: false};
const nodes = {
  'cob-send-form': {elements: {email}, addEventListener: (_, fn) => {submit = fn;}},
  'cob-send-button': button,
  'cob-cc': {dataset: {copias: JSON.stringify(['compras@example.com', 'CAIO@opao.online'])}},
  'cob-cc-lista': {},
  'cob-bcc': {dataset: {copias: JSON.stringify(['caio@opao.online', 'dakson@opao.online', 'contato@opao.online'])}},
  'cob-bcc-lista': {}
};
const sandbox = {
  document: {getElementById: id => nodes[id], querySelectorAll: () => []},
  window: {addEventListener: () => {}, confirm: texto => {
    mensagem = texto; confirmacoes++; return aceitar;
  }}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
assert.equal(nodes['cob-cc-lista'].textContent, 'compras@example.com, CAIO@opao.online');
assert.equal(nodes['cob-bcc-lista'].textContent, 'dakson@opao.online, contato@opao.online');
email.value = '  COMPRAS@example.com '; input();
assert.equal(nodes['cob-cc-lista'].textContent, 'CAIO@opao.online');
let impediu = false;
submit({preventDefault: () => {impediu = true;}});
assert.ok(impediu); assert.equal(button.disabled, false);
assert.match(mensagem, /para COMPRAS@example.com\?/);
assert.match(mensagem, /Em cópia \(CC\): CAIO@opao.online/);
aceitar = true; impediu = false;
submit({preventDefault: () => {impediu = true;}});
assert.equal(impediu, false); assert.equal(button.disabled, true);
submit({preventDefault: () => {impediu = true;}});
assert.ok(impediu); assert.equal(confirmacoes, 2);
email.value = 'DAKSON@opao.online'; input();
assert.equal(nodes['cob-bcc-lista'].textContent, 'contato@opao.online');
'''
    subprocess.run([node, '-e', script, str(arquivo)],
                   capture_output=True, text=True, check=True, timeout=15)
