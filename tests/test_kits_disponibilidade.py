"""Agenda mensal e horários especiais usam a mesma disponibilidade no navegador e no POST."""
import json
import re
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_loja_checkout import _form, _loja

from app.extensions import db
from app.models import EstoqueSitePlano, KitCafe, KitCafeItem, LojaDataEspecial, Produto
from app.services import kits_cafe, loja_catalogo, loja_checkout
from app.services.compra_kits import DIAS_AGENDA_KITS
from app.utils import agora, hoje


def _cenario(owner, *, dia=20, sob=False):
    produto = Produto(nome='Suco do mês', preco_site=10, ativo=True,
                      site_ativo=True, sob_encomenda=sob)
    kit = KitCafe(nome='Café mensal', usuario_id=owner.id, ativo=True)
    db.session.add_all([produto, kit])
    db.session.flush()
    kit.itens.append(KitCafeItem(kind='produto', produto_id=produto.id, quantidade=1))
    for indice in range(35):
        db.session.add(EstoqueSitePlano(kind='produto', item_id=produto.id,
                                       data=hoje() + timedelta(days=indice),
                                       qtd_planejada=10 if indice == dia else 0,
                                       qtd_reservada=0))
    db.session.commit()
    return kit, produto, hoje() + timedelta(days=dia)


def _cliente(app, owner):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner.id)
        sessao['_fresh'] = True
    return cliente


def test_kit_e_editor_aceitam_dia_20_sem_ampliar_catalogo_avulso(app, owner_user):
    kit, produto, _ = _cenario(owner_user)
    assert not loja_catalogo.tem_estoque_site('produto', produto.id)
    itens_avulsos, avisos = loja_checkout.montar_itens(kits_cafe.itens_do_kit(kit))
    assert not itens_avulsos and avisos
    assert kits_cafe.publicados() == [kit]
    assert kits_cafe.preco_kit(kit) == Decimal('10.00')
    cliente = _cliente(app, owner_user)
    html = cliente.get('/admin/kits-cafe/novo').get_data(as_text=True)
    assert f'name="qtd_produto_{produto.id}"' in html
    resposta = cliente.get(f'/loja/kits-cafe/{kit.id}')
    assert resposta.status_code == 200
    assert 'id="kit-form"' in resposta.get_data(as_text=True)
    selecao, erros = kits_cafe.preparar_itens([{'kind': 'produto', 'id': produto.id, 'qtd': 1}])
    assert not erros and selecao[0]['produto_id'] == produto.id


@pytest.mark.parametrize('sob,dia', [(False, 31), (True, 32)])
def test_ultimo_dia_da_agenda_tambem_conta_na_disponibilidade(app, owner_user, sob, dia):
    kit, produto, ultimo = _cenario(owner_user, dia=dia, sob=sob)
    datas = loja_checkout.datas_disponiveis('agendada', dias=DIAS_AGENDA_KITS,
                                           lead_dias=2 if sob else 0)
    assert datas[-1] == ultimo
    assert kits_cafe.publicados() == [kit]
    assert loja_catalogo.tem_estoque_site('produto', produto.id, datas=datas)


def test_kit_esgotado_no_mes_inteiro_continua_indisponivel(app, owner_user):
    kit, produto, _ = _cenario(owner_user, dia=34)
    assert kits_cafe.publicados() == []
    assert ('produto', produto.id) not in {(i['kind'], i['id']) for i in kits_cafe.catalogo_para_editor()}
    _, erros = kits_cafe.montar(kit)
    assert erros


def test_checkout_aceita_dia_20_mas_recusa_data_esgotada(app, owner_user):
    kit, _, data = _cenario(owner_user)
    _loja(db)
    dados = _form(sobrenome='Silva', modo_entrega='agendada',
                  data_entrega=data.isoformat(), janela_entrega='08:00–09:00')
    argumentos = dict(base=agora(), commit=False, reservar_estoque=False,
                      dias_agenda=DIAS_AGENDA_KITS, dias_disponibilidade=DIAS_AGENDA_KITS,
                      itens_estritos=True,
                      frete_validado=(Decimal('15.00'), 3.0, 'Rua X, 10', None))
    pedido, erros = loja_checkout.criar_pedido(dados, kits_cafe.itens_do_kit(kit), **argumentos)
    assert not erros and pedido.data_entrega == data
    assert pedido.valor_total == Decimal('25.00')
    db.session.rollback()
    dados['data_entrega'] = (data - timedelta(days=1)).isoformat()
    pedido, erros = loja_checkout.criar_pedido(dados, kits_cafe.itens_do_kit(kit), **argumentos)
    assert pedido is None and any('não está disponível' in erro for erro in erros)


