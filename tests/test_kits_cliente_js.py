"""Continuar explica pendências sem permitir um checkout inválido."""
import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cfg = {
  precoCentavos: 3000, dataMin:'2026-09-14', dataMax:'2026-10-14', corteKm:10,
  janelas:{'2026-09-15':['09:00–10:00','10:00–11:00'], '2026-09-22':['09:00–10:00','10:00–11:00']},
  janelasLonge:{'2026-09-15':['10:00–11:00'], '2026-09-22':['10:00–11:00']},
  freteUrl:'/frete', cepUrl:'/cep/00000000', agenda:[{data:'',janela:''}]
};
if (process.argv[2] === 'resumo') {
  for (const data of ['2026-09-29', '2026-10-06']) {
    cfg.janelas[data] = ['10:00–11:00']; cfg.janelasLonge[data] = ['10:00–11:00'];
  }
}
let focused = null;
function element(extra = {}) { return {value:'', textContent:'', disabled:false, handlers:{}, children:[],
  attributes:{}, hidden:false, required:false, checked:false, type:'text', id:'', name:'', tagName:'INPUT',
  addEventListener(n, f) {(this.handlers[n] ||= []).push(f);},
  setAttribute(n, v) {this.attributes[n] = String(v);}, getAttribute(n) {return this.attributes[n] ?? null;},
  removeAttribute(n) {delete this.attributes[n];},
  get validity() {
    const valueMissing = this.required && (this.type === 'checkbox' ? !this.checked : !this.value);
    const typeMismatch = this.type === 'email' && this.value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(this.value);
    const patternMismatch = !!(this.pattern && this.value && !new RegExp('^(?:' + this.pattern + ')$').test(this.value));
    const rangeUnderflow = !!(this.type === 'date' && this.value && this.min && this.value < this.min);
    const rangeOverflow = !!(this.type === 'date' && this.value && this.max && this.value > this.max);
    const customError = !!this.validacao;
    return {valueMissing, typeMismatch, patternMismatch, rangeUnderflow, rangeOverflow, customError,
      valid:this.disabled || !(valueMissing || typeMismatch || patternMismatch || rangeUnderflow || rangeOverflow || customError)};
  },
  get willValidate() {return !this.disabled && this.type !== 'hidden';},
  checkValidity() {return this.validity.valid;}, reportValidity() {return this.checkValidity();},
  setCustomValidity(v) {this.validacao = v;}, focus() {focused = this;}, scrollIntoView() {this.scrolled = true;},
  insertAdjacentElement(_, child) {this.inlineError = child;}, closest() {return this;},
  fire(n, event = {}) {
    event.target ||= this;
    const results = (this.handlers[n] || []).map(f => f.call(this, event));
    if (this !== form && (n === 'input' || n === 'change')) form.fire(n, event);
    return results.find(r => r && typeof r.then === 'function');
  },
  replaceChildren(...children) {this.children = children;}, appendChild(child) {this.children.push(child);},
  ...extra}; }
const rows = [], elements = {}, campos = {};
const values = {cep:'04567000', logradouro:'Rua de teste', numero:'12', complemento:'', bairro:'Brooklin',
  cidade:'São Paulo', uf:'SP', csrf_token:'teste', nome:'Cliente', sobrenome:'Teste',
  email:'cliente@example.invalid', telefone:'11999999999', cpf:'52998224725', aceite_lgpd:'1'};
Object.entries(values).forEach(([n, value]) => campos[n] = element({name:n, id:'kit-' + n, value,
  required:!['complemento', 'csrf_token'].includes(n), checked:n === 'aceite_lgpd',
  type:n === 'aceite_lgpd' ? 'checkbox' : n === 'email' ? 'email' : 'text'}));
