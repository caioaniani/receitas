"""O editor resume uma opção por grupo e sinaliza preços inválidos sem inventar totais."""
import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
function element(extra = {}) {
  const classes = new Set();
  return {value:'', checked:false, hidden:false, disabled:false, textContent:'', dataset:{},
    validity:{valid:true}, handlers:{}, children:[], selectors:{}, collections:{},
    classList:{toggle(name, on) {on ? classes.add(name) : classes.delete(name);},
      contains(name) {return classes.has(name);}},
    addEventListener(name, handler) {this.handlers[name] = handler;},
    fire(name) {return this.handlers[name].call(this, {target:this});},
    querySelector(selector) {return this.selectors[selector] || null;},
    querySelectorAll(selector) {return this.collections[selector] || [];},
    append(...nodes) {for (const node of nodes) this.children.push(...(node.fragment ? node.children : [node]));},
    replaceChildren(...nodes) {this.children = []; this.append(...nodes);},
    get childElementCount() {return this.children.length;}, ...extra};
}
function fixed(kind, id, nome, preco, quantidade) {
  const input = element({value:quantidade});
  const minus = element({dataset:{step:'-1'}}), plus = element({dataset:{step:'1'}});
  return element({dataset:{kind, id, nome, preco, busca:nome, indisponivel:'0'},
    selectors:{'.kit-quantidade':input, '[data-step="-1"]':minus, '[data-step="1"]':plus},
    collections:{'[data-step]':[minus,plus]}});
}
function option(value, nome, preco) {
  const input = element({value, checked:true, dataset:{nome, preco, indisponivel:'0'}});
  const label = element({dataset:{busca:nome}});
  return {input, label};
}
function group(nome, options) {
  return element({dataset:{nome}, options,
    selectors:{'.kits-option-count':element(), '.kit-busca-grupo':element(), '.kit-grupo-vazio':element()},
    collections:{'input[type="checkbox"]':options.map(o => o.input),
      '.kit-opcao-grupo':options.map(o => o.label)}});
}
const rows = [fixed('receita','1','Manteiga','9.90','2'), fixed('produto','7','Pão de queijo','12.00','0')];
const juices = [option('11','Suco de laranja','48.00'), option('12','Suco verde','50.00')];
const croissant = group('Croissant', [option('receita:4','Croissant amêndoas','18.00'),
  option('produto:6','Croissant chocolate','22.00')]);
const sourdough = group('Sourdough', [option('receita:9','Sourdough tradicional','30.00'),
  option('receita:10','Sourdough integral','35.00')]);
const groups = [croissant, sourdough];
const elements = {};
for (const id of ['kit-total','kit-resumo-itens','kit-resumo-vazio','kit-nome','kit-busca',
  'kit-so-selecionados','kit-sem-resultado','kit-sucos-contagem','kit-resumo-aviso',
  'kit-resumo-nome','kit-busca-sucos','kit-sem-sucos']) elements[id] = element();
elements['kit-editor'] = element({collections:{'.kit-product':rows,
  '#kit-sucos input[name="suco_ids"]':juices.map(o => o.input), '[data-opcoes-grupo]':groups}});