def _config_horarios(app, owner):
    kit, _, especial = _cenario(owner, dia=20)
    db.session.add(LojaDataEspecial(data=especial, rotulo='Manhã especial', janelas='08:00–09:00'))
    db.session.commit()
    html = _cliente(app, owner).get(f'/loja/kits-cafe/{kit.id}').get_data(as_text=True)
    bruto = re.search(r'<script id="kits-config" type="application/json">(.*?)</script>', html, re.S)
    assert bruto, html
    return json.loads(bruto.group(1)), especial.isoformat(), (especial + timedelta(days=1)).isoformat()


def test_mapa_para_cliente_longe_preserva_janela_especial_e_corta_dia_normal(app, owner_user):
    config, especial, normal = _config_horarios(app, owner_user)
    assert config['janelasLonge'][especial] == ['08:00–09:00']
    assert config['janelas'][especial] == ['08:00–09:00']
    assert '08:00–09:00' in config['janelas'][normal]
    assert '08:00–09:00' not in config['janelasLonge'][normal]


def test_navegador_usa_horarios_do_servidor_apos_calcular_frete(app, owner_user):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para testar a seleção de horários')
    config, especial, normal = _config_horarios(app, owner_user)
    config['agenda'] = [{'data': especial, 'janela': '08:00–09:00'},
                        {'data': normal, 'janela': '08:00–09:00'}]
    script = Path(__file__).resolve().parents[1] / 'app/static/loja/kits.js'
    harness = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cfg = JSON.parse(process.argv[2]);
function element() { return {value: '', textContent: '', disabled: false, handlers: {}, attributes: {},
  addEventListener(name, fn) {this.handlers[name] = fn;}, reportValidity() {return true;},
  setAttribute(n, v) {this.attributes[n] = String(v);}, getAttribute(n) {return this.attributes[n] ?? null;},
  removeAttribute(n) {delete this.attributes[n];},
  setCustomValidity() {}, focus() {}, fire(name) {return this.handlers[name].call(this);}}; }
const rows = [], elements = {}, campos = {};
['cep','logradouro','numero','complemento','bairro','cidade','uf','csrf_token'].forEach(n => campos[n] = element());
const form = element(); form.elements = {namedItem: n => campos[n]};
elements['kit-form'] = form;
elements['kits-config'] = {textContent: JSON.stringify(cfg)};
elements['kit-agenda'] = {querySelectorAll: () => rows, appendChild: row => rows.push(row)};
elements['kit-dia-template'] = {content: {firstElementChild: {cloneNode() {
  const data = element(), janela = element(), remover = element(); janela.options = [];
  janela.replaceChildren = () => {janela.options = [];}; janela.add = option => janela.options.push(option);
  return {querySelector: s => ({'.kit-data':data,'.kit-janela':janela,'.kit-remover':remover}[s]), remove() {}};
}}}};
for (const n of ['kit-continuar','frete-aviso','kits-quantidade','kits-subtotal','kits-fretes',
  'kits-total','adicionar-data','agenda-json','cep-aviso','kit-calcular-frete']) elements[n] = element();
const sandbox = {document: {getElementById: id => elements[id]},
  window: {addEventListener() {}},
  Option: function(text, value) {this.text = text; this.value = value;},
  fetch: async () => ({ok:true, json: async () => ({ok:true, valor:15, distancia_km:20})})};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
(async () => {
  assert.equal(rows[0].querySelector('.kit-janela').value, '08:00–09:00');
  assert.equal(rows[1].querySelector('.kit-janela').value, '08:00–09:00');
  await elements['kit-calcular-frete'].fire('click');
  assert.equal(rows[0].querySelector('.kit-janela').value, '08:00–09:00', 'Data especial mantém a janela do dono');
  assert.equal(rows[1].querySelector('.kit-janela').value, '', 'Dia normal exige escolher um horário válido para longe');
  campos.cep.fire('input');
  assert.ok(rows[1].querySelector('.kit-janela').options.some(o => o.value === '08:00–09:00'));
})().catch(e => {console.error(e); process.exit(1);});
'''
    resultado = subprocess.run([node, '-e', harness, str(script), json.dumps(config)],
                               capture_output=True, text=True, check=False, timeout=15)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