const form = element();
const allFields = () => [...Object.values(campos), ...rows.flatMap(r => [r.querySelector('.kit-data'), r.querySelector('.kit-janela')])];
form.elements = {namedItem:n => campos[n], [Symbol.iterator]:function* () {yield* allFields();}};
form.querySelectorAll = () => allFields(); form.reportValidity = () => allFields().every(f => f.checkValidity());
elements['kit-form'] = form;
Object.values(campos).forEach(c => elements[c.id] = c);
elements['kits-config'] = {textContent:JSON.stringify(cfg)};
elements['kit-agenda'] = {querySelectorAll:() => rows, appendChild:r => rows.push(r)};
elements['kit-dia-template'] = {content:{firstElementChild:{cloneNode() {
  const data = element({type:'date', required:true}), janela = element({tagName:'SELECT', required:true}),
    remover = element(), numero = element(), aviso = element();
  aviso.hidden = true;
  janela.add = o => janela.children.push(o);
  return {querySelector:s => ({'.kit-data':data,'.kit-janela':janela,'.kit-remover':remover,
    '.kit-dia-numero':numero,'.kit-dia-aviso':aviso}[s]),
    remove() {rows.splice(rows.indexOf(this), 1);}};
}}}};
for (const n of ['kit-continuar','frete-aviso','kits-quantidade','kits-subtotal','kits-fretes','kits-total',
  'adicionar-data','agenda-json','cep-aviso','kit-calcular-frete','kit-preco','kit-agenda-ajuda',
  'kit-proximo-passo','kits-resumo-datas','kit-erros','kit-erros-lista']) elements[n] = element({id:n});
let distancia = 3.4;
const windowHandlers = {};
const sandbox = {document:{getElementById:id => elements[id], createElement:tag => element({tagName:tag.toUpperCase()})},
  window:{addEventListener(name, fn) {windowHandlers[name] = fn;}},
  Option:function(text,value) {this.text = text; this.value = value;},
  fetch:async () => ({ok:true,json:async () => ({ok:true,valor:15.25,distancia_km:distancia})})};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const date = (index, value) => {const campo = rows[index].querySelector('.kit-data'); campo.value = value; campo.fire('change');};
