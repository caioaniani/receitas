"""Adicionais no navegador: quantidade, remoção, total e calendário efetivos."""
import shutil
import subprocess
from pathlib import Path

import pytest
from test_kits_cliente_js import _HARNESS


def test_adicionais_por_kit_atualizam_total_calendario_e_post():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar adicionais dos kits')
    setup = r'''
cfg.adicionais = [
  {chave:'produto:7',kind:'produto',id:7,nome:'Cookie',precoCentavos:735,leadDias:0},
  {chave:'produto:8',kind:'produto',id:8,nome:'Menu',precoCentavos:1000,leadDias:2,
    comp:{'12':3},descricao:'3 minis'}
];
cfg.maxAdicionais = 50;
cfg.calendarios = {0:{...cfg}, 2:{leadDias:2,dataMin:'2026-09-16',dataMax:'2026-10-16',
  janelas:{'2026-09-22':['10:00–11:00']},janelasLonge:{'2026-09-22':['10:00–11:00']}}};
for (const id of ['kit-adicional-produto','kit-adicional-qtd','kit-adicional-incluir',
  'kit-adicional-descricao','kit-adicional-aviso','kit-adicionais-lista',
  'kit-adicionais-total','adicionais-json']) elements[id] = element({id});
elements['kits-config'].textContent = JSON.stringify(cfg);
'''
    checks = r'''
  const pick = (key, qtd) => {
    elements['kit-adicional-produto'].value = key;
    elements['kit-adicional-produto'].fire('change');
    elements['kit-adicional-qtd'].value = String(qtd);
    elements['kit-adicional-incluir'].fire('click');
  };
  const saved = () => JSON.parse(elements['adicionais-json'].value);
  const list = elements['kit-adicionais-lista'];
  await elements['kit-calcular-frete'].fire('click');
  date(0, '2026-09-15'); time(0, '10:00–11:00');
  elements['adicionar-data'].fire('click'); time(1, '10:00–11:00');
  pick('produto:7', 2);
  assert.match(elements['kit-preco'].textContent, /44,70/);
  assert.match(elements['kits-total'].textContent, /119,90/);
  assert.match(elements['kits-fretes'].textContent, /30,50/);
  assert.match(elements['kit-adicionais-total'].textContent, /29,40/);
  assert.deepEqual(saved(), [{kind:'produto',id:7,qtd:2}]);
  pick('produto:7', 1);
  assert.equal(list.children.length, 1, 'Repetição soma quantidade na mesma linha');
  assert.equal(saved()[0].qtd, 3);
  const qtd = list.children[0].children.at(-1).children[0].children[0];
  qtd.value = '1'; qtd.fire('input');
  assert.equal(saved()[0].qtd, 1);
  assert.match(elements['kits-total'].textContent, /105,20/);
  pick('produto:8', 1);
  assert.match(text(list), /3 minis/);
  assert.deepEqual(saved()[1].comp, {'12':3});
  assert.equal(rows[0].querySelector('.kit-data').min, '2026-09-16');
  assert.equal(rows[0].querySelector('.kit-janela').value, '');
  assert.equal(rows[1].querySelector('.kit-janela').value, '10:00–11:00');
  submitBlocked(/Entrega 1/);
  list.children[1].children.at(-1).children.at(-1).fire('click');
  assert.equal(rows[0].querySelector('.kit-data').min, '2026-09-14');
  time(0, '10:00–11:00');
  assert.match(elements['kits-total'].textContent, /105,20/);
  list.children[0].children.at(-1).children.at(-1).fire('click');
  assert.deepEqual(saved(), []);
  assert.match(elements['kits-total'].textContent, /90,50/);
  pick('produto:7', 0);
  assert.deepEqual(saved(), []);
  assert.match(elements['kit-adicional-aviso'].textContent, /inteira/);
  return;
'''
    harness = _HARNESS.replace('vm.runInNewContext(', setup + '\nvm.runInNewContext(', 1)
    harness = harness.replace('(async () => {', '(async () => {' + checks, 1)
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    result = subprocess.run([node, '-e', harness, str(script)], capture_output=True,
                            text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
