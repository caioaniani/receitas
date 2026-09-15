"""As escolhas de pães liberam uma combinação completa de preço e entregas."""
import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cenario = process.argv[2];
const normal = {
  precoCentavos:6780, dataMin:'2026-09-14', dataMax:'2026-10-14',
  janelas:{'2026-09-15':['09:00–10:00','10:00–11:00'], '2026-09-22':['09:00–10:00','10:00–11:00']},
  janelasLonge:{'2026-09-15':['10:00–11:00'], '2026-09-22':['10:00–11:00']}
};
const encomenda = {
  precoCentavos:6980, dataMin:'2026-09-16', dataMax:'2026-10-16',
  janelas:{'2026-09-17':['11:00–12:00','12:00–13:00'], '2026-09-22':['11:00–12:00','12:00–13:00']},
  janelasLonge:{'2026-09-17':['12:00–13:00'], '2026-09-22':['12:00–13:00']}
};
const cfg = {...normal, corteKm:10, freteUrl:'/frete', cepUrl:'/cep/00000000',
  grupos:['croissant','sourdough'], sucos:{11:normal, 12:encomenda},
  combinacoes:{
    '11|receita:4|receita:9':normal,
    '11|produto:6|receita:9':encomenda,
    '12|produto:6|receita:9':{...encomenda, precoCentavos:7180},
    '12|produto:6|receita:10':{...encomenda, precoCentavos:7480}
  }
};
if (cenario === 'sem_suco' || cenario === 'grupo_unico') {
  delete cfg.sucos;
  cfg.combinacoes = {'|receita:4|receita:9':normal};
  if (cenario === 'grupo_unico') {
    cfg.grupos = ['sourdough'];
    cfg.combinacoes = {'|receita:9':normal};
  }
}
if (cenario === 'restaurada') cfg.agenda = [{data:'2026-09-17',janela:'12:00–13:00'}];
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
const allFields = () => [...Object.values(campos), ...[elements['kit-suco'], ...cfg.grupos.map(g => elements['kit-escolha-' + g])].filter(Boolean),
  ...rows.flatMap(r => [r.querySelector('.kit-data'), r.querySelector('.kit-janela')])];
form.elements = {namedItem:n => campos[n], [Symbol.iterator]:function* () {yield* allFields();}};
form.querySelectorAll = () => allFields(); form.reportValidity = () => allFields().every(f => f.checkValidity());
elements['kit-form'] = form;
Object.values(campos).forEach(c => elements[c.id] = c);
elements['kits-config'] = {textContent:JSON.stringify(cfg)};
elements['kit-agenda'] = {querySelectorAll:() => rows, appendChild:r => rows.push(r)};
elements['kit-dia-template'] = {content:{firstElementChild:{cloneNode() {
  const data = element({type:'date', required:true}), janela = element({tagName:'SELECT', required:true}),
    remover = element(), numero = element(), aviso = element();
  janela.add = o => janela.children.push(o);
  return {querySelector:s => ({'.kit-data':data,'.kit-janela':janela,'.kit-remover':remover,
    '.kit-dia-numero':numero,'.kit-dia-aviso':aviso}[s]),
    remove() {rows.splice(rows.indexOf(this), 1);}};
}}}};
for (const n of ['kit-continuar','frete-aviso','kits-quantidade','kits-subtotal','kits-fretes','kits-total',
  'adicionar-data','agenda-json','cep-aviso','kit-calcular-frete','kit-preco','kit-agenda-ajuda',
  'kit-proximo-passo','kits-resumo-datas','kit-erros','kit-erros-lista']) elements[n] = element({id:n});
if (cfg.sucos) elements['kit-suco'] = element({required:true, tagName:'SELECT'});
cfg.grupos.forEach(grupo => elements['kit-escolha-' + grupo] = element({required:true, tagName:'SELECT',
  attributes:{'data-grupo':grupo, 'aria-describedby':'kit-opcoes-ajuda'}}));