const time = (index, value) => {const campo = rows[index].querySelector('.kit-janela'); campo.value = value; campo.fire('change');};
const pay = elements['kit-continuar'];
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const errors = () => text(elements['kit-erros-lista']);
const submitBlocked = (expected, target) => {
  let prevented = false;
  form.fire('submit', {preventDefault() {prevented = true;}});
  assert.equal(prevented, true, 'Clique e envio pelo teclado impedem compra inválida');
  assert.equal(pay.disabled, false, 'Botão continua respondendo para explicar a correção');
  assert.equal(elements['kit-erros'].hidden, false);
  if (expected) assert.match(errors(), expected);
  if (target) {assert.equal(focused, target); assert.equal(target.scrolled, true);}
};
(async () => {
  assert.equal(form.noValidate, true, 'JS assume validação para tap e Enter mostrarem o mesmo resumo');
  if (process.argv[2] === 'agenda') {
    await elements['kit-calcular-frete'].fire('click');
    submitBlocked(/Entrega 1.*data/i, rows[0].querySelector('.kit-data'));
    date(0, '2026-09-15');
    submitBlocked(/Entrega 1.*horário/i, rows[0].querySelector('.kit-janela'));
    time(0, '10:00–11:00');
    assert.equal(pay.disabled, false);
    assert.match(elements['kits-resumo-datas'].children[0].textContent, /15.*set.*10:00/);
    elements['adicionar-data'].fire('click');
    submitBlocked(/Entrega 2.*horário/i, rows[1].querySelector('.kit-janela'));
    time(1, '10:00–11:00');
    assert.equal(pay.disabled, false);
    date(1, '2026-09-15');
    submitBlocked(/data diferente/i);
    assert.match(rows[1].querySelector('.kit-dia-aviso').textContent, /data diferente/);
    assert.equal(rows[1].querySelector('.kit-data').inlineError.hidden, false);
    date(1, '2026-09-22');
    assert.equal(pay.disabled, false);
    assert.equal(rows[1].querySelector('.kit-dia-aviso').hidden, true);
    date(1, '2026-09-23');
    submitBlocked(/Entrega 2.*data/i, rows[1].querySelector('.kit-data'));
    rows[1].querySelector('.kit-remover').fire('click');
    assert.equal(pay.disabled, false);
    assert.equal(rows[0].querySelector('.kit-dia-numero').textContent, '01');
    assert.equal(elements['kits-resumo-datas'].children.length, 1);
    assert.deepEqual(JSON.parse(elements['agenda-json'].value), [{data:'2026-09-15',janela:'10:00–11:00'}]);
  } else if (process.argv[2] === 'regiao') {
    date(0, '2026-09-15'); time(0, '09:00–10:00');
    submitBlocked(/fretes/i, elements['kit-calcular-frete']);
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(pay.disabled, false);
    distancia = 12;
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(rows[0].querySelector('.kit-janela').value, '');
    submitBlocked(/Entrega 1.*horário/i, rows[0].querySelector('.kit-janela'));
    time(0, '09:00–10:00');
    submitBlocked(/horário disponível/i);
    assert.match(rows[0].querySelector('.kit-dia-aviso').textContent, /horário disponível/);
    assert.equal(rows[0].querySelector('.kit-janela').inlineError.hidden, false);
    time(0, '10:00–11:00');
    assert.equal(pay.disabled, false);
    assert.equal(rows[0].querySelector('.kit-dia-aviso').hidden, true);
    campos.numero.fire('input');
    submitBlocked(/fretes/i, elements['kit-calcular-frete']);
    assert.equal(elements['kits-fretes'].textContent, 'Informe seu endereço');
    assert.match(elements['kit-proximo-passo'].textContent, /calcule os fretes/);
  } else if (process.argv[2] === 'campos') {
    date(0, '2026-09-15'); time(0, '10:00–11:00');
    await elements['kit-calcular-frete'].fire('click');
    campos.nome.value = ''; campos.nome.fire('input');
    submitBlocked(/nome/i, campos.nome);
    assert.equal(campos.nome.getAttribute('aria-invalid'), 'true');
    campos.nome.value = 'Cliente'; campos.nome.fire('input');
    assert.notEqual(campos.nome.getAttribute('aria-invalid'), 'true');
    campos.email.value = 'email-invalido'; campos.email.fire('input');
    submitBlocked(/e-mail/i, campos.email);
    campos.email.value = 'cliente@example.invalid'; campos.email.fire('input');
    campos.aceite_lgpd.checked = false; campos.aceite_lgpd.fire('change');
    submitBlocked(/termos/i, campos.aceite_lgpd);
    campos.aceite_lgpd.checked = true; campos.aceite_lgpd.fire('change');
    assert.equal(elements['kit-erros'].hidden, true, 'Corrigir termos limpa pendências');
    campos.complemento.pattern = '[A-Za-z ]+'; campos.complemento.value = '123';
    campos.complemento.fire('input');
    submitBlocked(/campo/i, elements['kit-calcular-frete']);
    assert.equal(campos.complemento.getAttribute('aria-invalid'), 'true', 'Restrição nativa opcional continua validada');
    campos.complemento.value = ''; campos.complemento.fire('input');
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(elements['kit-erros'].hidden, true, 'Backstop de validação não torna opcional obrigatório');
    let prevented = false;
    form.fire('submit', {preventDefault() {prevented = true;}});
    assert.equal(prevented, false, 'Compra válida pode avançar');
    assert.equal(pay.disabled, true, 'Somente envio em andamento desabilita botão');
    form.fire('submit', {preventDefault() {prevented = true;}});
    assert.equal(prevented, true, 'Segundo envio em andamento é impedido');
    const agendaAntes = elements['agenda-json'].value;
    windowHandlers.pageshow({persisted:true});
    assert.equal(pay.disabled, false, 'Voltar no Safari permite continuar outra vez');
    assert.equal(elements['agenda-json'].value, agendaAntes, 'Restaurar página não apaga entregas');
    assert.equal(campos.email.value, 'cliente@example.invalid');
  } else if (process.argv[2] === 'resumo') {
    date(0, '2026-09-15'); time(0, '10:00–11:00');
    for (let i = 0; i < 3; i++) elements['adicionar-data'].fire('click');
    time(1, '10:00–11:00');
    await elements['kit-calcular-frete'].fire('click');
    const antes = elements['agenda-json'].value;
    submitBlocked(/Entrega 3.*horário/i, rows[2].querySelector('.kit-janela'));
    assert.match(errors(), /Entrega 4.*horário/i);
    assert.equal(elements['kit-erros-lista'].children.length, 2, 'Não duplica erros genéricos dos campos');
    const primeiraPendencia = elements['kit-erros-lista'].children[0];
    campos.nome.fire('input');
    assert.equal(elements['kit-erros-lista'].children[0], primeiraPendencia, 'Digitar sem mudar erros não reconstrói o alerta');
    assert.equal(elements['agenda-json'].value, antes, 'Tentar continuar preserva todas as escolhas');
    elements['kit-erros-lista'].children[1].children[0].fire('click');
    assert.equal(focused, rows[3].querySelector('.kit-janela'), 'Mensagem clicável abre a entrega indicada');
    time(2, '10:00–11:00');
    submitBlocked(/Entrega 4.*horário/i, rows[3].querySelector('.kit-janela'));
    assert.doesNotMatch(errors(), /Entrega 3/);
    time(3, '10:00–11:00');
    assert.equal(elements['kit-erros'].hidden, true);
    assert.notEqual(rows[2].querySelector('.kit-janela').getAttribute('aria-invalid'), 'true');
    assert.notEqual(rows[3].querySelector('.kit-janela').getAttribute('aria-invalid'), 'true');
    assert.equal(JSON.parse(elements['agenda-json'].value).length, 4);
  } else if (process.argv[2] === 'limites') {
    await elements['kit-calcular-frete'].fire('click');
    date(0, '2026-09-13');
    submitBlocked(/Entrega 1.*data/i, rows[0].querySelector('.kit-data'));
    assert.match(errors(), /14\/09\/2026.*14\/10\/2026/);
    assert.equal(elements['kit-erros-lista'].children.length, 1, 'Corrige data antes de solicitar horário impossível');
    date(0, '2026-10-23');
    submitBlocked(/Entrega 1.*data/i, rows[0].querySelector('.kit-data'));
    assert.match(errors(), /14\/09\/2026.*14\/10\/2026/);
    assert.equal(elements['kit-erros-lista'].children.length, 1);
    date(0, '2026-09-15'); time(0, '10:00–11:00');
    elements['adicionar-data'].fire('click');
    elements['adicionar-data'].fire('click');
    elements['adicionar-data'].fire('click');
    date(1, '2026-09-22'); time(1, '10:00–11:00');
    // A numeração deve levar até a terceira entrega, não às duas já completas.
    submitBlocked(/Entrega 3.*data/i, rows[2].querySelector('.kit-data'));
    assert.equal(rows[0].querySelector('.kit-janela').value, '10:00–11:00');
    rows[2].querySelector('.kit-remover').fire('click');
    submitBlocked(/Entrega 3.*data/i, rows[2].querySelector('.kit-data'));
    assert.doesNotMatch(errors(), /Entrega 4/);
    while (rows.length) rows[0].querySelector('.kit-remover').fire('click');
    submitBlocked(/entrega/i, elements['adicionar-data']);
  }
})().catch(e => {console.error(e); process.exit(1);});
'''


@pytest.mark.parametrize('cenario', ['agenda', 'regiao', 'campos', 'limites', 'resumo'])
def test_pagamento_exige_agenda_valida_e_frete_atualizado(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar agendamento dos kits')
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    resultado = subprocess.run([node, '-e', _HARNESS, str(script), cenario],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
