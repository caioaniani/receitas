"""Seletor visual de adicionais: diálogo, quantidades, total e calendário."""
import shutil
import subprocess
from pathlib import Path

import pytest
from test_kits_cliente_js import _HARNESS

_DIALOG_SETUP = r'''
cfg.adicionais = [
  {chave:'produto:7',kind:'produto',id:7,nome:'Cookie',precoCentavos:735,leadDias:0},
  {chave:'produto:8',kind:'produto',id:8,nome:'Menu',precoCentavos:1000,leadDias:2,
    comp:{'12':3},descricao:'3 minis'},
  {chave:'produto:9',kind:'produto',id:9,nome:'Pão de Queijo',precoCentavos:900,leadDias:0}
];
cfg.maxAdicionais = 50;
cfg.calendarios = {0:{...cfg}, 2:{leadDias:2,dataMin:'2026-09-16',dataMax:'2026-10-16',
  janelas:{'2026-09-22':['10:00–11:00']},janelasLonge:{'2026-09-22':['10:00–11:00']}}};
const baseElement = element;
element = function(extra = {}) {
  const classes = new Set();
  return baseElement({dataset:{},
    closest(selector) {return selector === '[hidden]' ? (this.hidden ? this : null) : this;},
    classList:{add(...names) {names.forEach(n => classes.add(n));},
      remove(...names) {names.forEach(n => classes.delete(n));},
      contains(n) {return classes.has(n);},
      toggle(n, force) {const add = force === undefined ? !classes.has(n) : force;
        if (add) classes.add(n); else classes.delete(n); return add;}},
    ...extra});
};
for (const id of ['kit-adicional-abrir','kit-adicional-fechar','kit-adicionais-dialog',
  'kit-adicional-qtd','kit-adicional-incluir','kit-adicional-busca','kit-adicional-vazio',
  'kit-adicional-selecao','kit-adicional-descricao','kit-adicional-dialog-aviso',
  'kit-adicional-aviso','kit-adicionais-lista',
  'kit-adicionais-total','adicionais-json']) elements[id] = element({id});
const dialog = elements['kit-adicionais-dialog'];
dialog.tagName = 'DIALOG'; dialog.open = false;
dialog.showModal = function() {this.open = true; this.setAttribute('open', '');};
dialog.close = function() {this.open = false; this.removeAttribute('open'); this.fire('close');};
dialog.getBoundingClientRect = () => ({left:100,right:900,top:100,bottom:700});
const cards = cfg.adicionais.map(item => element({tagName:'BUTTON',type:'button',
  className:'kits-product-card',textContent:item.nome,dataset:{chave:item.chave,nome:item.nome},
  children:[element({className:'kits-product-action',textContent:'Selecionar'})],
  querySelector(selector) {return selector === '.kits-product-action' ? this.children[0] : null;},
  getAttribute(n) {return n === 'data-chave' ? this.dataset.chave : this.attributes[n] ?? null;},
  closest(selector) {return selector === '[hidden]' ? (this.hidden ? this : null)
    : selector.includes('kits-product-card') || selector.includes('data-chave') ? this : null;}}));
const matchingCards = selector => cards.filter(card => {
  if (!(selector.includes('kits-product-card') || selector.includes('data-chave'))) return false;
  const key = selector.match(/data-chave=['"]([^'"]+)['"]/);
  return !key || key[1] === card.dataset.chave;
});
const dialogControls = [elements['kit-adicional-fechar'], elements['kit-adicional-busca'],
  ...cards, elements['kit-adicional-qtd'], elements['kit-adicional-incluir']];
dialog.querySelectorAll = selector => selector === 'button:not([disabled]), input:not([disabled])'
  ? dialogControls.filter(control => !control.disabled) : matchingCards(selector);
dialog.querySelector = selector => matchingCards(selector)[0] || null;
dialog.children = cards;
sandbox.document.querySelectorAll = matchingCards;
sandbox.document.querySelector = selector => matchingCards(selector)[0] || null;
sandbox.document.body = element({tagName:'BODY'});
Object.defineProperty(sandbox.document, 'activeElement', {get() {return focused;}});
for (const card of cards) {
  const fire = card.fire;
  card.fire = function(name, event = {}) {
    event.target ||= this;
    const result = fire.call(this, name, event);
    if (name === 'click') dialog.fire(name, event);
    return result;
  };
}
elements['kit-adicional-qtd'].value = '1';
elements['kit-adicional-incluir'].disabled = true;
elements['kit-adicional-vazio'].hidden = true;
elements['kits-config'].textContent = JSON.stringify(cfg);
'''

