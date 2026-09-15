"""Escolha pública de suco: preços, datas e persistência de cada entrega."""
import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from test_kits_checkout import _abrir, _agenda, _post
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import frete as frete
from test_kits_checkout import kit as kit
from test_kits_cliente_imagens import _secao_kits

from app.extensions import db
from app.models import CompraKit, EstoqueLoja, KitCafeSuco, PedidoOnline, Produto

pytestmark = pytest.mark.loja_host


@pytest.fixture
def kit_sucos(kit, loja, congela_hoje):
    congela_hoje(2026, 9, 14, 6)
    laranja = db.session.get(Produto, kit.itens[1].produto_id)
    laranja.nome = 'Suco de laranja 1 L'
    laranja.preco_site = Decimal('48.00')
    verde = Produto(nome='Suco verde 1 L', ativo=True, site_ativo=True,
                    preco_site=Decimal('50.00'))
    db.session.add(verde)
    db.session.flush()
    kit.itens.pop(1)
    kit.sucos.extend([KitCafeSuco(produto_id=laranja.id), KitCafeSuco(produto_id=verde.id)])
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=verde.id,
                               quantidade=20, quantidade_reservada=0))
    db.session.commit()
    return kit, laranja, verde


def _config(html):
    return json.loads(re.search(r'<script id="kits-config"[^>]*>(.*?)</script>', html, re.S).group(1))


def test_catalogo_anuncia_um_suco_sem_duplica_lo_como_item_fixo(app, kit_sucos):
    kit, laranja, verde = kit_sucos
    html = _secao_kits(app.test_client().get('/loja/').get_data(as_text=True))
    assert '1 suco à escolha: Suco de laranja 1 L ou Suco verde 1 L' in html
    assert 'A partir de R$ 67,80 por kit' in html
    assert '1× Suco' not in html
    verde.preco_site = laranja.preco_site
    db.session.commit()
    html = _secao_kits(app.test_client().get('/loja/').get_data(as_text=True))
    assert 'A partir de' not in html


def test_get_nao_seleciona_suco_e_informa_preco_incluido_para_cada_opcao(app, kit_sucos):
    kit, laranja, verde = kit_sucos
    _, _, html = _abrir(app, kit)
    seletor = re.search(r'<select name="suco_id".*?</select>', html, re.S).group(0)
    assert 'required' in seletor and 'selected' not in seletor
    assert 'R$ 48,00 incluídos no kit' in seletor
    assert 'R$ 50,00 incluídos no kit' in seletor
    assert 'A mesma escolha de suco vale para todas as entregas' in html
    config = _config(html)
    assert config['sucos'][str(laranja.id)]['precoCentavos'] == 6780
    assert config['sucos'][str(verde.id)]['precoCentavos'] == 6980


@pytest.mark.parametrize('escolha', [None, '', '99999', '01'])
def test_post_sem_escolha_valida_nao_cria_pedidos(app, kit_sucos, frete, escolha):
    kit, _, _ = kit_sucos
    cliente, token, _ = _abrir(app, kit)
    dados = {} if escolha is None else {'suco_id': escolha}
    resposta = _post(cliente, kit, token, **dados)
    assert resposta.status_code == 400
    assert 'Escolha um dos sucos' in resposta.get_data(as_text=True)
    assert CompraKit.query.count() == PedidoOnline.query.count() == 0
    frete.assert_not_called()


def test_post_usa_suco_escolhido_e_mesmo_preco_em_todas_as_entregas(app, kit_sucos, frete):
    kit, laranja, verde = kit_sucos
    cliente, token, _ = _abrir(app, kit)
    resposta = _post(cliente, kit, token, suco_id=str(verde.id), preco='0.01')
    assert resposta.status_code == 302
    compra = CompraKit.query.one()
    assert compra.subtotal == Decimal('139.60')
    assert compra.frete_total == Decimal('30.50')
    assert compra.valor_total == Decimal('170.10')
    for entrega in compra.entregas:
        itens = entrega.pedido.itens
        assert len(itens) == 2
        assert all(it.produto_id != laranja.id for it in itens)
        suco = next(it for it in itens if it.produto_id == verde.id)
        assert suco.quantidade == 1 and suco.preco_unitario == Decimal('50.00')


def test_erro_preserva_escolha_dados_agenda_e_preco_correspondente(app, kit_sucos, frete):
    kit, _, verde = kit_sucos
    cliente, token, _ = _abrir(app, kit)
    agenda = _agenda(1, 1)
    resposta = _post(cliente, kit, token, agenda=agenda, suco_id=str(verde.id), nome='Catarina')
    assert resposta.status_code == 400
    html = resposta.get_data(as_text=True)
    assert f'<option value="{verde.id}" selected>' in html
    assert 'value="Catarina"' in html
    assert 'R$ 69,80 por kit' in html
    config = _config(html)
    assert config['agenda'] == agenda
    assert config['precoCentavos'] == 6980


