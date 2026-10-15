"""Cadastro fiscal DECLARADO pelo cliente resolvido pela base pública, sem
esperar o dono (15/10/2026 — dono: "Por que fica aguardando minha
conferência? Deveria já buscar automaticamente e emitir").

A consulta à CNPJ.ws é simulada no formato REAL dela (lista
`inscricoes_estaduais`); nenhum teste sai para a rede.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import Cliente, FiscalPedidoOnline, PedidoOnline
from app.services import consulta_empresa, fiscal_online
from app.utils import agora

CNPJ = '48104494000180'
RECENTE = (agora() - timedelta(days=10)).strftime('%Y-%m-%dT00:00:00Z')


def _payload(ies=None, uf='SP', sem_lista=False, atualizado=None):
    est = {'cnpj': CNPJ, 'estado': {'sigla': uf}, 'cidade': {'nome': 'São Paulo'},
           'tipo_logradouro': 'Avenida', 'logradouro': 'Eng Luiz Carlos Berrini',
           'numero': '1748', 'complemento': 'Andar 23 Sala 2304',
           'bairro': 'Cidade Moncoes', 'cep': '04571000',
           'atualizado_em': atualizado or RECENTE}
    if not sem_lista:
        est['inscricoes_estaduais'] = ies or []
    return {'razao_social': 'SECURITY SYARD BRASIL LTDA', 'estabelecimento': est}


def _ie(numero, uf='SP', ativo=True):
    return {'inscricao_estadual': numero, 'ativo': ativo, 'estado': {'sigla': uf}}


@pytest.fixture
def base_publica(app, monkeypatch):
    """Faz a consulta real (`consulta_empresa.consultar`) ler o payload dado."""
    monkeypatch.setattr(consulta_empresa, '_reservar_consulta', lambda: True)
    monkeypatch.setattr(consulta_empresa, '_ler_cache', lambda _doc: None)
    estado = {'payload': _payload(), 'tiny': {'contato': None}}

    def _get(*_a, **_k):
        resp = MagicMock(status_code=200)
        resp.json.return_value = estado['payload']
        return resp
    with patch('app.services.consulta_empresa.requests.get', side_effect=_get), \
            patch('app.services.tiny.buscar_contato_por_documento',
                  side_effect=lambda _d: estado['tiny']):
        yield estado


def _pedido(situacao='desconhecida', ie=''):
    cli = Cliente(nome='Security', email='sec@x.com', cpf=CNPJ)
    db.session.add(cli)
    db.session.flush()
    ped = PedidoOnline(cliente_id=cli.id, nome_cliente='Security', email_cliente='sec@x.com',
                       telefone_cliente='11988887777', modo_entrega='express',
                       valor_total=Decimal('250'), status='pago', pago_em=agora())
    db.session.add(ped)
    db.session.flush()
    db.session.add(FiscalPedidoOnline(
        pedido_id=ped.id, documento=CNPJ, origem='checkout_declarado',
        situacao_ie=situacao, criado_em=agora(),
        erro='Dados informados pelo cliente. O dono precisa conferir o cadastro fiscal '
             'e a inscrição estadual antes de autorizar a nota.',
        dados={'nome': 'SECURITY SYARD BRASIL LTDA', 'endereco': 'AV BERRINI',
               'numero': '1748', 'complemento': '', 'bairro': 'Moncoes',
               'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04571000', 'ie': ie}))
    db.session.commit()
    return ped


def test_empresa_sem_inscricao_sai_nao_contribuinte_sem_o_dono(app, base_publica):
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert erro is None
    assert cliente['cpf_cnpj'] == CNPJ and cliente['ie'] == ''
    row = db.session.get(FiscalPedidoOnline, ped.id)
    assert row.origem == fiscal_online.ORIGEM_AUTO and row.situacao_ie == 'nao_contribuinte'
    # Dados da NOTA = os da base pública (Receita), não os digitados.
    assert row.dados['endereco'] == 'Avenida Eng Luiz Carlos Berrini'
    assert row.dados['_auto']['declarado']['endereco'] == 'AV BERRINI'


def test_cliente_que_declarou_isento_sai_isento(app, base_publica):
    ped = _pedido(situacao='isento')
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert erro is None and cliente['ie'] == 'ISENTO'


def test_ie_unica_na_base_sai_contribuinte(app, base_publica):
    base_publica['payload'] = _payload([_ie('115.946.174.118')])
    ped = _pedido(situacao='contribuinte', ie='115946174118')
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert erro is None and cliente['ie'] == '115946174118'
    assert db.session.get(FiscalPedidoOnline, ped.id).situacao_ie == 'contribuinte'


def test_ie_divergente_fica_com_o_dono_com_o_motivo(app, base_publica):
    base_publica['payload'] = _payload([_ie('115946174118')])
    ped = _pedido(situacao='contribuinte', ie='999999999999')
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and '999999999999' in erro and '115946174118' in erro
    row = db.session.get(FiscalPedidoOnline, ped.id)
    assert row.origem == 'checkout_declarado' and row.confirmado_em is None


@pytest.mark.parametrize('payload,trecho', [
    (_payload([_ie('115946174118'), _ie('110042490114')]), '2 inscrições'),
    (_payload(sem_lista=True), 'não trouxe'),
    (_payload(uf='RJ'), 'outro estado'),
    (_payload([_ie('115946174118', uf='RJ')], uf='RJ'), 'outro estado'),
    (_payload([{'inscricao_estadual': '115946174118', 'estado': {'sigla': 'SP'}}]),
     'sem situação'),
    (_payload([_ie('115946174118'), _ie('ISENTO')]), '2 inscrições'),
    (_payload(atualizado='2023-01-01T00:00:00Z'), 'desatualizada'),
])
def test_casos_que_a_base_nao_resolve_ficam_com_o_dono(app, base_publica, payload, trecho):
    base_publica['payload'] = payload
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and trecho in erro


def test_cliente_que_disse_ter_ie_e_a_base_nao_mostra_fica_com_o_dono(app, base_publica):
    ped = _pedido(situacao='contribuinte', ie='123456789012')
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and 'contribuinte' in erro


def test_base_fora_nao_vira_nao_contribuinte(app, monkeypatch):
    monkeypatch.setattr(consulta_empresa, 'consultar',
                        lambda _d: {'dados': {}, 'origem': '', 'falha': 'indisponivel'})
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and 'não respondeu' in erro
    row = db.session.get(FiscalPedidoOnline, ped.id)
    assert row.situacao_ie == 'desconhecida'
    # Indisponibilidade não é pendência do dono e não carimba a consulta:
    # a próxima tentativa da fila consulta de novo.
    assert not fiscal_online.aguarda_dono(row) and row.consultado_em is None


def test_tiny_fora_tenta_de_novo_sem_chamar_o_dono(app, base_publica):
    base_publica['tiny'] = {'erro': 'Não foi possível consultar os clientes no Tiny.'}
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and 'Tiny' in erro
    assert not fiscal_online.aguarda_dono(db.session.get(FiscalPedidoOnline, ped.id))


def test_ie_no_tiny_impede_nao_contribuinte(app, base_publica):
    base_publica['tiny'] = {'contato': {'ie': '115.946.174.118'}}
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and '115946174118' in erro
    assert fiscal_online.aguarda_dono(db.session.get(FiscalPedidoOnline, ped.id))


def test_ie_do_tiny_divergente_da_base_vai_ao_dono(app, base_publica):
    base_publica['payload'] = _payload([_ie('115946174118')])
    base_publica['tiny'] = {'contato': {'ie': '110042490114'}}
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert cliente is None and 'Tiny' in erro and '110042490114' in erro


def test_tiny_isento_sai_isento(app, base_publica):
    base_publica['tiny'] = {'contato': {'ie': 'ISENTO'}}
    ped = _pedido()
    cliente, erro = fiscal_online.payload_cliente(ped)
    assert erro is None and cliente['ie'] == 'ISENTO'


def test_metadados_do_checkout_ficam_na_trilha(app, base_publica):
    ped = _pedido()
    row = db.session.get(FiscalPedidoOnline, ped.id)
    row.dados = {**row.dados, '_checkout': {'origem_consulta': 'cnpj_ws'}}
    db.session.commit()
    fiscal_online.payload_cliente(ped)
    row = db.session.get(FiscalPedidoOnline, ped.id)
    assert row.origem == fiscal_online.ORIGEM_AUTO
    assert row.dados['_checkout'] == {'origem_consulta': 'cnpj_ws'}


def test_consulta_forcada_continua_aguardando_o_dono(app, base_publica):
    """A consulta explícita do dono troca a origem; se ainda falta dado, o
    pedido continua marcado como aguardando o dono (não some do aviso)."""
    ped = _pedido()
    base_publica['tiny'] = {'erro': 'Tiny fora'}
    row = fiscal_online.consultar(ped, forcar=True)
    assert row.origem != 'checkout_declarado' and row.erro
    assert fiscal_online.aguarda_dono(row)


def test_fallback_sem_lista_de_ie_nunca_resolve(app, monkeypatch):
    """A fonte de endereço (BrasilAPI etc.) não sabe de IE: não é "não tem"."""
    monkeypatch.setattr(consulta_empresa, 'consultar', lambda _d: {
        'origem': 'cnpj_publico', 'dados': {'nome': 'X', 'endereco': 'Rua A', 'numero': '1',
                                            'bairro': 'B', 'cidade': 'São Paulo', 'uf': 'SP',
                                            'cep': '04571000', 'ie': '', 'situacao_ie': None}})
    ped = _pedido()
    cliente, _erro = fiscal_online.payload_cliente(ped)
    assert cliente is None


def test_reconsulta_espera_o_intervalo(app, base_publica, monkeypatch):
    """Pendência do dono (aqui: empresa de outro estado) não reconsulta a cada
    tentativa da fila — só depois de REAVALIAR_DECLARADO_MIN."""
    base_publica['payload'] = _payload(uf='RJ')
    real = consulta_empresa.consultar
    chamadas = []
    monkeypatch.setattr(consulta_empresa, 'consultar',
                        lambda d: chamadas.append(d) or real(d))
    ped = _pedido()
    fiscal_online.payload_cliente(ped)
    fiscal_online.payload_cliente(ped)
    assert len(chamadas) == 1
    row = db.session.get(FiscalPedidoOnline, ped.id)
    row.consultado_em = agora() - timedelta(minutes=fiscal_online.REAVALIAR_DECLARADO_MIN + 1)
    db.session.commit()
    fiscal_online.payload_cliente(ped)
    assert len(chamadas) == 2


def test_conferencia_do_dono_continua_valendo(app, base_publica, owner_user):
    """Resolvido automaticamente, a consulta não reescreve; o dono ainda pode
    conferir e trocar."""
    ped = _pedido()
    fiscal_online.payload_cliente(ped)
    erro = fiscal_online.salvar_conferencia(ped, {
        'conferido': '1', 'nome': 'SECURITY SYARD BRASIL LTDA', 'ie': '',
        'endereco': 'Av Berrini', 'numero': '1748', 'complemento': '', 'bairro': 'Moncoes',
        'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04571000', 'situacao_ie': 'isento'},
        owner_user.id)
    assert erro is None
    assert db.session.get(FiscalPedidoOnline, ped.id).situacao_ie == 'isento'


def test_consulta_expoe_quantas_ie_ativas_na_uf(app, base_publica):
    base_publica['payload'] = _payload([_ie('1', ativo=False), _ie('2', uf='RJ')])
    assert consulta_empresa.consultar(CNPJ)['ie_ativas_uf'] == 0
    base_publica['payload'] = _payload(sem_lista=True)
    assert consulta_empresa.consultar(CNPJ)['ie_ativas_uf'] is None
    base_publica['payload'] = _payload([_ie('115946174118', ativo=None)])
    assert consulta_empresa.consultar(CNPJ)['ie_ativas_uf'] is None
    base_publica['payload'] = _payload(['lixo'])
    assert consulta_empresa.consultar(CNPJ)['ie_ativas_uf'] is None