_HELPERS = r'''
  const saved = () => JSON.parse(elements['adicionais-json'].value);
  const list = elements['kit-adicionais-lista'];
  const open = () => {elements['kit-adicional-abrir'].fire('click'); assert.equal(dialog.open, true);};
  const select = key => cards.find(card => card.dataset.chave === key).fire('click');
  const pick = (key, qtd) => {
    open();
    select(key);
    elements['kit-adicional-qtd'].value = String(qtd);
    elements['kit-adicional-incluir'].fire('click');
  };
  const search = value => {
    elements['kit-adicional-busca'].value = value;
    elements['kit-adicional-busca'].fire('input');
  };
'''


def _executar(checks, setup_extra=''):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar adicionais dos kits')
    setup = _DIALOG_SETUP + setup_extra + '\nelements["kits-config"].textContent = JSON.stringify(cfg);'
    harness = _HARNESS.replace('vm.runInNewContext(', setup + '\nvm.runInNewContext(', 1)
    harness = harness.replace('(async () => {', '(async () => {' + _HELPERS + checks + '\nreturn;', 1)
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    result = subprocess.run([node, '-e', harness, str(script)], capture_output=True,
                            text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_adicionais_por_kit_atualizam_total_calendario_e_post():
    _executar(r'''
  await elements['kit-calcular-frete'].fire('click');
  date(0, '2026-09-15'); time(0, '10:00–11:00');
  elements['adicionar-data'].fire('click'); time(1, '10:00–11:00');
  pick('produto:7', 2);
  assert.equal(dialog.open, false, 'Incluir fecha o seletor e aplica os adicionais');
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
  assert.equal(dialog.open, true, 'Quantidade inválida mantém o seletor aberto para correção');
  assert.match(elements['kit-adicional-dialog-aviso'].textContent, /inteira/);
''')


def test_dialogo_mostra_selecao_e_cancelar_nao_altera_adicionais():
    _executar(r'''
  assert.equal(elements['kit-adicional-produto'], undefined, 'Não depende do select antigo');
  open();
  assert.equal(elements['kit-adicional-incluir'].disabled, true, 'Escolha de produto vem antes de incluir');
  select('produto:8');
  assert.equal(elements['kit-adicional-incluir'].disabled, false);
  const selection = text(elements['kit-adicional-selecao']);
  assert.match(selection, /Menu/);
  assert.match(selection, /10,00/);
  assert.match(text(elements['kit-adicional-descricao']), /3 minis/);
  assert.deepEqual(saved(), [], 'Clicar na foto/cartão ainda não adiciona ao pedido');
  elements['kit-adicional-qtd'].value = '4';
  elements['kit-adicional-fechar'].fire('click');
  assert.equal(dialog.open, false);
  assert.deepEqual(saved(), []);
  open();
  select('produto:7');
  elements['kit-adicional-qtd'].value = '2';
  elements['kit-adicional-incluir'].fire('click');
  assert.deepEqual(saved(), [{kind:'produto',id:7,qtd:2}]);
  open();
  select('produto:8');
  // Escape no dialog nativo emite cancel; o navegador fecha se não houver preventDefault.
  let prevented = false;
  dialog.fire('cancel', {preventDefault() {prevented = true;}});
  if (!prevented) dialog.close();
  assert.equal(dialog.open, false);
  assert.deepEqual(saved(), [{kind:'produto',id:7,qtd:2}]);
''')


def test_busca_filtra_cartoes_e_explica_quando_nao_encontra():
    _executar(r'''
  open();
  search('COOK');
  assert.deepEqual(cards.filter(card => !card.hidden).map(card => card.dataset.chave), ['produto:7']);
  assert.equal(elements['kit-adicional-vazio'].hidden, true);
  search('produto inexistente');
  assert.equal(cards.filter(card => !card.hidden).length, 0);
  assert.equal(elements['kit-adicional-vazio'].hidden, false);
  search('');
  assert.equal(cards.filter(card => !card.hidden).length, 3);
  assert.equal(elements['kit-adicional-vazio'].hidden, true);
  assert.deepEqual(saved(), [], 'A busca não altera a composição do pedido');
''')


def test_escape_no_campo_de_busca_fecha_dialogo_sem_alterar_pedido():
    _executar(r'''
  pick('produto:7', 2);
  for (const termo of ['Cookie', '']) {
    open();
    search(termo);
    elements['kit-adicional-busca'].focus();
    let prevented = false;
    dialog.fire('keydown', {key:'Escape', target:elements['kit-adicional-busca'],
      preventDefault() {prevented = true;}});
    assert.equal(prevented, true, 'Escape não pode ser consumido pelo input search');
    assert.equal(dialog.open, false, 'Escape fecha em vez de apenas limpar a busca');
    assert.equal(sandbox.document.body.classList.contains('kits-dialog-open'), false);
    assert.equal(focused, elements['kit-adicional-abrir']);
    assert.deepEqual(saved(), [{kind:'produto',id:7,qtd:2}]);
  }
''')


def test_tab_e_shift_tab_permanecem_nos_controles_visiveis_do_dialogo():
    _executar(r'''
  const tab = (control, shiftKey, expected) => {
    control.focus();
    let prevented = false;
    dialog.fire('keydown', {key:'Tab', shiftKey, target:control,
      preventDefault() {prevented = true;}});
    assert.equal(prevented, true, 'Tab no limite não sai para os controles do navegador');
    assert.equal(focused, expected);
  };
  const fechar = elements['kit-adicional-fechar'], incluir = elements['kit-adicional-incluir'];
  open();
  assert.equal(incluir.disabled, true);
  assert.equal(elements['kit-adicional-qtd'].disabled, true);
  tab(fechar, true, cards[2]);
  tab(cards[2], false, fechar);
  search('Cookie');
  tab(fechar, true, cards[0]);
  tab(cards[0], false, fechar);
  search('produto inexistente');
  tab(fechar, true, elements['kit-adicional-busca']);
  tab(elements['kit-adicional-busca'], false, fechar);
  search('');
  select('produto:7');
  assert.equal(incluir.disabled, false);
  tab(incluir, false, fechar);
  tab(fechar, true, incluir);
  assert.deepEqual(saved(), [], 'Navegação de teclado não inclui um produto por conta própria');
''')


@pytest.mark.parametrize('quantidade', ['0', '-1', '1.5', '1000', 'abc', ''])
def test_quantidade_invalida_nao_fecha_dialogo_nem_adiciona(quantidade):
    _executar(r'''
  pick('produto:7', quantidade);
  assert.equal(dialog.open, true);
  assert.deepEqual(saved(), []);
  assert.match(elements['kit-adicional-dialog-aviso'].textContent, /inteira/);
''', setup_extra=f'const quantidade = {quantidade!r};')


def test_limites_mantem_composicao_existente_e_permite_corrigir_quantidade():
    _executar(r'''
  pick('produto:7', 998);
  pick('produto:7', 2);
  assert.equal(dialog.open, true);
  assert.equal(saved()[0].qtd, 998);
  assert.match(elements['kit-adicional-dialog-aviso'].textContent, /Limite/);
  elements['kit-adicional-qtd'].value = '1';
  elements['kit-adicional-incluir'].fire('click');
  assert.equal(dialog.open, false);
  assert.equal(saved()[0].qtd, 999);
  pick('produto:8', 1);
  assert.equal(saved().length, 2);
  assert.deepEqual(saved()[1].comp, {'12':3});
  pick('produto:9', 1);
  assert.equal(dialog.open, true);
  assert.equal(saved().length, 2);
  assert.match(elements['kit-adicional-dialog-aviso'].textContent, /Limite/);
''', setup_extra='cfg.maxAdicionais = 2;')
