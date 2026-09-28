"""Auditoria do checkout (27/09/2026) — bloco fiscal / CNPJ.

1. A consulta pública de CNPJ preenchia e ASSINAVA razão social/endereço/
   complemento/bairro/cidade como vinham da base pública, sem respeitar os
   limites do Tiny. A tela preenche por atribuição (o ``maxlength`` não
   corta), o POST recusava pelo limite e, se o cliente encurtasse à mão, o
   dado deixava de bater com o assinado — a compra virava declaração.
   Agora o servidor corta ANTES de assinar: assinado = preenchido =
   comparado, e a resposta diz o que foi abreviado. As mensagens de
   pendência usam os rótulos da tela, nunca a chave interna.
2. CNPJ alfanumérico (IN RFB 2.229/2024, novas inscrições desde julho de
   2026) era recusado: o documento era reduzido a dígitos antes de validar.
   Helper canônico único em ``app.utils`` (normalizar/validar), usado no
   checkout, no cadastro fiscal, na consulta pública, no Tiny e no JS.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

CNPJ_ALFA = '12ABC34501DE35'          # exemplo oficial da Receita Federal
CNPJ_ALFA_MASCARA = '12.abc.345/01de-35'
CNPJ_NUM = '11222333000181'

_RAIZ = Path(__file__).resolve().parents[1]


# ── Helpers ─────────────────────────────────────────────────────────────

def _consulta_longa():
    """Dados como a base pública devolve: vários campos acima do Tiny."""
    return {
        'dados': {
            'nome': ('PANIFICADORA E CONFEITARIA ARTESANAL DO BROOKLIN '
                     'COMERCIO DE ALIMENTOS LTDA'),
            'endereco': ('Avenida Engenheiro Luiz Carlos Berrini Lado Impar '
                         'Trecho Norte'),
            'numero': '1500',
            'complemento': ('Conjunto 42 Bloco B Torre Sul Edificio Comercial '
                            'Berrini Center'),
            'bairro': 'Cidade Moncoes e Vila Olimpia Sul',
            'cidade': 'Sao Paulo Capital do Estado de SP',
            'uf': 'sp',
            'cep': '04571-000',
            'ie': '110.042.490.114',
            'situacao_ie': 'contribuinte',
        },
        'origem': 'cnpj_ws',
        'atualizado_em': '2026-09-01T10:00:00',
        'aviso': '',
    }


def _form_da_resposta(resposta, **extra):
    """O que a tela devolve no POST quando o cliente só confirma."""
    from app.services.checkout_fiscal import CAMPOS
    dados = resposta['dados']
    form = {'fiscal_checkout': '1', 'fiscal_confirmado': '1',
            'fiscal_token': resposta['token'],
            'fiscal_situacao_ie': dados['situacao_ie']}
    form.update({'fiscal_' + campo: dados[campo] for campo in CAMPOS})
    form.update(extra)
    return form


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _loja(db, nome='Brooklin'):
    from app.models import AppConfig, EstoqueLoja, Loja, Produto
    loja = Loja(nome=nome, endereco='Ribeiro do Vale, 455', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    for pr in Produto.query.all():
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=pr.id,
                                   quantidade=999))
    db.session.commit()
    return loja


def _form_retirada(loja, data, cpf, email='pj@x.com', **extra):
    form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': email,
            'cpf': cpf, 'aceite_lgpd': '1',
            'modo_entrega': 'retirada', 'loja_id': str(loja.id),
            'data_entrega': data, 'janela_entrega': '08:00–09:00',
            'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
            'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}
    form.update(extra)
    return form


def _pedido_confirmado(db, documento, dados, **extra):
    """Pedido pago com cadastro fiscal JÁ conferido pelo dono."""
    from app.models import Cliente, FiscalPedidoOnline, PedidoOnline
    from app.utils import agora
    cli = Cliente(nome='Padoca LTDA', email='pj-alfa@x.com', cpf=documento)
    db.session.add(cli)
    db.session.flush()
    ped = PedidoOnline(cliente_id=cli.id, nome_cliente='Padoca LTDA',
                       email_cliente='pj-alfa@x.com',
                       telefone_cliente='11988887777',
                       modo_entrega='retirada', valor_total=Decimal('20'),
                       **extra)
    db.session.add(ped)
    db.session.flush()
    db.session.add(FiscalPedidoOnline(
        pedido_id=ped.id, documento=documento, dados=dados,
        situacao_ie='nao_contribuinte', origem='owner',
        confirmado_em=agora(), criado_em=agora()))
    db.session.commit()
    return ped


_DADOS_FISCAIS = {'nome': 'PADOCA LTDA', 'endereco': 'Rua das Flores',
                  'numero': '10', 'complemento': '', 'bairro': 'Moema',
                  'cidade': 'Sao Paulo', 'uf': 'SP', 'cep': '04077000',
                  'ie': ''}


# ── 1. Consulta pública cortada ao limite do Tiny ───────────────────────

def test_resposta_consulta_corta_ao_limite_e_assina_o_mesmo_valor(app):
    from app.services import checkout_fiscal
    from app.services.fiscal_online import LIMITES_TINY
    consulta = _consulta_longa()
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, consulta)
    dados = resposta['dados']
    for campo, limite in LIMITES_TINY.items():
        assert len(dados[campo]) <= limite, campo
        assert dados[campo] == dados[campo].rstrip(), campo
    original = consulta['dados']
    for campo in ('nome', 'endereco', 'complemento', 'bairro', 'cidade'):
        assert dados[campo] == original[campo][:LIMITES_TINY[campo]].rstrip()
    # O que foi abreviado vem com o rótulo DA TELA (para o aviso ao cliente).
    abreviados = {item['campo']: item['rotulo'] for item in resposta['abreviados']}
    assert abreviados == {
        'nome': 'razão social', 'endereco': 'rua ou avenida',
        'complemento': 'complemento', 'bairro': 'bairro', 'cidade': 'cidade'}
    # Assinado == devolvido (o que a tela preenche).
    prova = checkout_fiscal._assinador().loads(resposta['token'])
    assert prova['dados'] == {k: v for k, v in dados.items() if k != 'situacao_ie'}
    assert prova['cnpj'] == CNPJ_NUM


def test_resposta_consulta_sem_campo_longo_nao_abrevia(app):
    from app.services import checkout_fiscal
    consulta = {'dados': dict(_DADOS_FISCAIS, situacao_ie=None), 'origem': 'cnpj_ws'}
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, consulta)
    assert resposta['abreviados'] == []
    assert resposta['dados']['nome'] == 'PADOCA LTDA'


def test_dados_abreviados_da_consulta_passam_como_verificados(app):
    """Regressão central: o cliente só confirma o que a tela preencheu e a
    compra sai VERIFICADA — antes o POST recusava pelo limite."""
    from app.services import checkout_fiscal
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, _consulta_longa())
    snapshot, erros = checkout_fiscal.validar(_form_da_resposta(resposta), CNPJ_NUM)
    assert erros == []
    assert snapshot['verificado'] is True
    assert snapshot['documento'] == CNPJ_NUM


def test_quebra_de_linha_da_base_publica_nao_quebra_a_verificacao(app):
    """<input type=text> descarta quebras de linha do valor: o servidor
    colapsa espaços dos dois lados para o assinado voltar igual."""
    from app.services import checkout_fiscal
    consulta = {'dados': dict(_DADOS_FISCAIS, complemento='SALA 1\nANDAR  2',
                              ie='110042490114', situacao_ie='contribuinte'),
                'origem': 'cnpj_ws'}
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, consulta)
    assert resposta['dados']['complemento'] == 'SALA 1 ANDAR 2'
    form = _form_da_resposta(resposta, fiscal_complemento='SALA 1 ANDAR 2')
    snapshot, erros = checkout_fiscal.validar(form, CNPJ_NUM)
    assert erros == [] and snapshot['verificado'] is True


def test_pendencia_de_limite_usa_rotulo_da_tela(app):
    from app.services import checkout_fiscal
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, _consulta_longa())
    form = _form_da_resposta(resposta, fiscal_endereco='R' * 60, fiscal_bairro='B' * 31)
    snapshot, erros = checkout_fiscal.validar(form, CNPJ_NUM)
    texto = ' '.join(erros)
    assert 'rua ou avenida com até 50 caracteres' in texto
    assert 'bairro com até 30 caracteres' in texto
    assert 'endereco com' not in texto            # chave interna nunca aparece
    assert snapshot['verificado'] is False        # não bate com o assinado


def test_pendencia_do_dono_usa_rotulo_e_nao_a_chave(app):
    from app.services.fiscal_online import pendencias
    row = SimpleNamespace(dados=dict(_DADOS_FISCAIS, endereco='R' * 60),
                          situacao_ie='nao_contribuinte')
    erros = pendencias(row)
    assert 'logradouro com até 50 caracteres (limite da nota fiscal)' in erros
    assert not any(e.startswith('endereco') for e in erros)


def test_rotulos_do_checkout_batem_com_a_tela(app):
    """ROTULOS_CHECKOUT duplica os rótulos de _fiscal_cnpj.html de
    propósito (mensagem do servidor); este teste impede que divirjam."""
    from app.services.checkout_fiscal import CAMPOS, ROTULOS_CHECKOUT
    html = (_RAIZ / 'app' / 'templates' / 'loja' / '_fiscal_cnpj.html').read_text(encoding='utf-8')
    for campo in CAMPOS:
        m = re.search(r'<label[^>]*for="fiscal-%s"[^>]*>\s*([^<\n]+)' % campo, html)
        assert m, campo
        assert m.group(1).strip().lower().startswith(ROTULOS_CHECKOUT[campo].lower()), campo


def test_criar_pedido_com_consulta_longa_sai_verificado(app):
    """Fluxo inteiro: consulta longa → cliente só confirma → pedido nasce
    com o cadastro fiscal VERIFICADO (checkout_consulta), não declarado."""
    from app.extensions import db
    from app.models import FiscalPedidoOnline
    from app.services import checkout_fiscal, loja_checkout
    p = _produto(db)
    loja = _loja(db)
    base = datetime(2026, 6, 17, 10, 0)
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    resposta = checkout_fiscal.resposta_consulta(CNPJ_NUM, _consulta_longa())
    form = _form_retirada(loja, data, '11.222.333/0001-81', **_form_da_resposta(resposta))
    pedido, erros = loja_checkout.criar_pedido(
        form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
    assert erros == []
    row = db.session.get(FiscalPedidoOnline, pedido.id)
    assert row.origem == 'checkout_consulta'
    assert row.documento == CNPJ_NUM
    assert row.dados['nome'] == resposta['dados']['nome']


@pytest.mark.loja_host
def test_api_cnpj_devolve_campos_abreviados(app, monkeypatch):
    from app.services import consulta_empresa
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    monkeypatch.setattr(consulta_empresa, 'consultar', lambda doc: _consulta_longa())
    r = app.test_client().post('/loja/api/cnpj', json={'cnpj': '11.222.333/0001-81'})
    assert r.status_code == 200
    corpo = r.get_json()
    assert len(corpo['dados']['nome']) <= 50
    assert {item['campo'] for item in corpo['abreviados']} >= {'nome', 'endereco'}


# ── 2. CNPJ alfanumérico ────────────────────────────────────────────────

def test_helper_canonico_de_documento():
    from app.utils import (
        cnpj_valido,
        cpf_valido,
        documento_valido,
        normalizar_documento,
        tipo_documento,
    )
    assert normalizar_documento(CNPJ_ALFA_MASCARA) == CNPJ_ALFA
    assert normalizar_documento(' 529.982.247-25 ') == '52998224725'
    assert normalizar_documento(None) == ''
    assert cnpj_valido(CNPJ_ALFA) and cnpj_valido(CNPJ_ALFA_MASCARA)
    assert not cnpj_valido('12ABC34501DE36')      # DV errado
    assert not cnpj_valido('12ABC34501DEAB')      # DV tem que ser numérico
    assert not cnpj_valido('12ABC34501D35')       # 13 posições
    # CNPJ numérico: resultado idêntico ao cálculo tradicional.
    assert cnpj_valido(CNPJ_NUM) and cnpj_valido('11.222.333/0001-81')
    assert not cnpj_valido('11222333000182')
    assert not cnpj_valido('11111111111111')
    # CPF continua só numérico.
    assert cpf_valido('529.982.247-25')
    assert not cpf_valido('5299822472A')
    assert tipo_documento(CNPJ_ALFA) == 'cnpj'
    assert tipo_documento('52998224725') == 'cpf'
    assert tipo_documento('122223330001') is None
    assert documento_valido(CNPJ_ALFA_MASCARA)


def test_loja_checkout_aceita_cnpj_alfanumerico(app):
    from app.services.loja_checkout import _cnpj_valido, _cpf_cnpj_valido, _cpf_valido
    assert _cnpj_valido(CNPJ_ALFA_MASCARA) is True
    assert _cpf_cnpj_valido(CNPJ_ALFA_MASCARA) is True
    assert _cpf_cnpj_valido('52998224725') is True
    assert _cpf_valido(CNPJ_ALFA) is False


def test_criar_pedido_grava_cnpj_alfanumerico_inteiro(app):
    from app.extensions import db
    from app.models import Cliente, FiscalPedidoOnline
    from app.services import loja_checkout
    p = _produto(db)
    loja = _loja(db)
    base = datetime(2026, 6, 17, 10, 0)
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    pedido, erros = loja_checkout.criar_pedido(
        _form_retirada(loja, data, CNPJ_ALFA_MASCARA),
        [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
    assert erros == []
    assert Cliente.query.filter_by(email='pj@x.com').one().cpf == CNPJ_ALFA
    assert db.session.get(FiscalPedidoOnline, pedido.id).documento == CNPJ_ALFA


def test_criar_pedido_cnpj_alfanumerico_com_consulta_sai_verificado(app):
    from app.extensions import db
    from app.models import FiscalPedidoOnline
    from app.services import checkout_fiscal, loja_checkout
    p = _produto(db)
    loja = _loja(db)
    base = datetime(2026, 6, 17, 10, 0)
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    # A rota ainda reduz a dígitos (outro bloco); o serviço normaliza o que
    # receber — aqui chega a forma com máscara, como no JS novo.
    resposta = checkout_fiscal.resposta_consulta(CNPJ_ALFA_MASCARA, _consulta_longa())
    form = _form_retirada(loja, data, CNPJ_ALFA_MASCARA, **_form_da_resposta(resposta))
    pedido, erros = loja_checkout.criar_pedido(
        form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
    assert erros == []
    row = db.session.get(FiscalPedidoOnline, pedido.id)
    assert row.documento == CNPJ_ALFA
    assert row.origem == 'checkout_consulta'


def test_criar_pedido_recusa_cnpj_alfanumerico_com_dv_errado(app):
    from app.extensions import db
    from app.services import loja_checkout
    p = _produto(db)
    loja = _loja(db)
    base = datetime(2026, 6, 17, 10, 0)
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    pedido, erros = loja_checkout.criar_pedido(
        _form_retirada(loja, data, '12.ABC.345/01DE-36'),
        [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
    assert pedido is None
    assert any('cnpj' in e.lower() for e in erros)


def test_consulta_empresa_consulta_o_cnpj_alfanumerico_inteiro(app, monkeypatch):
    from app.services import consulta_empresa
    monkeypatch.setattr(consulta_empresa, '_reservar_consulta', lambda: True)
    payload = {'razao_social': 'EMPRESA NOVA LTDA', 'estabelecimento': {
        'cnpj': CNPJ_ALFA, 'estado': {'sigla': 'SP'}, 'cidade': {'nome': 'São Paulo'},
        'tipo_logradouro': 'Rua', 'logradouro': 'das Flores', 'numero': '10',
        'complemento': '', 'bairro': 'Moema', 'cep': '04077000',
        'inscricoes_estaduais': []}}
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch('app.services.consulta_empresa.requests.get', return_value=resp) as get:
        resultado = consulta_empresa.consultar(CNPJ_ALFA_MASCARA)
    assert get.call_args.args[0].endswith('/cnpj/' + CNPJ_ALFA)
    assert resultado['dados']['nome'] == 'EMPRESA NOVA LTDA'


def test_consulta_empresa_recusa_cadastro_de_outro_cnpj_alfanumerico():
    """Letras fazem parte do número: mesma projeção em dígitos ≠ mesmo CNPJ."""
    from app.services.consulta_empresa import _documento_exato
    assert _documento_exato('12.ABC.345/01DE-35', CNPJ_ALFA)
    assert not _documento_exato('12.ABD.345/01DE-35', CNPJ_ALFA)
    assert not _documento_exato('CNPJ 12ABC34501DE35', CNPJ_ALFA)


def test_payloads_levam_cnpj_alfanumerico_inteiro(app):
    """NF (Tiny) e cliente do Pagar.me recebem o documento sem truncar."""
    from app.extensions import db
    from app.services import fiscal_online, pagarme, tiny_nf
    ped = _pedido_confirmado(db, CNPJ_ALFA, dict(_DADOS_FISCAIS))
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert erro is None
    assert cliente['cpf_cnpj'] == CNPJ_ALFA and cliente['tipo_pessoa'] == 'J'
    assert tiny_nf._payload_cliente(ped)['cpf_cnpj'] == CNPJ_ALFA
    assert tiny_nf._payload_cliente(ped)['tipo_pessoa'] == 'J'
    customer = pagarme._payload_customer(ped)
    assert customer['document'] == CNPJ_ALFA
    assert customer['document_type'] == 'cnpj'


def test_documento_de_pedido_sem_snapshot_preserva_letras(app):
    from app.extensions import db
    from app.models import Cliente, PedidoOnline
    from app.services.fiscal_online import documento
    cli = Cliente(nome='X LTDA', email='x-alfa@x.com', cpf=CNPJ_ALFA_MASCARA)
    db.session.add(cli)
    db.session.flush()
    ped = PedidoOnline(cliente_id=cli.id, nome_cliente='X LTDA', email_cliente='x-alfa@x.com',
                       modo_entrega='retirada', valor_total=Decimal('20'))
    db.session.add(ped)
    db.session.commit()
    assert documento(ped) == CNPJ_ALFA


def test_conferencia_do_rascunho_compara_as_letras_do_cnpj(app, monkeypatch):
    from app.extensions import db
    from app.services import fiscal_online, tiny
    ped = _pedido_confirmado(db, CNPJ_ALFA, dict(_DADOS_FISCAIS),
                             tiny_nota_fiscal_id='nf-1')
    cliente = {**_DADOS_FISCAIS, 'cpf_cnpj': '12.ABC.345/01DE-35'}
    monkeypatch.setattr(tiny, 'obter_nota_fiscal', lambda _id: {'cliente': dict(cliente)})
    assert fiscal_online.conferir_rascunho(ped) is None
    # Outro CNPJ com os mesmos dígitos e letras diferentes = divergência.
    cliente['cpf_cnpj'] = '12.ABD.345/01DE-35'
    erro = fiscal_online.conferir_rascunho(ped)
    assert erro and 'cpf_cnpj' in erro


def test_tiny_busca_contato_com_o_cnpj_alfanumerico_inteiro(app, monkeypatch):
    """Antes o documento ia reduzido a dígitos: recusado, ou — com três
    letras — pesquisado como se a sobra fosse um CPF."""
    from app.services import tiny
    chamadas = []

    def _get(endpoint, params=None, **_kw):
        chamadas.append((endpoint, dict(params or {})))
        contato = {'id': '7', 'nome': 'EMPRESA NOVA LTDA', 'cpf_cnpj': '12.ABC.345/01DE-35'}
        if endpoint == 'contatos.pesquisa.php':
            return {'status': 'OK', 'numero_paginas': 1, 'contatos': [{'contato': contato}]}
        return {'status': 'OK', 'contato': contato}

    monkeypatch.setattr(tiny, '_get', _get)
    res = tiny.buscar_contato_por_documento(CNPJ_ALFA_MASCARA)
    assert res.get('contato', {}).get('id') == '7'
    assert chamadas[0][1]['cpf_cnpj'] == CNPJ_ALFA


def test_tiny_busca_contato_recusa_documento_invalido_sem_rede(app, monkeypatch):
    from app.services import tiny
    monkeypatch.setattr(tiny, '_get', lambda *a, **k: pytest.fail('não deveria consultar'))
    assert tiny.buscar_contato_por_documento('12ABC34501DE36') == {'erro': 'Documento inválido.'}


# ── JS: window.DocumentoFiscal + aviso de campos abreviados ─────────────

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const timers = [];
function el(extra = {}) {
  return Object.assign({value: '', hidden: false, disabled: false, required: false, checked: false,
    textContent: '', handlers: {}, attrs: {},
    addEventListener(n, f) { (this.handlers[n] ||= []).push(f); },
    setAttribute(n, v) { this.attrs[n] = String(v); }, removeAttribute(n) { delete this.attrs[n]; },
    fire(n) { (this.handlers[n] || []).forEach(f => f.call(this, {type: n})); }}, extra);
}
const nomes = ['nome', 'endereco', 'numero', 'complemento', 'bairro', 'cidade', 'uf', 'cep', 'ie', 'situacao_ie'];
const campos = {};
nomes.forEach(n => { campos['fiscal_' + n] = el(); });
campos.fiscal_token = el(); campos.fiscal_confirmado = el(); campos.csrf_token = el({value: 'tok'});
const documento = el(), fieldset = el({disabled: true}), status = el(), botao = el(), ieCampo = el();
const form = {elements: {namedItem(n) { return campos[n] || null; }},
  querySelector(sel) { return sel === '[name="cpf"]' ? documento : null; }, dispatchEvent() {}};
const bloco = el({hidden: true, dataset: {consultaUrl: '/loja/api/cnpj'}, closest() { return form; },
  querySelector(sel) {
    return {'fieldset': fieldset, '[data-fiscal-status]': status, '[data-fiscal-consultar]': botao,
      '[data-fiscal-ie-campo]': ieCampo}[sel] || null;
  }});
const document = {readyState: 'complete', addEventListener() {},
  querySelectorAll(sel) { return sel === '[data-fiscal-cnpj]' ? [bloco] : []; }};
const RESPOSTA = __RESPOSTA__;
let corpo = null;
async function fetch(url, opts) { corpo = JSON.parse(opts.body); return {ok: true, json: async () => RESPOSTA}; }
const ctx = {document, fetch, AbortController, CustomEvent, Event, Set, console,
  setTimeout(f) { timers.push(f); return timers.length; }, clearTimeout() {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

(async () => {
  const D = ctx.DocumentoFiscal;
  assert.ok(D, 'window.DocumentoFiscal ausente');
  assert.equal(D.normalizar('12.abc.345/01de-35'), '12ABC34501DE35');
  assert.equal(D.ehCnpj('12.abc.345/01de-35'), true);
  assert.equal(D.ehCnpj('11.222.333/0001-81'), true);
  assert.equal(D.ehCnpj('529.982.247-25'), false);
  assert.equal(D.ehCnpj('12ABC34501DEAB'), false);
  assert.equal(D.mascarar('12abc34501de35'), '12.ABC.345/01DE-35');
  assert.equal(D.mascarar('11222333000181'), '11.222.333/0001-81');
  assert.equal(D.mascarar('52998224725'), '529.982.247-25');
  assert.equal(D.mascarar('529982'), '529.982');
  assert.equal(D.mascarar('12a'), '12.A');

  // CNPJ alfanumérico digitado: bloco fiscal aparece e a consulta leva as letras.
  documento.value = '12.abc.345/01de-35';
  documento.fire('input');
  assert.equal(bloco.hidden, false, 'bloco fiscal deveria aparecer para CNPJ alfanumérico');
  assert.equal(fieldset.disabled, false);
  // O cliente já tinha digitado a rua: ela não é sobrescrita nem anunciada.
  campos.fiscal_endereco.value = 'Rua do cliente';
  campos.fiscal_endereco.fire('input');
  assert.equal(timers.length, 1, 'consulta agendada');
  await timers.shift()();
  assert.deepEqual(corpo, {cnpj: '12ABC34501DE35'});
  assert.equal(campos.fiscal_nome.value, RESPOSTA.dados.nome);
  assert.equal(campos.fiscal_endereco.value, 'Rua do cliente');
  assert.equal(campos.fiscal_token.value, RESPOSTA.token);
  assert.match(status.textContent, /Abreviamos razão social e bairro para caber no limite da nota fiscal/);
  assert.doesNotMatch(status.textContent, /rua ou avenida/);
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node indisponível')
def test_fiscal_cnpj_js_documento_alfanumerico_e_aviso_de_abreviados(tmp_path):
    resposta = {
        'dados': {'nome': 'PANIFICADORA E CONFEITARIA ARTESANAL DO BROOKLIN CO',
                  'endereco': 'Avenida Engenheiro Luiz Carlos Berrini Lado Impar',
                  'numero': '1500', 'complemento': '', 'bairro': 'Cidade Moncoes e Vila Olimpia',
                  'cidade': 'Sao Paulo', 'uf': 'SP', 'cep': '04571000', 'ie': '',
                  'situacao_ie': 'desconhecida'},
        'token': 'tok-assinado', 'origem': 'cnpj_ws', 'atualizado_em': '',
        'aviso': 'Confira os dados.',
        'abreviados': [{'campo': 'nome', 'rotulo': 'razão social'},
                       {'campo': 'endereco', 'rotulo': 'rua ou avenida'},
                       {'campo': 'bairro', 'rotulo': 'bairro'}],
    }
    harness = tmp_path / 'harness.js'
    harness.write_text(_HARNESS.replace('__RESPOSTA__', json.dumps(resposta)), encoding='utf-8')
    script = _RAIZ / 'app' / 'static' / 'loja' / 'fiscal-cnpj.js'
    proc = subprocess.run(['node', str(harness), str(script)], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'OK' in proc.stdout
