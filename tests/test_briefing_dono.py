"""Briefing diário do dono (16/07/2026) — cockpit push.

Cobre o serviço (pendências, vendas de ontem, custo de IA, texto), o envio
(zapi mockado — padrão do test_uso_ia_vigia), a rota owner /admin/briefing,
o bloco "Precisa de você hoje" da home, o manual de operação e a sonda
/api/claude/acuracia.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.utils import hoje

TOKEN = 'token-de-teste-bem-longo-123'


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _plano(data, enviado, itens=()):
    """PlanejamentoProducao origem=cronograma + itens (qtd_alvo, produzido)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao, Receita
    p = PlanejamentoProducao(data=data, origem='cronograma',
                             enviado_ao_padeiro=enviado)
    db.session.add(p)
    db.session.flush()
    for i, (qtd_alvo, produzido) in enumerate(itens):
        r = Receita(nome=f'Pão Plano {p.id}-{i}', categoria='Paes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0)
        db.session.add(r)
        db.session.flush()
        db.session.add(PlanejamentoItem(planejamento_id=p.id,
                                        receita_id=r.id,
                                        qtd_alvo=qtd_alvo,
                                        produzido_qtd=produzido))
    db.session.commit()
    return p


def _venda_dia(data, loja_seru='Loja A', fat=1000, n=10):
    from app.models import VendaSeruDiaLoja
    db.session.add(VendaSeruDiaLoja(
        data=data, loja_seru=loja_seru, n_pedidos=n,
        faturamento=Decimal(fat), faturamento_pedidos=Decimal(fat)))
    db.session.commit()


# ── pendências ───────────────────────────────────────────────────────────────

def test_pendencia_ordem_hoje_ausente(app):
    from app.services import briefing_dono
    chaves = {p['chave'] for p in briefing_dono.pendencias()}
    assert 'ordem_ausente' in chaves


def test_pendencia_ordem_rascunho_nao_enviado(app):
    from app.services import briefing_dono
    _plano(hoje(), enviado=False)
    chaves = {p['chave'] for p in briefing_dono.pendencias()}
    assert 'ordem_rascunho' in chaves
    assert 'ordem_ausente' not in chaves


def test_ordem_enviada_nao_gera_pendencia(app):
    from app.services import briefing_dono
    _plano(hoje(), enviado=True)
    chaves = {p['chave'] for p in briefing_dono.pendencias()}
    assert 'ordem_rascunho' not in chaves
    assert 'ordem_ausente' not in chaves


def test_pendencia_producao_vencida_soma_falta(app):
    from app.services import briefing_dono
    _plano(hoje() - timedelta(days=1), enviado=True,
           itens=[(50, 20), (10, 10)])          # falta 30 + falta 0
    it = next(p for p in briefing_dono.pendencias()
              if p['chave'] == 'producao_vencida')
    assert it['qtd'] == 30


def test_pendencia_orcamento_parado(app, catalogo):
    from app.models import Orcamento
    from app.services import briefing_dono
    db.session.add(Orcamento(codigo='ORC-T-0001', cliente_nome='Avulso',
                             status='rascunho', subtotal=Decimal('10'),
                             valor_total=Decimal('10')))
    db.session.commit()
    it = next(p for p in briefing_dono.pendencias()
              if p['chave'] == 'orcamentos')
    assert it['qtd'] == 1


def test_orcamento_arquivado_fora(app):
    from app.models import Orcamento
    from app.services import briefing_dono
    from app.utils import agora
    db.session.add(Orcamento(codigo='ORC-T-0002', cliente_nome='Avulso',
                             status='rascunho', subtotal=Decimal('10'),
                             valor_total=Decimal('10'),
                             arquivado_em=agora()))
    db.session.commit()
    assert not [p for p in briefing_dono.pendencias()
                if p['chave'] == 'orcamentos']


def test_pendencia_conta_vencida(app):
    from app.models import ContaPagar
    from app.services import briefing_dono
    db.session.add(ContaPagar(origem_canal='C_RIB',
                              valor_total=Decimal('100'),
                              vencimento=hoje() - timedelta(days=2),
                              status='aberto', tipo_documento='boleto'))
    db.session.commit()
    it = next(p for p in briefing_dono.pendencias()
              if p['chave'] == 'contas_pagar')
    assert it['qtd'] == 1


def test_pendencia_vigia_doente(app):
    from app.models import AppConfig
    from app.services import briefing_dono
    AppConfig.set('site_vigia_quebrado_desde', '2026-07-16T06:00:00')
    db.session.commit()
    chaves = {p['chave'] for p in briefing_dono.pendencias()}
    assert 'site_vigia_quebrado_desde' in chaves


def test_vigia_doente_escondido_de_admin_comum(app):
    """As telas dos vigias são owner-only — admin comum não ganha o item
    (clicar daria 403; achado A2 da revisão)."""
    from app.models import AppConfig
    from app.services import briefing_dono
    AppConfig.set('site_vigia_quebrado_desde', '2026-07-16T06:00:00')
    db.session.commit()
    chaves = {p['chave'] for p in briefing_dono.pendencias(incluir_owner=False)}
    assert 'site_vigia_quebrado_desde' not in chaves


def test_incluir_owner_false_esconde_itens_owner(app, catalogo):
    """Órfãos de cesta (tela owner) não aparecem pro admin comum."""
    from app.models import ProdutoItem
    from app.services import briefing_dono
    db.session.add(ProdutoItem(produto_id=catalogo['produto'].id,
                               tipo='receita', item_nome='Fantasma',
                               quantidade=1))
    db.session.commit()
    com = {p['chave'] for p in briefing_dono.pendencias(incluir_owner=True)}
    sem = {p['chave'] for p in briefing_dono.pendencias(incluir_owner=False)}
    assert 'cestas_orfaos' in com
    assert 'cestas_orfaos' not in sem


# ── vendas de ontem / custo de IA ────────────────────────────────────────────

def test_vendas_ontem_compara_com_media_do_dow(app):
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, fat=1200)
    _venda_dia(ontem - timedelta(days=7), fat=1000)
    _venda_dia(ontem - timedelta(days=14), fat=1000)
    _venda_dia(ontem - timedelta(days=2), loja_seru='Loja A', fat=555)  # dow errado, fora
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['pdv_total'] == 1200.0
    lj = v['por_loja'][0]
    assert lj['media'] == 1000.0
    assert lj['delta_pct'] == 20.0


