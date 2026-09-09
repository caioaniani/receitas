"""Sugestão B2B no orçamento, sem substituir a negociação já informada."""
import json
import re
import shutil
import subprocess
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.extensions import db
from app.models import ClienteB2B, PrecoClienteB2B, Produto, Receita
from app.services import orcamentos, vendas_b2b


def _login(client, usuario):
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True


def _json_bloco(resposta, identificador):
    html = resposta.get_data(as_text=True)
    trecho = re.search(
        rf'<script[^>]+id="{identificador}"[^>]*>(.*?)</script>', html, re.S)
    assert trecho, identificador
    return json.loads(trecho.group(1))


class _CamposCabecalho(HTMLParser):
    def __init__(self, resposta):
        super().__init__()
        self.valores = {}
        self.selecao = self.textarea = None
        self.feed(resposta.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        nome = attrs.get('name')
        if tag == 'input' and nome:
            self.valores[nome] = attrs.get('value', '')
        elif tag == 'select':
            self.selecao = nome
            self.valores[nome] = ''
        elif tag == 'option' and self.selecao and 'selected' in attrs:
            self.valores[self.selecao] = attrs.get('value', '')
        elif tag == 'textarea':
            self.textarea = nome
            self.valores[nome] = ''

    def handle_data(self, data):
        if self.textarea:
            self.valores[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == 'select':
            self.selecao = None
        elif tag == 'textarea':
            self.textarea = None


def _catalogo():
    receita = Receita(nome='Mini Croissant', categoria='Mini Pães',
                      rendimento_qtd=1, rendimento_unidade='un', peso_base=100,
                      preco_venda=10.10, preco_loja=30, preco_site=40)
    produto = Produto(nome='Caixa dos minis', ativo=True,
                      preco_atacado=20.10, preco_loja=50, preco_site=60)
    desconto = ClienteB2B(nome='Cliente com desconto', desconto_percentual=5)
    especial = ClienteB2B(nome='Cliente com tabela', desconto_percentual=20)
    db.session.add_all([receita, produto, desconto, especial])
    db.session.flush()
    for kind, item, preco in [('receita', receita, '7.23'),
                              ('produto', produto, '16.41')]:
        db.session.add(PrecoClienteB2B(
            cliente_id=especial.id, kind=kind, item_id=item.id,
            preco=Decimal(preco)))
    db.session.commit()
    return receita, produto, desconto, especial


@pytest.mark.parametrize('kind,esperados', [
    ('receita', [('10.10', 0), ('10.10', 5), ('7.23', 0)]),
    ('produto', [('20.10', 0), ('20.10', 5), ('16.41', 0)]),
])
def test_orcamento_sugere_atacado_desconto_e_tabela_do_cliente(
        app, admin_user, kind, esperados):
    client = app.test_client()
    _login(client, admin_user)
    with app.app_context():
        receita, produto, desconto, especial = _catalogo()
        item = receita if kind == 'receita' else produto
        ref = f'{kind}:{item.id}'
        resposta = client.get('/b2b/orcamentos/novo')
        assert resposta.status_code == 200
        tabelas = _json_bloco(resposta, 'sugestoes-precos-json')
        for cliente, esperado in zip([None, desconto, especial], esperados):
            cid = str(cliente.id) if cliente else ''
            taxa = tabelas['descontos'].get(cid, '0')
            sugerido = tabelas['especificos'].get(cid, {}).get(
                ref, tabelas['por_desconto'][taxa][ref])
            assert sugerido['preco_unitario'] == esperado[0]
            assert Decimal(sugerido['desconto_percentual']) == esperado[1]
        # Ambos os formulários consomem as mesmas condições, sem preço líquido arredondado.
        assert _json_bloco(client.get('/b2b/vendas/nova'), 'sugestoes-precos-json') == tabelas


def test_orcamento_sem_atacado_nao_herda_varejo_ou_site(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    with app.app_context():
        receita, produto, _, _ = _catalogo()
        receita.preco_venda = None
        produto.preco_atacado = None
        db.session.commit()
        tabelas = _json_bloco(client.get('/b2b/orcamentos/novo'), 'sugestoes-precos-json')
        assert f'receita:{receita.id}' not in tabelas['por_desconto']['0']
        assert f'produto:{produto.id}' not in tabelas['por_desconto']['0']


def test_edicao_preserva_preco_negociado_apos_mudar_tabela(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    with app.app_context():
        receita, _, _, especial = _catalogo()
        campos = {'cliente_id': str(especial.id)}
        linha = {'catalogo': f'receita:{receita.id}', 'nome': 'Mini negociado',
                 'qtd': '20', 'preco_unitario': '6.37'}
        orc, erros = orcamentos.criar_orcamento(campos, [linha])
        assert not erros
        receita.preco_venda = 99
        db.session.commit()
        url = f'/b2b/orcamentos/{orc.id}/editar'
        existente = _json_bloco(client.get(url), 'itens-existentes')[0]
        assert existente['preco'] == 6.37
        assert existente['nome'] == 'Mini negociado'
        resposta = client.post(url, data={
            **campos, **{f'itens[0][{chave}]': valor for chave, valor in linha.items()}})
        assert resposta.status_code == 302
        db.session.refresh(orc)
        assert orc.itens[0].preco_unitario == Decimal('6.37')
        assert orc.itens[0].desconto_percentual == 0
        assert orc.valor_total == Decimal('127.40')


def test_erro_do_formulario_preserva_linhas_e_preco_informado(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resposta = client.post('/b2b/orcamentos/novo', data={
        'cliente_nome': '', 'itens[0][catalogo]': 'livre',
        'itens[0][nome]': 'Composição negociada', 'itens[0][qtd]': '30',
        'itens[0][preco_unitario]': '0', 'itens[0][observacao]': 'Condição combinada',
        'itens[0][desconto_percentual]': '3,5',
    })
    assert resposta.status_code == 200
    linha = _json_bloco(resposta, 'itens-existentes')[0]
    assert linha['nome'] == 'Composição negociada'
    assert linha['qtd'] == '30'
    assert linha['preco'] == '0'
    assert linha['obs'] == 'Condição combinada'
    assert linha['desconto'] == '3,5'


@pytest.mark.parametrize('limpar', [False, True])
def test_edicao_invalida_preserva_cabecalho_digitado_sem_mudar_banco(
        app, admin_user, limpar):
    client = app.test_client()
    _login(client, admin_user)
    with app.app_context():
        receita, _, desconto, especial = _catalogo()
        antigos = {
            'cliente_id': str(especial.id), 'cliente_nome': 'Nome antigo',
            'cliente_documento': 'Documento antigo', 'cliente_email': 'antigo@example.test',
            'cliente_telefone': '11911111111', 'cliente_endereco': 'Endereço antigo',
            'desconto_valor': '2', 'frete_valor': '10', 'validade_dias': '14',
            'data_entrega': '2026-09-15', 'observacao': 'Observação antiga',
        }
        linha = {'catalogo': f'receita:{receita.id}', 'nome': 'Mini negociado',
                 'qtd': '100', 'preco_unitario': '10.10', 'desconto_percentual': '5'}
        orc, erros = orcamentos.criar_orcamento(antigos, [linha])
        assert not erros
        url = f'/b2b/orcamentos/{orc.id}/editar'
        salvo = _CamposCabecalho(client.get(url)).valores
        assert salvo['validade_dias'] == '14'
        cabecalho_banco = {col.name: getattr(orc, col.name) for col in orc.__table__.columns}
        item_banco = {col.name: getattr(orc.itens[0], col.name)
                      for col in orc.itens[0].__table__.columns}
        digitados = {
            'cliente_id': '', 'cliente_nome': 'Nome novo " & <teste>',
            'cliente_documento': 'Documento novo', 'cliente_email': 'novo@example.test',
            'cliente_telefone': '11922222222', 'cliente_endereco': 'Endereço novo',
            'desconto_valor': '3', 'frete_valor': '25', 'validade_dias': '20',
            'data_entrega': '2026-09-20', 'observacao': 'Observação nova\nSegunda linha',
        }
        if limpar:
            digitados = dict.fromkeys(digitados, '')
            digitados.update(cliente_id=str(desconto.id), desconto_valor='0', frete_valor='0')
        resposta = client.post(url, data={
            **digitados,
            **{f'itens[0][{chave}]': valor for chave, valor in linha.items()},
            'itens[0][desconto_percentual]': '101',
        })
        assert resposta.status_code == 200
        assert 'Desconto por item inválido' in resposta.get_data(as_text=True)
        campos = _CamposCabecalho(resposta).valores
        assert {nome: campos[nome] for nome in digitados} == digitados
        assert _json_bloco(resposta, 'itens-existentes')[0]['desconto'] == '101'
        db.session.expire_all()
        assert {nome: getattr(orc, nome) for nome in cabecalho_banco} == cabecalho_banco
        assert {nome: getattr(orc.itens[0], nome) for nome in item_banco} == item_banco
        # O GET seguinte continua a mostrar o snapshot, não o POST rejeitado.
        depois = _CamposCabecalho(client.get(url)).valores
        assert {nome: depois[nome] for nome in antigos} == {
            nome: salvo[nome] for nome in antigos}


_JS = r'''
require(process.argv[1]);
const assert = require('assert');
const api = global.B2BOrcamentoPrecos;
const tabelas = JSON.parse(process.argv[2]);
const refs = Object.keys(tabelas.por_desconto['0']);
const clientes = Object.keys(tabelas.descontos);
let ref = 'livre', cliente = '';
function elemento(value = '') {
  const listeners = {};
  return {value, hidden: false, textContent: '',
    addEventListener: (evento, fn) => { listeners[evento] = fn; },
    disparar: evento => listeners[evento]()};
}
function linha(preco = '', preservar = false) {
  const nome = elemento(), input = elemento(preco), desconto = elemento('0'), aviso = elemento(), botao = elemento();
  const controle = api.controlar({nome, preco: input, desconto, aviso, botao,
    preservarPreco: preservar, nomeManual: false, recalcular: () => {},
    sugestao: () => {
      const condicoes = api.sugerido(tabelas, cliente, ref);
      return {selecionado: ref !== 'livre', nome: ref,
              preco: condicoes ? condicoes.preco_unitario : '',
              desconto: condicoes ? condicoes.desconto_percentual : '0'};
    }});
  return {nome, input, desconto, aviso, botao, controle};
}
const atual = linha();
assert.strictEqual(atual.aviso.textContent, ''); // linha vazia não acusa cadastro
ref = refs[0];
atual.controle.trocarItem();
assert.strictEqual(atual.input.value, '10.10');
ref = refs[1]; atual.controle.trocarItem();
assert.strictEqual(atual.nome.value, refs[1]);
assert.strictEqual(atual.input.value, '20.10');
cliente = clientes[0]; atual.controle.trocarCliente();
assert.strictEqual(atual.input.value, '20.10');
assert.strictEqual(Number(atual.desconto.value), 5);
cliente = clientes[1]; atual.controle.trocarCliente();
assert.strictEqual(atual.input.value, '16.41'); // tabela sem desconto adicional
assert.strictEqual(Number(atual.desconto.value), 0);
atual.nome.value = 'Nome personalizado'; atual.nome.disparar('input');
atual.input.value = '6.37'; atual.input.disparar('input');
atual.desconto.value = '3.5'; atual.desconto.disparar('input');
ref = refs[0]; cliente = ''; atual.controle.trocarItem(); atual.controle.trocarCliente();
assert.strictEqual(atual.nome.value, 'Nome personalizado');
assert.strictEqual(atual.input.value, '6.37');
assert.strictEqual(atual.desconto.value, '3.5');
assert.strictEqual(atual.botao.hidden, false);
atual.botao.disparar('click');
assert.strictEqual(atual.input.value, '10.10');
cliente = clientes[0]; atual.controle.trocarCliente();
assert.strictEqual(atual.input.value, '10.10'); // base e percentual, sem 9,60 intermediário
assert.strictEqual(Number(atual.desconto.value), 5);
assert.strictEqual(api.subtotalCentavos('100', atual.input.value, atual.desconto.value), 95950n);
atual.input.value = ''; atual.input.disparar('input');
assert.strictEqual(atual.aviso.textContent, 'Informe o preço ou use a sugestão.');
assert.strictEqual(atual.botao.hidden, false);
atual.controle.trocarCliente();
assert.strictEqual(atual.input.value, ''); // apagar não volta silenciosamente ao catálogo
atual.botao.disparar('click');
assert.strictEqual(atual.input.value, '10.10');
for (const valor of ['6.37', '0']) {
  const salva = linha(valor, true);
  salva.desconto.value = '12.3';
  salva.controle.trocarCliente(); salva.controle.trocarItem();
  assert.strictEqual(salva.input.value, valor); // snapshot, inclusive zero
  assert.strictEqual(salva.desconto.value, '12.3');
}
ref = 'livre';
atual.controle.trocarItem();
assert.strictEqual(atual.input.value, ''); // não arrasta o preço de outro item
assert.strictEqual(atual.aviso.textContent, '');
assert.strictEqual(api.escapar('\"><img src=x onerror=alert(1)>&'),
                   '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;');
assert.strictEqual(api.escapar(0), '0');
assert.strictEqual(api.escapar(null), '');
const casos = JSON.parse(process.argv[3]);
for (const caso of casos) {
  assert.strictEqual(api.subtotalCentavos(...caso.valores), BigInt(caso.centavos));
}
const meio = api.subtotalCentavos('0.001', '5.00', '0');
assert.strictEqual(api.moedaCentavos(meio + meio), 'R$ 0,02'); // arredonda cada linha antes de somar
console.log('ok');
'''


def test_js_troca_item_cliente_e_preserva_negociacao(app):
    node = shutil.which('node')
    if not node:
        pytest.skip('node não instalado')
    with app.app_context():
        receita, produto, desconto, especial = _catalogo()
        tabelas = vendas_b2b.sugestoes_orcamento(
            vendas_b2b.catalogo_precos([receita], [produto]), [desconto, especial])
    arquivo = Path(__file__).parents[1] / 'app/static/js/b2b-orcamento-precos.js'
    from app.services.precos_b2b import subtotal_com_desconto
    valores = [('100', '10.10', '5'), ('1', '10.10', '5'),
               ('.5', '6.37', '12.3'), ('0.001', '5.00', '0'), ('30', '6.00', '12.3')]
    casos = [{'valores': v, 'centavos': str(int(subtotal_com_desconto(*v) * 100))}
             for v in valores]
    resultado = subprocess.run(
        [node, '-e', _JS, str(arquivo), json.dumps(tabelas), json.dumps(casos)],
        capture_output=True, text=True, timeout=30)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert 'ok' in resultado.stdout
