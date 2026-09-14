"""O resumo do cliente só libera pagamento com uma agenda utilizável."""
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
function element() { return {value:'', textContent:'', disabled:false, handlers:{}, children:[],
  addEventListener(n, f) {this.handlers[n] = f;}, reportValidity() {return true;},
  setCustomValidity(v) {this.validacao = v;}, focus() {},
  fire(n, event) {return this.handlers[n].call(this, event);},
  replaceChildren() {this.children = [];}, appendChild(child) {this.children.push(child);}}; }
const rows = [], elements = {}, campos = {};
['cep','logradouro','numero','complemento','bairro','cidade','uf','csrf_token'].forEach(n => campos[n] = element());
const form = element(); form.elements = {namedItem: n => campos[n]}; elements['kit-form'] = form;
elements['kits-config'] = {textContent:JSON.stringify(cfg)};
elements['kit-agenda'] = {querySelectorAll:() => rows, appendChild:r => rows.push(r)};
elements['kit-dia-template'] = {content:{firstElementChild:{cloneNode() {
  const data = element(), janela = element(), remover = element(), numero = element(), aviso = element();
  aviso.hidden = true;
  janela.add = o => janela.children.push(o);
  return {querySelector:s => ({'.kit-data':data,'.kit-janela':janela,'.kit-remover':remover,
    '.kit-dia-numero':numero,'.kit-dia-aviso':aviso}[s]),
    remove() {rows.splice(rows.indexOf(this), 1);}};
}}}};
for (const n of ['kit-continuar','frete-aviso','kits-quantidade','kits-subtotal','kits-fretes','kits-total',
  'adicionar-data','agenda-json','cep-aviso','kit-calcular-frete','kit-preco','kit-agenda-ajuda',
  'kit-proximo-passo','kits-resumo-datas']) elements[n] = element();
let distancia = 3.4;
const sandbox = {document:{getElementById:id => elements[id], createElement:() => element()},
  Option:function(text,value) {this.text = text; this.value = value;},
  fetch:async () => ({ok:true,json:async () => ({ok:true,valor:15.25,distancia_km:distancia})})};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const date = (index, value) => {const campo = rows[index].querySelector('.kit-data'); campo.value = value; campo.fire('change');};
const time = (index, value) => {const campo = rows[index].querySelector('.kit-janela'); campo.value = value; campo.fire('change');};
const pay = elements['kit-continuar'];
(async () => {
  if (process.argv[2] === 'agenda') {
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(pay.disabled, true, 'Frete calculado não basta com entrega vazia');
    let prevented = false;
    form.fire('submit', {preventDefault() {prevented = true;}});
    assert.equal(prevented, true, 'Envio por teclado também respeita agenda incompleta');
    date(0, '2026-09-15');
    assert.equal(pay.disabled, true, 'Data sem horário ainda está incompleta');
    time(0, '10:00–11:00');
    assert.equal(pay.disabled, false);
    assert.match(elements['kits-resumo-datas'].children[0].textContent, /15.*set.*10:00/);
    elements['adicionar-data'].fire('click');
    assert.equal(pay.disabled, true, 'Nova entrega exige escolher horário');
    time(1, '10:00–11:00');
    assert.equal(pay.disabled, false);
    date(1, '2026-09-15');
    assert.equal(pay.disabled, true, 'Não pode pagar duas entregas na mesma data');
    assert.match(rows[1].querySelector('.kit-dia-aviso').textContent, /data diferente/);
    assert.equal(rows[1].querySelector('.kit-dia-aviso').hidden, false);
    date(1, '2026-09-22');
    assert.equal(pay.disabled, false);
    assert.equal(rows[1].querySelector('.kit-dia-aviso').hidden, true);
    date(1, '2026-09-23');
    assert.equal(pay.disabled, true, 'Data sem disponibilidade bloqueia pagamento');
    rows[1].querySelector('.kit-remover').fire('click');
    assert.equal(pay.disabled, false);
    assert.equal(rows[0].querySelector('.kit-dia-numero').textContent, '01');
    assert.equal(elements['kits-resumo-datas'].children.length, 1);
    assert.deepEqual(JSON.parse(elements['agenda-json'].value), [{data:'2026-09-15',janela:'10:00–11:00'}]);
  } else {
    date(0, '2026-09-15'); time(0, '09:00–10:00');
    assert.equal(pay.disabled, true, 'Endereço sem frete impede avançar');
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(pay.disabled, false);
    distancia = 12;
    await elements['kit-calcular-frete'].fire('click');
    assert.equal(rows[0].querySelector('.kit-janela').value, '');
    assert.equal(pay.disabled, true, 'Região distante exige escolher novo horário');
    time(0, '09:00–10:00');
    assert.equal(pay.disabled, true, 'Horário antigo não passa se recolocado no campo');
    assert.match(rows[0].querySelector('.kit-dia-aviso').textContent, /horário disponível/);
    assert.equal(rows[0].querySelector('.kit-dia-aviso').hidden, false);
    time(0, '10:00–11:00');
    assert.equal(pay.disabled, false);
    assert.equal(rows[0].querySelector('.kit-dia-aviso').hidden, true);
    campos.numero.fire('input');
    assert.equal(pay.disabled, true, 'Mudança no endereço invalida frete');
    assert.equal(elements['kits-fretes'].textContent, 'Informe seu endereço');
    assert.match(elements['kit-proximo-passo'].textContent, /calcule os fretes/);
  }
})().catch(e => {console.error(e); process.exit(1);});
'''


@pytest.mark.parametrize('cenario', ['agenda', 'regiao'])
def test_pagamento_exige_agenda_valida_e_frete_atualizado(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar agendamento dos kits')
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    resultado = subprocess.run([node, '-e', _HARNESS, str(script), cenario],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