def test_agenda_de_cada_suco_respeita_sua_antecedencia(app, kit_sucos):
    kit, laranja, verde = kit_sucos
    verde.sob_encomenda = True
    db.session.commit()
    _, _, html = _abrir(app, kit)
    config = _config(html)
    normal = config['calendarios'][str(config['sucos'][str(laranja.id)]['leadDias'])]
    encomenda = config['calendarios'][str(config['sucos'][str(verde.id)]['leadDias'])]
    assert normal['dataMin'] == '2026-09-14'
    assert encomenda['dataMin'] == '2026-09-16'
    assert '2026-09-15' in normal['janelas']
    assert '2026-09-15' not in encomenda['janelas']
    assert normal['dataMax'] == '2026-10-15'
    assert encomenda['dataMax'] == '2026-10-16'


def test_navegador_recalcula_preco_e_datas_ao_trocar_suco(app, kit_sucos, tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar seleção de sucos')
    kit, laranja, verde = kit_sucos
    verde.sob_encomenda = True
    db.session.commit()
    _, _, html = _abrir(app, kit)
    config = _config(html)
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    harness = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')), [laranja, verde] = Object.keys(cfg.sucos);
function element() { return {value:'', textContent:'', disabled:false, handlers:{}, attributes:{},
  addEventListener(n, f) {this.handlers[n] = f;}, reportValidity() {return true;},
  setAttribute(n, v) {this.attributes[n] = String(v);}, getAttribute(n) {return this.attributes[n] ?? null;},
  removeAttribute(n) {delete this.attributes[n];},
  setCustomValidity(v) {this.validacao = v;}, focus() {}, fire(n) {return this.handlers[n].call(this);}}; }
const rows = [], elements = {}, campos = {};
['cep','logradouro','numero','complemento','bairro','cidade','uf','csrf_token'].forEach(n => campos[n] = element());
const form = element(); form.elements = {namedItem: n => campos[n]}; elements['kit-form'] = form;
elements['kits-config'] = {textContent:JSON.stringify(cfg)};
elements['kit-agenda'] = {querySelectorAll:() => rows, appendChild:r => rows.push(r)};
elements['kit-dia-template'] = {content:{firstElementChild:{cloneNode() {
  const data = element(), janela = element(), remover = element(); janela.options = [];
  janela.replaceChildren = () => {janela.options = [];}; janela.add = o => janela.options.push(o);
  return {querySelector:s => ({'.kit-data':data,'.kit-janela':janela,'.kit-remover':remover}[s]), remove() {}};
}}}};
for (const n of ['kit-continuar','frete-aviso','kits-quantidade','kits-subtotal','kits-fretes','kits-total',
  'adicionar-data','agenda-json','cep-aviso','kit-calcular-frete','kit-suco','kit-preco']) elements[n] = element();
elements['kit-preco'].textContent = 'A partir de R$ 67,80 por kit';
const sandbox = {document:{getElementById:id => elements[id]},
  window:{addEventListener() {}},
  Option:function(text,value) {this.text = text; this.value = value;},
  fetch:async () => ({ok:true,json:async () => ({ok:true,valor:15.25,distancia_km:3.4})})};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
(async () => {
  assert.equal(elements['kit-suco'].value, '');
  assert.equal(elements['kits-subtotal'].textContent, 'Escolha o suco');
  assert.equal(rows[0].querySelector('.kit-data').disabled, true);
  await elements['kit-calcular-frete'].fire('click');
  assert.equal(elements['kit-continuar'].disabled, false, 'Continuar responde ao toque mesmo faltando escolhas');
  elements['kit-suco'].value = laranja; elements['kit-suco'].fire('change');
  const dia = rows[0].querySelector('.kit-data');
  assert.equal(dia.disabled, false); assert.equal(dia.min, '2026-09-14');
  dia.value = '2026-09-15'; dia.fire('change');
  rows[0].querySelector('.kit-janela').value = '09:00–10:00';
  elements['adicionar-data'].fire('click');
  assert.match(elements['kits-subtotal'].textContent, /135,60/);
  assert.match(elements['kits-fretes'].textContent, /30,50/);
  assert.match(elements['kits-total'].textContent, /166,10/);
  elements['kit-suco'].value = verde; elements['kit-suco'].fire('change');
  assert.match(elements['kits-subtotal'].textContent, /139,60/);
  assert.match(elements['kits-total'].textContent, /170,10/);
  assert.match(elements['kit-preco'].textContent, /69,80/);
  assert.equal(dia.min, '2026-09-16'); assert.equal(dia.value, '2026-09-15');
  assert.match(dia.validacao, /data entre/);
  assert.equal(rows[0].querySelector('.kit-janela').value, '');
  elements['kit-suco'].value = ''; elements['kit-suco'].fire('change');
  assert.equal(elements['kit-continuar'].disabled, false, 'Resumo explica pendência sem botão inerte');
  assert.equal(elements['kits-total'].textContent, '—');
  assert.equal(elements['kit-preco'].textContent, 'A partir de R$ 67,80 por kit');
})().catch(e => {console.error(e); process.exit(1);});
'''
    config_path = tmp_path / 'kits-sucos-config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    resultado = subprocess.run([node, '-e', harness, str(script), str(config_path)],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