def test_loja_com_historico_e_venda_zero_aparece(app):
    """Loja que vende toda semana mas ZEROU ontem (PDV fora?) NÃO some do
    briefing — entra com R$ 0 e queda de 100% (achado A8 da revisão)."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem - timedelta(days=7), fat=1000)
    _venda_dia(ontem - timedelta(days=14), fat=1000)
    # ontem: NENHUMA linha pra Loja A
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    lj = next(x for x in v['por_loja'] if x['loja'] == 'Loja A')
    assert lj['faturamento'] == 0.0
    assert lj['delta_pct'] == -100.0


def test_vendas_ontem_inclui_site_pago_por_pago_em(app):
    from app.models import PedidoOnline
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    db.session.add(PedidoOnline(
        codigo='PO-1', nome_cliente='X', email_cliente='x@x.com',
        modo_entrega='retirada', valor_total=Decimal('80'),
        pago_em=datetime.combine(ontem, time(10, 0))))
    db.session.add(PedidoOnline(                      # criado ontem, NÃO pago
        codigo='PO-2', nome_cliente='Y', email_cliente='y@y.com',
        modo_entrega='retirada', valor_total=Decimal('999')))
    db.session.commit()
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['site_qtd'] == 1
    assert v['site_total'] == 80.0


def test_custo_ia_ontem_janela_fechada(app):
    from app.models import UsoIA
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    db.session.add(UsoIA(funcao='vigia', modelo='claude-sonnet-4-6',
                         custo_usd=Decimal('1.50'),
                         criado_em=datetime.combine(ontem, time(9, 0))))
    db.session.add(UsoIA(funcao='vigia', modelo='claude-sonnet-4-6',
                         custo_usd=Decimal('9.99'),
                         criado_em=datetime.combine(hoje(), time(1, 0))))
    db.session.commit()
    assert briefing_dono.custo_ia_ontem() == 1.5


# ── texto e envio ────────────────────────────────────────────────────────────

def test_montar_texto_tem_secoes(app):
    from app.services import briefing_dono
    _venda_dia(hoje() - timedelta(days=1), fat=500)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        texto = briefing_dono.montar_texto()
    assert 'Briefing O Pão' in texto
    assert 'Vendas de ontem' in texto
    assert 'Loja A' in texto
    assert 'IA ontem' in texto
    # sem plano de hoje → a pendência de ordem aparece
    assert 'Precisa de você' in texto


def test_enviar_briefing_manda_pro_dono(app):
    from app.services import briefing_dono
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
    with patch('app.services.vendas_diarias.garantir_capturado'), \
            patch('app.services.zapi.enviar_texto',
                  return_value={'ok': True}) as tx:
        r = briefing_dono.enviar_briefing()
    assert r['ok'] is True
    assert tx.call_count == 1
    assert tx.call_args[0][0] == '5511999999999'
    assert 'Briefing O Pão' in tx.call_args[0][1]


def test_enviar_briefing_sem_numero(app):
    from app.services import briefing_dono
    app.config['ZAPI_BOT_DONO_NUMERO'] = ''
    app.config['CHATWOOT_VIGIA_INFRA_NUMERO'] = ''
    r = briefing_dono.enviar_briefing()
    assert r['ok'] is False


# ── rota /admin/briefing ─────────────────────────────────────────────────────

def test_rota_briefing_exige_owner(app, admin_user, cliente):
    _login(cliente, admin_user)                  # admin comum, NÃO owner
    assert cliente.get('/admin/briefing').status_code == 403


def test_rota_briefing_owner_ve_preview(app, owner_user, cliente):
    _login(cliente, owner_user)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        resp = cliente.get('/admin/briefing')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Briefing diário' in body
    assert 'Texto exato da mensagem' in body


def test_rota_briefing_enviar_1_dispara(app, owner_user, cliente):
    _login(cliente, owner_user)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
    with patch('app.services.vendas_diarias.garantir_capturado'), \
            patch('app.services.zapi.enviar_texto',
                  return_value={'ok': True}) as tx:
        resp = cliente.get('/admin/briefing?enviar=1')
    assert resp.status_code == 302
    assert tx.call_count == 1


# ── home: bloco "Precisa de você hoje" ───────────────────────────────────────

def test_home_admin_mostra_pendencias(app, admin_user, cliente):
    _login(cliente, admin_user)
    resp = cliente.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Precisa de você hoje' in body
    # sem ordem de hoje → o item aparece com link pro cronograma
    assert 'sem ordem de produção enviada' in body
    assert '/telaindustriateste/' in body
    assert 'Manual de operação' in body


def test_home_tudo_ok_mostra_estado_verde(app, admin_user, cliente):
    _plano(hoje(), enviado=True)
    _login(cliente, admin_user)
    body = cliente.get('/').get_data(as_text=True)
    assert 'Nada pendente' in body


# ── manual de operação ───────────────────────────────────────────────────────

def test_manual_renderiza_para_admin(app, admin_user, cliente):
    _login(cliente, admin_user)
    resp = cliente.get('/admin/manual')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Manual de operação' in body
    assert 'RODA SOZINHO' in body
    assert 'DIÁRIO' in body
    assert 'SEMANAL' in body
    assert '/telaindustriateste/' in body


def test_manual_no_menu_da_area_administracao(app, owner_user, cliente):
    _login(cliente, owner_user)
    body = cliente.get('/area/administracao').get_data(as_text=True)
    assert '/admin/manual' in body
    assert '/admin/briefing' in body             # owner vê o briefing


def test_briefing_nao_aparece_pra_admin_comum(app, admin_user, cliente):
    _login(cliente, admin_user)
    body = cliente.get('/area/administracao').get_data(as_text=True)
    assert '/admin/manual' in body
    assert '/admin/briefing' not in body


# ── sonda /api/claude/acuracia ───────────────────────────────────────────────

def test_api_acuracia_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    assert app.test_client().get('/api/claude/acuracia').status_code == 401


def test_api_acuracia_devolve_resumo_e_por_item(app, loja, catalogo):
    from app.models import PrevisaoSnapshot
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    rid = catalogo['receita'].id
    # 5 datas-alvo distintas (min_n do acuracia_por_loja_receita)
    for i in range(5):
        db.session.add(PrevisaoSnapshot(
            data_alvo=hoje() - timedelta(days=i + 1), loja_id=loja.id,
            receita_id=rid, previsto=10, realizado=5,
            motor='venda_estoque', lead_dias=0))
    db.session.commit()
    resp = app.test_client().get(
        '/api/claude/acuracia?dias=30',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['ok'] is True
    assert 'resumo' in j
    linhas = j['por_loja_receita']['venda_estoque']
    assert len(linhas) == 1
    assert linhas[0]['receita'] == catalogo['receita'].nome
    assert linhas[0]['wape_pct'] == 100.0        # |10-5|*5 / (5*5) = 100%
