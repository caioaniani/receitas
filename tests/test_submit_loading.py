"""Loading global preserva o botão enviado e bloqueia só reenvios do formulário."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const block = source.split('// ═══ UX: LOADING STATES ═══')[1]
  .split('// ═══ UX: CTRL+S PARA SALVAR ═══')[0];
const handlers = {}, windowHandlers = {}, timers = new Map(), microtasks = [];
const forms = [];
let nextTimer = 0;
vm.runInNewContext(block, {
  document: {forms, addEventListener(name, fn) {handlers[name] = fn;}},
  window: {addEventListener(name, fn) {windowHandlers[name] = fn;}},
  setTimeout(fn, delay) {
    assert.equal(delay, 10000);
    timers.set(++nextTimer, fn); return nextTimer;
  },
  clearTimeout(id) {timers.delete(id);},
  queueMicrotask(fn) {microtasks.push(fn);}
});
function flushMicrotasks() {while (microtasks.length) microtasks.shift()();}
function expireTimers() {for (const fn of [...timers.values()]) fn();}
function button(value, tagName = 'BUTTON') {
  const attrs = new Map(), classes = new Set();
  const btn = {tagName, name:'acao', value, dataset:{},
    getAttribute(name) {return attrs.has(name) ? attrs.get(name) : null;},
    setAttribute(name, val) {attrs.set(name, val);},
    removeAttribute(name) {attrs.delete(name);},
    classList: {contains(v) {return classes.has(v);}, add(v) {classes.add(v);},
      remove(v) {classes.delete(v);}}};
  // Disabled altera o payload nativo. O loading nunca pode usá-lo como trava.
  Object.defineProperty(btn, 'disabled', {
    get() {return false;}, set() {throw new Error('Loading alterou disabled');}
  });
  if (tagName === 'BUTTON') btn.innerHTML = `Texto ${value}`;
  else Object.defineProperty(btn, 'innerHTML', {
    get() {return '';}, set() {throw new Error('Spinner alterou um input');}
  });
  return btn;
}
function form(...buttons) {
  const f = {tagName:'FORM', buttons,
    querySelector() {throw new Error('Escolheu botão por posição, não pelo submitter');}};
  buttons.forEach(b => {b.form = f;}); forms.push(f); return f;
}
function submit(target, submitter, prevented = false) {
  const e = {target, submitter, defaultPrevented:prevented,
    preventDefault() {this.defaultPrevented = true;}};
  handlers.submit(e); return e;
}
const first = button('publicar'), second = button('rascunho');
const kit = form(first, second);
switch (process.argv[2]) {
  case 'submitter': {
    assert.equal(submit(kit, second).defaultPrevented, false);
    assert.equal(first.innerHTML, 'Texto publicar');
    assert.equal(first.getAttribute('aria-disabled'), null);
    assert.match(second.innerHTML, /Salvando/);
    assert.equal(second.name, 'acao'); assert.equal(second.value, 'rascunho');
    assert.equal(second.disabled, false);
    break;
  }
  case 'reenvio': {
    submit(kit, first); flushMicrotasks();
    assert.equal(submit(kit, second).defaultPrevented, true);
    assert.equal(second.innerHTML, 'Texto rascunho');
    const other = button('outro'), otherForm = form(other);
    assert.equal(submit(otherForm, other).defaultPrevented, false);
    break;
  }
  case 'timeout': {
    first.setAttribute('aria-disabled', 'false'); first.classList.add('disabled');
    submit(kit, first); flushMicrotasks(); expireTimers();
    assert.equal(first.innerHTML, 'Texto publicar');
    assert.equal(first.getAttribute('aria-disabled'), 'false');
    assert.equal(first.classList.contains('disabled'), true);
    assert.equal(timers.size, 0);
    assert.equal(submit(kit, second).defaultPrevented, false);
    assert.equal(second.value, 'rascunho');
    flushMicrotasks(); expireTimers();
    assert.equal(second.getAttribute('aria-disabled'), null);
    assert.equal(second.classList.contains('disabled'), false);
    break;
  }
  case 'historico': {
    submit(kit, first); flushMicrotasks(); windowHandlers.pageshow();
    assert.equal(first.innerHTML, 'Texto publicar');
    assert.equal(first.getAttribute('aria-disabled'), null);
    assert.equal(first.classList.contains('disabled'), false);
    assert.equal(timers.size, 0);
    assert.equal(submit(kit, second).defaultPrevented, false);
    break;
  }
  case 'optout': {
    second.dataset.noLoading = '1';
    assert.equal(submit(kit, second).defaultPrevented, false);
    assert.equal(submit(kit, second).defaultPrevented, false);
    assert.equal(second.innerHTML, 'Texto rascunho'); assert.equal(timers.size, 0);
    // O opt-out do segundo botão não desliga o loading do primeiro.
    assert.equal(submit(kit, first).defaultPrevented, false);
    assert.match(first.innerHTML, /Salvando/);
    break;
  }
  case 'interceptacao': {
    submit(kit, first, true);
    assert.equal(first.innerHTML, 'Texto publicar'); assert.equal(timers.size, 0);
    const e = submit(kit, first); e.preventDefault(); flushMicrotasks();
    assert.equal(first.innerHTML, 'Texto publicar'); assert.equal(timers.size, 0);
    assert.equal(submit(kit, second).defaultPrevented, false);
    break;
  }
  case 'input': {
    for (const type of ['submit', 'image']) {
      const input = button('Exportar', 'INPUT'); input.type = type;
      input.formAction = '/exportar'; input.formMethod = 'post';
      const f = form(input);
      assert.equal(submit(f, input).defaultPrevented, false);
      assert.equal(input.value, 'Exportar'); assert.equal(input.name, 'acao');
      assert.equal(input.formAction, '/exportar'); assert.equal(input.formMethod, 'post');
      assert.equal(input.disabled, false);
      flushMicrotasks(); expireTimers();
      assert.equal(input.value, 'Exportar');
    }
    break;
  }
  case 'sem_submitter': {
    assert.equal(submit(kit, null).defaultPrevented, false);
    assert.equal(first.innerHTML, 'Texto publicar'); assert.equal(second.innerHTML, 'Texto rascunho');
    assert.equal(submit(kit, null).defaultPrevented, true);
    flushMicrotasks(); expireTimers();
    assert.equal(submit(kit, first).defaultPrevented, false);
    break;
  }
  default: throw new Error('Cenário desconhecido');
}
'''


@pytest.mark.parametrize('cenario', [
    'submitter', 'reenvio', 'timeout', 'historico', 'optout',
    'interceptacao', 'input', 'sem_submitter',
])
def test_loading_preserva_envio_nativo_e_restaura_formulario(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para verificar o loading global')
    arquivo = Path(__file__).resolve().parents[1] / 'app/static/js/app.js'
    resultado = subprocess.run(
        [node, '-e', HARNESS, str(arquivo), cenario],
        capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
