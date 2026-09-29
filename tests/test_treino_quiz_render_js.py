"""Conteúdo do quiz é texto, mesmo quando cadastrado por gestores delegados."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('aprovada', [True, False])
def test_quiz_renderiza_texto_sem_executar_html_e_envia_respostas(aprovada):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para o teste de interação')
    arquivo = Path(__file__).resolve().parents[1] / 'app/templates/treino/trilha.html'
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ''; }
  set innerHTML(value) { throw new Error('Quiz não deve interpretar HTML'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  scrollIntoView() { this.scrolled = true; }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelectorAll(selector) {
    if (selector === '[data-q]') return this.all().filter(n => n.dataset.q !== undefined);
    throw new Error('Seletor não esperado: ' + selector);
  }
  querySelector(selector) {
    assert.equal(selector, 'input:checked');
    return this.all().find(n => n.tag === 'input' && n.checked);
  }
}
const box = new Element('div'), requests = [], approved = process.argv[2] === 'true';
const document = {
  getElementById: id => id === 'quizbox' ? box : box.all().find(n => n.id === id),
  querySelectorAll: () => [],
  createElement: tag => new Element(tag),
  createTextNode: text => Object.assign(new Element('#text'), {textContent: text}),
};
const source = fs.readFileSync(process.argv[1], 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const sandbox = {document, Date, Promise, encodeURIComponent, alert: () => {}, fetch: (url, options) => {
  requests.push({url, options});
  return Promise.resolve({json: () => Promise.resolve({ok: true, aprovada: approved, acertos: 1, total: 1})});
}};
vm.runInNewContext(source, sandbox);
const enunciado = '<img src=x onerror="window.quizXss=1">';
const alternativa = '<svg onload="window.quizXss=2"></svg>';
sandbox.renderQuiz({tentativa_id: 15, questoes: [{questao_id: 7, enunciado,
  alternativas: [{id: 9, texto: alternativa}, {id: 10, texto: 'Resposta comum & correta'}]}]});
assert.ok(box.scrolled);
assert.equal(box.all().find(n => n.tag === 'strong').textContent, '1. ' + enunciado);
assert.equal(box.all().filter(n => n.tag === '#text')[0].textContent, ' ' + alternativa);
assert.equal(box.all().filter(n => n.tag === '#text')[1].textContent, ' Resposta comum & correta');
assert.equal(box.all().filter(n => ['img', 'svg', 'script'].includes(n.tag)).length, 0);
const radio = box.all().find(n => n.type === 'radio');
assert.equal(radio.name, 'q7'); assert.equal(radio.value, 9); radio.checked = true;
document.getElementById('enviar').onclick().then(() => {
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, '/treino/api/tentativas/15/responder');
  assert.match(requests[0].options.body, /^questao_id=7&alternativa_id=9&segundos=\d+$/);
  assert.equal(requests[1].url, '/treino/api/tentativas/15/finalizar');
  const notice = document.getElementById('qres').children[0];
  assert.equal(notice.textContent, approved ? '1/1 — Você passou.' : '1/1 — Você ainda não atingiu a nota mínima.');
  assert.ok(notice.className.endsWith(approved ? 'success' : 'warning'));
  sandbox.renderQuiz({tentativa_id: 16, questoes: []});
  assert.equal(box.children.length, 1); assert.equal(document.getElementById('tid').value, 16);
  assert.equal(box.all().filter(n => n.tag === 'strong').length, 0);
}).catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node, '-e', script, str(arquivo), str(aprovada).lower()],
                   capture_output=True, text=True, check=True, timeout=15)
