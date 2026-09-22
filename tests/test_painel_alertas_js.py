"""Aviso de pendências do /entregas/painel (painel-alertas-atendimento.js)
NÃO bloqueia quem atende — caso 22/09/2026 ("não consigo acessar meu chat,
e o da TV não abre"): o modal reabria 20 s depois do "Abrir conversa"
(a outra pendência seguia vencida), voltava por cima da conversa a cada
5 min e, na TV, onde ninguém clica, cobria o painel inteiro.

Roda o JS REAL no node com um DOM mínimo (padrão de test_kits_cliente_js).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'js' / 'painel-alertas-atendimento.js'

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
let clock = 1700000000000;
const timeouts = [], intervals = [], dispatched = [];
function element(extra = {}) {
  const classes = new Set();
  return {textContent:'', value:'', hidden:false, disabled:false, open:false, handlers:{}, children:[],
    attributes:{}, dataset:{}, className:'', type:'', tagName:'DIV', parent:null,
    classList:{add(...n) {n.forEach(x => classes.add(x));}, remove(...n) {n.forEach(x => classes.delete(x));},
      contains(n) {return classes.has(n);},
      toggle(n, force) {const add = force === undefined ? !classes.has(n) : !!force;
        if (add) classes.add(n); else classes.delete(n); return add;}},
    addEventListener(n, f) {(this.handlers[n] ||= []).push(f);},
    setAttribute(n, v) {this.attributes[n] = String(v);}, getAttribute(n) {return this.attributes[n] ?? null;},
    removeAttribute(n) {delete this.attributes[n];},
    append(...nodes) {nodes.forEach(node => {node.parent = this; this.children.push(node);});},
    insertBefore(node, ref) {node.parent = this; const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(node); else this.children.splice(i, 0, node); return node;},
    remove() {if (this.parent) {const i = this.parent.children.indexOf(this); if (i >= 0) this.parent.children.splice(i, 1);} this.parent = null;},
    focus() {},
    fire(n, event = {}) {event.target ||= this; event.type = n; return (this.handlers[n] || []).map(f => f.call(this, event));},
    ...extra};
}
const partes = {};
for (const nome of ['botao','contador','dialog','lista','resumo','adiamento','erro-fora','erro-dentro','fechar','adiar']) {
  partes[nome] = element({id:'pa-' + nome});
}
partes.botao.hidden = true;
const dialog = partes.dialog;
const chamadas = {show:0, showModal:0};
dialog.show = function () {chamadas.show++; this.open = true;};
dialog.showModal = function () {chamadas.showModal++; this.open = true;};
dialog.close = function () {this.open = false; this.fire('close');};
const raiz = element({dataset:{url:'/entregas/api/atendimento/alertas'},
  querySelector(sel) {const m = sel.match(/^\[data-pa-([a-z-]+)\]$/); return m ? partes[m[1]] : null;}});
const thread = element({id:'at-thread'}); thread.classList.add('hidden');
const compose = element({id:'compose-texto', tagName:'TEXTAREA'});
const docHandlers = {};
const document = {readyState:'complete', hidden:false, body:element({tagName:'BODY'}),
  getElementById(id) {return {'painel-alertas-atendimento':raiz, 'at-thread':thread, 'compose-texto':compose}[id] || null;},
  querySelector() {return null;},
  createElement(tag) {return element({tagName:tag.toUpperCase()});},
  addEventListener(n, f) {(docHandlers[n] ||= []).push(f);},
  dispatchEvent(ev) {dispatched.push(ev); (docHandlers[ev.type] || []).forEach(f => f(ev)); return true;}};
const storage = {};
const window = {addEventListener() {}, location:{origin:'https://gestao.local'},
  sessionStorage:{getItem(k) {return storage[k] ?? null;}, setItem(k, v) {storage[k] = String(v);}}};
let payload = {alertas:[]};
const sandbox = {document, window, console,
  Date:{now:() => clock},
  CustomEvent:function (type, init) {this.type = type; this.detail = init && init.detail;},
  AbortController:function () {this.signal = {}; this.abort = () => {};},
  setTimeout(fn, ms) {const t = {fn, at:clock + ms}; timeouts.push(t); return t;},
  clearTimeout(t) {const i = timeouts.indexOf(t); if (i >= 0) timeouts.splice(i, 1);},
  setInterval(fn, ms) {const t = {fn, ms}; intervals.push(t); return t;},
  clearInterval(t) {const i = intervals.indexOf(t); if (i >= 0) intervals.splice(i, 1);},
  fetch:async () => ({ok:true, json:async () => JSON.parse(JSON.stringify(payload))})};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const tick = () => new Promise(r => setImmediate(r));
const settle = async () => {for (let i = 0; i < 6; i++) await tick();};
const poll = async () => {intervals[0].fn(); await settle();};
const vencer = async ms => {
  clock += ms;
  const due = timeouts.filter(t => t.at <= clock);
  due.forEach(t => {const i = timeouts.indexOf(t); if (i >= 0) timeouts.splice(i, 1);});
  due.forEach(t => t.fn());
  await settle();
};
const alerta = (conv, extra = {}) => ({chave:'c' + conv, conv_id:conv, cliente:'Cliente ' + conv,
  motivo:'Cliente aguardando uma resposta da equipe.', mensagem:'oi', grave:false,
  estado:'aguardando', ha_minutos:30, ...extra});
const botaoAbrir = i => partes.lista.children[i].children.find(c => c.tagName === 'BUTTON');
(async () => {
  await settle();  // consulta inicial sem pendências: fechado
  assert.equal(dialog.open, false);
  payload = {alertas:[alerta(2429, {grave:true}), alerta(2430)]};
  await poll();
  assert.equal(dialog.open, true, 'pendência nova abre o aviso');
  assert.equal(chamadas.showModal, 0, 'nunca modal: o painel atrás continua utilizável (TV)');
  assert.equal(chamadas.show, 1);
  assert.equal(partes.lista.children.length, 2);
  // "Abrir conversa" é botão, não link: fecha o aviso, dispara o evento que o
  // painel usa pra abrir a thread na lateral e adia TODAS as pendências.
  botaoAbrir(0).fire('click');
  const abrir = dispatched.find(ev => ev.type === 'atendimento:abrir');
  assert.ok(abrir && abrir.detail.conv_id === 2429, 'abre a conversa na coluna da direita');
  assert.equal(dialog.open, false);
  thread.classList.remove('hidden');  // o painel abriu a thread na coluna da direita
  await poll();
  assert.equal(dialog.open, false, 'regressão 22/09: a pendência não clicada reabria o aviso no poll seguinte');
  payload = {alertas:[alerta(2429, {grave:true}), alerta(2430), alerta(2431, {grave:true})]};
  await poll();
  assert.equal(dialog.open, false, 'pendência NOVA não abre por cima de quem está atendendo');
  await vencer(6 * 60 * 1000);
  assert.equal(dialog.open, false, 'adiamento vencido também não abre por cima da conversa aberta');
  thread.classList.add('hidden');  // voltou pra lista
  await poll();
  assert.equal(dialog.open, true, 'de volta à lista, a cobrança continua');
  assert.equal(partes.lista.children.length, 3);
  // Esc adia como o botão × (diálogo não-modal não recebe `cancel`).
  (docHandlers.keydown || []).forEach(f => f({key:'Escape'}));
  assert.equal(dialog.open, false);
  // O botão "!" (gesto manual) abre mesmo com conversa aberta.
  thread.classList.remove('hidden');
  partes.botao.fire('click');
  assert.equal(dialog.open, true, 'gesto manual continua abrindo');
  assert.equal(chamadas.showModal, 0);
  console.log('OK');
})().catch(err => {console.error(err); process.exit(1);});
'''


def test_aviso_de_pendencias_nao_bloqueia_quem_atende():
    node = shutil.which('node')
    if not node:
        pytest.skip('node ausente')
    resultado = subprocess.run([node, '-e', _HARNESS, str(_SCRIPT)],
                               capture_output=True, text=True, timeout=60)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert 'OK' in resultado.stdout