elements['kit-sucos'] = element({collections:{'.kit-opcao-suco':juices.map(o => o.label)}});
const sandbox = {document:{getElementById:id => elements[id], createElement:() => element(),
  createDocumentFragment:() => element({fragment:true}),
  querySelectorAll:selector => selector === '.kit-opcao-suco'
    ? [...juices, ...groups.flatMap(g => g.options)].map(o => o.label) : []}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const total = elements['kit-total'], aviso = elements['kit-resumo-aviso'];
const summary = () => elements['kit-resumo-itens'].children.map(li => li.children[1].textContent);
const refresh = () => croissant.options[0].input.fire('change');
function valid(price, varies = true) {
  assert.equal(total.textContent.replace(/\s+/g, ' '), (varies ? 'A partir de ' : '') + 'R$ ' + price);
  assert.equal(total.classList.contains('is-incomplete'), false);
  assert.equal(aviso.hidden, true);
}
function invalid(message) {
  assert.equal(total.textContent, 'Revise os itens e as opções');
  assert.equal(total.textContent.includes('R$'), false, 'Preço inválido não anuncia total monetário');
  assert.equal(total.classList.contains('is-incomplete'), true);
  assert.equal(aviso.hidden, false);
  assert.match(aviso.textContent, message);
}
valid('115,80');
assert.deepEqual(summary(), ['Manteiga','Suco à escolha (2 opções)',
  'Croissant à escolha (2 opções)','Sourdough à escolha (2 opções)']);
assert.equal(elements['kit-resumo-vazio'].hidden, true);
assert.equal(croissant.querySelector('.kits-option-count').textContent, '2 selecionadas');
assert.equal(sourdough.querySelector('.kits-option-count').textContent, '2 selecionadas');
const cenario = process.argv[2];
if (cenario === 'preco_minimo') {
  for (const options of [juices, croissant.options, sourdough.options]) {
    options[1].input.dataset.preco = options[0].input.dataset.preco;
    options[1].input.fire('change');
  }
  valid('115,80', false);
  const quantidade = rows[0].querySelector('.kit-quantidade');
  quantidade.value = '3'; quantidade.fire('input');
  valid('125,70', false);
  sourdough.options[1].input.dataset.preco = '42.00';
  sourdough.options[1].input.fire('change');
  valid('125,70', true);
} else if (cenario === 'remocao') {
  for (const group of groups) {
    group.options.forEach(o => {o.input.checked = false;}); refresh();
    assert.equal(group.querySelector('.kits-option-count').textContent, '0 selecionadas');
    assert.equal(summary().some(text => text.startsWith(group.dataset.nome)), false);
  }
  valid('67,80');
  juices.forEach(o => {o.input.checked = false;}); juices[0].input.fire('change');
  valid('19,80', false);
  assert.deepEqual(summary(), ['Manteiga']);
} else if (cenario === 'grupo_unico') {
  croissant.options[1].input.checked = false; refresh();
  invalid(/Croissant: selecione de 2 a 10 opções/);
  assert.equal(croissant.querySelector('.kits-option-count').textContent, '1 selecionada');
  croissant.options[0].input.checked = false; refresh();
  valid('97,80');
} else if (cenario === 'sem_preco') {
  for (const value of ['', '0', '-2', 'inválido']) {
    sourdough.options[0].input.dataset.preco = value; refresh();
    invalid(/Sourdough: há opções sem preço ou indisponíveis/);
  }
  sourdough.options[0].input.dataset.preco = '30.00';
  sourdough.options[0].input.dataset.indisponivel = '1'; refresh();
  invalid(/Sourdough: há opções sem preço ou indisponíveis/);
  sourdough.options[0].input.dataset.indisponivel = '0'; refresh();
  valid('115,80');
} else if (cenario === 'duplicados') {
  croissant.options[1].input.value = 'receita:1'; refresh();
  invalid(/Manteiga: está nos itens fixos e nas opções/);
  croissant.options[1].input.value = 'produto:6'; refresh(); valid('115,80');
  sourdough.options[1].input.value = 'receita:4'; refresh();
  invalid(/selecionado em mais de um grupo/);
  sourdough.options[1].input.value = 'produto:11'; refresh();
  invalid(/selecionado em mais de um grupo/);
  sourdough.options[1].input.value = 'receita:10'; refresh(); valid('115,80');
} else if (cenario === 'filtros') {
  elements['kit-busca-sucos'].value = 'laranja'; elements['kit-busca-sucos'].fire('input');
  assert.equal(juices[0].label.hidden, false); assert.equal(juices[1].label.hidden, true);
  assert.equal(groups.every(g => g.options.every(o => !o.label.hidden)), true,
    'Busca de suco não oculta opções de croissant ou sourdough');
  const buscaCroissant = croissant.querySelector('.kit-busca-grupo');
  buscaCroissant.value = 'AMENDOAS'; buscaCroissant.fire('input');
  assert.equal(croissant.options[0].label.hidden, false);
  assert.equal(croissant.options[1].label.hidden, true);
  assert.equal(sourdough.options.every(o => !o.label.hidden), true);
  assert.equal(juices[0].label.hidden, false); assert.equal(juices[1].label.hidden, true);
  buscaCroissant.value = 'inexistente'; buscaCroissant.fire('input');
  assert.equal(croissant.querySelector('.kit-grupo-vazio').hidden, false);
  elements['kit-busca-sucos'].value = 'inexistente'; elements['kit-busca-sucos'].fire('input');
  assert.equal(elements['kit-sem-sucos'].hidden, false);
  valid('115,80');
  assert.equal(summary().length, 4, 'Filtrar não altera itens selecionados ou total');
}
'''


@pytest.mark.parametrize('cenario', ['preco_minimo', 'remocao', 'grupo_unico', 'sem_preco',
                                    'duplicados', 'filtros'])
def test_editor_resume_e_valida_opcoes_dos_grupos(cenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar o resumo administrativo dos kits')
    script = Path(__file__).resolve().parents[1] / 'app/static/js/kits-cafe-admin.js'
    resultado = subprocess.run([node, '-e', _HARNESS, str(script), cenario],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