if (cenario === 'restaurada') {
  elements['kit-suco'].value = '12';
  elements['kit-escolha-croissant'].value = 'produto:6';
  elements['kit-escolha-sourdough'].value = 'receita:10';
}
elements['kit-preco'].textContent = 'A partir de R$ 67,80 por kit';
let fetches = 0;
const sandbox = {document:{getElementById:id => elements[id], createElement:tag => element({tagName:tag.toUpperCase()})},
  window:{addEventListener() {}},
  Option:function(text,value) {this.text = text; this.value = value;},
  fetch:async () => {fetches++; return {ok:true,json:async () => ({ok:true,valor:15.25,distancia_km:12})};}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const escolha = (id, value) => {elements[id].value = value; elements[id].fire('change');};
const date = (i, value) => {const campo = rows[i].querySelector('.kit-data'); campo.value = value; campo.fire('change');};
const time = (i, value) => {const campo = rows[i].querySelector('.kit-janela'); campo.value = value; campo.fire('change');};
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
  if (target) assert.equal(focused, target);
};
(async () => {
  if (cenario === 'restaurada') {
    assert.match(elements['kit-preco'].textContent, /74,80/);
    assert.equal(rows[0].querySelector('.kit-data').min, '2026-09-16');
    assert.equal(rows[0].querySelector('.kit-data').value, '2026-09-17');
    assert.equal(rows[0].querySelector('.kit-janela').value, '12:00–13:00');
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(pay.disabled, false, 'Agenda preservada usa a combinação escolhida');
    assert.match(elements['kits-total'].textContent, /90,05/);
    return;
  }
  assert.equal(rows[0].querySelector('.kit-data').value, '', 'Não escolhe data antes das opções');
  assert.equal(rows[0].querySelector('.kit-data').disabled, true);
  assert.equal(elements['kits-subtotal'].textContent, 'Escolha as opções');
  assert.match(elements['kit-agenda-ajuda'].textContent, /opções do plano/);
  assert.match(elements['kit-proximo-passo'].textContent, /opções do plano/);
  await elements['kit-calcular-frete'].fire('click');
  submitBlocked(/Escolha o/, elements['kit-suco'] || elements['kit-escolha-' + cfg.grupos[0]]);
  if (cenario === 'sem_suco' || cenario === 'grupo_unico') {
    if (cenario === 'sem_suco') escolha('kit-escolha-croissant', 'receita:4');
    escolha('kit-escolha-sourdough', 'receita:9');
    date(0, '2026-09-15'); time(0, '10:00–11:00');
    assert.equal(pay.disabled, false, 'Grupo sem suco usa chave com primeiro valor vazio');
    assert.match(elements['kits-total'].textContent, /83,05/);
    assert.equal(fetches, 1);
    return;
  }
  escolha('kit-escolha-croissant', 'receita:4');
  escolha('kit-escolha-sourdough', 'receita:9');
  assert.equal(rows[0].querySelector('.kit-data').disabled, true, 'Suco também é obrigatório');
  escolha('kit-suco', '11');
  assert.equal(rows[0].querySelector('.kit-data').disabled, false);
  date(0, '2026-09-15'); time(0, '10:00–11:00');
  assert.equal(pay.disabled, false);
  if (cenario === 'obrigatorias') {
    for (const [id, valor] of [['kit-suco','11'], ['kit-escolha-croissant','receita:4'],
      ['kit-escolha-sourdough','receita:9']]) {
      escolha(id, '');
      submitBlocked(/Escolha o/, elements[id]);
      if (id !== 'kit-suco') assert.match(elements[id].getAttribute('aria-describedby'), /kit-opcoes-ajuda/);
      assert.equal(rows[0].querySelector('.kit-data').disabled, true);
      assert.equal(elements['adicionar-data'].disabled, true);
      assert.equal(elements['kits-subtotal'].textContent, 'Escolha as opções');
      assert.equal(elements['kits-total'].textContent, '—');
      assert.equal(elements['kit-preco'].textContent, 'A partir de R$ 67,80 por kit');
      escolha(id, valor); time(0, '10:00–11:00');
      assert.equal(pay.disabled, false);
      assert.notEqual(elements[id].getAttribute('aria-invalid'), 'true');
      if (id !== 'kit-suco') assert.equal(elements[id].getAttribute('aria-describedby'), 'kit-opcoes-ajuda');
    }
    escolha('kit-escolha-sourdough', 'produto:999');
    submitBlocked(/combinação não está disponível/);
    assert.equal(elements['kits-total'].textContent, '—');
    assert.equal(fetches, 1);
    return;
  }
  elements['adicionar-data'].fire('click'); time(1, '10:00–11:00');
  assert.equal(pay.disabled, false);
  assert.match(elements['kits-subtotal'].textContent, /135,60/);
  escolha('kit-escolha-croissant', 'produto:6');
  assert.match(elements['kits-subtotal'].textContent, /139,60/);
  assert.match(elements['kits-total'].textContent, /170,10/);
  assert.equal(rows[0].querySelector('.kit-data').value, '2026-09-15');
  assert.equal(rows[0].querySelector('.kit-data').min, '2026-09-16');
  assert.match(rows[0].querySelector('.kit-dia-aviso').textContent, /data entre/);
  assert.equal(rows[0].querySelector('.kit-janela').value, '');
  submitBlocked(/Entrega 1.*data entre/i, rows[0].querySelector('.kit-data'));
  date(0, '2026-09-17');
  assert.deepEqual(rows[0].querySelector('.kit-janela').children.map(o => o.value), ['', '12:00–13:00']);
  time(0, '11:00–12:00');
  submitBlocked(/horário disponível/i);
  time(0, '12:00–13:00'); time(1, '12:00–13:00');
  assert.equal(pay.disabled, false);
  escolha('kit-suco', '12');
  assert.match(elements['kits-total'].textContent, /174,10/);
  escolha('kit-escolha-sourdough', 'receita:10');
  assert.match(elements['kits-subtotal'].textContent, /149,60/);
  assert.match(elements['kits-fretes'].textContent, /30,50/);
  assert.match(elements['kits-total'].textContent, /180,10/);
  assert.match(elements['kit-preco'].textContent, /74,80/);
  assert.equal(pay.disabled, false);
  assert.equal(fetches, 1, 'Trocar opções reaproveita o frete calculado');
  assert.deepEqual(JSON.parse(elements['agenda-json'].value), [
    {data:'2026-09-17',janela:'12:00–13:00'}, {data:'2026-09-22',janela:'12:00–13:00'}]);
  campos.numero.fire('input');
  submitBlocked(/fretes/i, elements['kit-calcular-frete']);
})().catch(e => {console.error(e); process.exit(1);});
'''


@pytest.mark.parametrize('cenario', ['obrigatorias', 'troca', 'sem_suco', 'grupo_unico', 'restaurada'])
def test_escolhas_do_plano_recalculam_preco_e_disponibilidade(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar escolhas dos planos')
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    resultado = subprocess.run([node, '-e', _HARNESS, str(script), cenario],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
