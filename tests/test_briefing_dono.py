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

def test_vendas_ontem_compara_com_a_semana_passada(app):
    """Decisão do dono 23/07/2026: "sexta vs sexta passada" — a base é a MESMA
    data 7 dias antes, NÃO a média de várias semanas."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, fat=1200)
    _venda_dia(ontem - timedelta(days=7), fat=1000)     # <- a base
    _venda_dia(ontem - timedelta(days=14), fat=400)     # 2 semanas: IGNORADA
    _venda_dia(ontem - timedelta(days=2), loja_seru='Loja A', fat=555)  # fora
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['pdv_total'] == 1200.0
    assert v['comparado_com'] == ontem - timedelta(days=7)
    lj = v['por_loja'][0]
    assert lj['base'] == 1000.0        # só a semana passada (média daria 700)
    assert lj['delta_pct'] == 20.0


def test_dois_companies_da_mesma_loja_somam_numa_linha(app):
    """Caso real (dono, 17/07/2026): "Bread & Brew e O Pão Filial Nebraska
    são a mesma loja" — dois company names do Seru vinculados à MESMA Loja
    aparecem como UMA linha, com faturamento e média SOMADOS."""
    from app.models import Loja, SeruLojaMap
    from app.services import briefing_dono
    from app.utils import agora
    nebraska = Loja(nome='Loja Nebraska', ativa=True)
    db.session.add(nebraska)
    db.session.commit()
    db.session.add_all([
        SeruLojaMap(seru_company_name='BREAD & BREW', loja_id=nebraska.id,
                    confirmado_em=agora()),
        SeruLojaMap(seru_company_name='O PAO FILIAL - NEBRASKA',
                    loja_id=nebraska.id, confirmado_em=agora()),
    ])
    db.session.commit()
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='BREAD & BREW', fat=300)
    _venda_dia(ontem, loja_seru='O PAO FILIAL - NEBRASKA', fat=700)
    _venda_dia(ontem - timedelta(days=7), loja_seru='BREAD & BREW', fat=200)
    _venda_dia(ontem - timedelta(days=7),
               loja_seru='O PAO FILIAL - NEBRASKA', fat=300)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    nomes = [x['loja'] for x in v['por_loja']]
    assert nomes.count('Loja Nebraska') == 1
    assert 'BREAD & BREW' not in nomes
    lj = next(x for x in v['por_loja'] if x['loja'] == 'Loja Nebraska')
    assert lj['faturamento'] == 1000.0           # 300 + 700
    assert lj['base'] == 500.0                   # (200+300) somados no dia


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


def test_loja_sem_venda_na_semana_passada_fica_sem_comparacao(app):
    """Sem base (loja nova / PDV fora naquele dia) NÃO inventa percentual: o
    delta vem None e a tela mostra "sem comparação". Não existe % sobre zero."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=900)
    _venda_dia(ontem - timedelta(days=14), loja_seru='Loja A', fat=800)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    lj = next(x for x in v['por_loja'] if x['loja'] == 'Loja A')
    assert lj['faturamento'] == 900.0
    assert lj['base'] is None and lj['delta_pct'] is None
    assert v['pdv_delta_pct'] is None


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


def test_vendas_ontem_total_geral_e_base_do_total(app):
    """total_geral = PDV + site; pdv_base compara o TOTAL de ontem com o total
    da SEMANA PASSADA (soma das lojas na data-base)."""
    from app.models import PedidoOnline
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=700)
    _venda_dia(ontem, loja_seru='Loja B', fat=500)          # total ontem 1200
    # semana passada: 600+400 = 1000 (a base). 2 semanas atrás é ignorada.
    _venda_dia(ontem - timedelta(days=7), loja_seru='Loja A', fat=600)
    _venda_dia(ontem - timedelta(days=7), loja_seru='Loja B', fat=400)
    _venda_dia(ontem - timedelta(days=14), loja_seru='Loja A', fat=50)
    db.session.add(PedidoOnline(
        codigo='PO-T', nome_cliente='X', email_cliente='x@x.com',
        modo_entrega='retirada', valor_total=Decimal('300'),
        pago_em=datetime.combine(ontem, time(12, 0))))
    db.session.commit()
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['pdv_total'] == 1200.0
    assert v['site_total'] == 300.0
    assert v['total_geral'] == 1500.0
    assert v['pdv_base'] == 1000.0
    assert v['pdv_delta_pct'] == 20.0


def test_vendas_ontem_snapshot_ok_reflete_captura(app):
    """snapshot_ok=True só quando VendaSeruDiaria tem o dia de ontem — a home
    avisa em vez de mostrar um R$ 0 falso quando o snapshot não existe."""
    from app.models import VendaSeruDiaria
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        assert briefing_dono.vendas_ontem(capturar=False)['snapshot_ok'] is False
        db.session.add(VendaSeruDiaria(data=ontem, loja_seru='Loja A',
                                       seru_nome='Croissant', qtd=1,
                                       faturamento=Decimal('10'), n_pedidos=1))
        db.session.commit()
        assert briefing_dono.vendas_ontem(capturar=False)['snapshot_ok'] is True


def test_vendas_ontem_capturar_false_nao_chama_captura(app):
    """O modo da home NUNCA pode bater na API Seru (garantir_capturado)."""
    from app.services import briefing_dono
    with patch('app.services.vendas_diarias.garantir_capturado') as gc:
        briefing_dono.vendas_ontem(capturar=False)
    gc.assert_not_called()


def test_vendas_hoje_soma_pdv_e_site_de_hoje(app):
    """vendas_hoje: snapshot do PDV de HOJE + site pago hoje, sem delta
    (dia incompleto não se compara com média de dia cheio)."""
    from app.models import PedidoOnline
    from app.services import briefing_dono
    from app.utils import agora
    _venda_dia(hoje(), loja_seru='Loja A', fat=900, n=9)
    _venda_dia(hoje() - timedelta(days=1), loja_seru='Loja A', fat=555)  # ontem, fora
    db.session.add(PedidoOnline(
        codigo='PO-H', nome_cliente='X', email_cliente='x@x.com',
        modo_entrega='retirada', valor_total=Decimal('100'),
        pago_em=agora()))
    db.session.commit()
    v = briefing_dono.vendas_hoje()
    assert v['pdv_total'] == 900.0
    assert v['n_pedidos'] == 9
    assert v['site_total'] == 100.0
    assert v['total_geral'] == 1000.0
    assert v['por_loja'][0]['loja'] == 'Loja A'
    assert 'delta_pct' not in v['por_loja'][0]


def test_vendas_hoje_default_nao_chama_captura(app):
    """O modo da home NUNCA bate na API Seru — default capturar=False."""
    from app.services import briefing_dono
    with patch('app.services.vendas_diarias.garantir_capturado') as gc:
        briefing_dono.vendas_hoje()
    gc.assert_not_called()


def _breakdown(data, dim, chave, valor, loja_seru='Loja A'):
    from app.models import VendaSeruDiaBreakdown
    db.session.add(VendaSeruDiaBreakdown(
        data=data, loja_seru=loja_seru, dimensao=dim, chave=chave,
        valor=Decimal(str(valor))))
    db.session.commit()


def test_vendas_ontem_inclui_cancelamentos_e_descontos(app):
    """Cockpit da home: cancelamentos (contagem+valor) e descontos de ontem,
    lidos SÓ do snapshot (VendaSeruDiaBreakdown)."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, fat=1000)
    _breakdown(ontem, 'cancelados', '', 2)      # 2 cancelamentos
    _breakdown(ontem, 'cancelados', 'v', 85.0)  # R$ 85 cancelados
    _breakdown(ontem, 'desconto', '', 12.5)     # R$ 12,50 de desconto
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem(capturar=False)
    assert v['cancelados_n'] == 2
    assert v['cancelados_valor'] == 85.0
    assert v['desconto'] == 12.5


def test_vendas_hoje_inclui_cancelamentos_e_descontos(app):
    from app.services import briefing_dono
    hoje_d = hoje()
    _venda_dia(hoje_d, fat=900, n=9)
    _breakdown(hoje_d, 'cancelados', '', 1)
    _breakdown(hoje_d, 'cancelados', 'v', 40.0)
    _breakdown(hoje_d, 'desconto', '', 7.0)
    v = briefing_dono.vendas_hoje()
    assert v['cancelados_n'] == 1
    assert v['cancelados_valor'] == 40.0
    assert v['desconto'] == 7.0


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
    # critico=True: 1 msg/dia que não pode virar digest na manhã de um
    # incidente (achado A3 da revisão).
    assert tx.call_args[1].get('critico') is True


def test_enviar_briefing_nao_usa_numero_de_grupo_dos_vigias(app):
    """O destino é SÓ ZAPI_BOT_DONO_NUMERO — o número dos vigias pode ser
    um GRUPO da equipe e o briefing carrega faturamento (achado A1)."""
    from app.services import briefing_dono
    app.config['ZAPI_BOT_DONO_NUMERO'] = ''
    app.config['CHATWOOT_VIGIA_INFRA_NUMERO'] = '123456-group'
    r = briefing_dono.enviar_briefing('texto qualquer')
    assert r['ok'] is False                      # não caiu no grupo


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


# ── Drill-down: abrir cancelamentos/descontos (detalhe ao vivo) ───────────────

def _pedido_cd(pid, code, loja, total, *, dia, hh='13', canceled=False,
               desconto=0.0, subtotal=0.0, caixa=None, nf=False):
    """Pedido cru do Seru pro detalhe ao vivo (createdAt UTC → BRT no dia)."""
    p = {'id': pid, 'code': code, 'company': {'name': loja},
         'createdAt': '%sT%s:00:00Z' % (dia.isoformat(), hh),
         'canceledAt': ('%sT23:00:00Z' % dia.isoformat()) if canceled else None,
         'total': total, 'subtotal': subtotal, 'discount': desconto,
         'items': [] if canceled else [{'name': 'Pao', 'quantity': 1, 'total': total}]}
    if caixa:
        p['cashier'] = {'code': caixa}
    if nf:
        p['taxInvoice'] = {'status': 'authorized'}
    return p


def test_cancelados_descontos_detalhe(app):
    from datetime import date

    from app.services import briefing_dono
    dia = date(2026, 6, 15)
    pedidos = [
        _pedido_cd(1, 'A1', 'Loja A', 50.0, dia=dia, hh='16',
                   canceled=True, caixa='CX1', nf=True),
        _pedido_cd(2, 'A2', 'Loja A', 40.0, dia=dia, hh='14',
                   desconto=10.0, subtotal=50.0),
        _pedido_cd(3, 'A3', 'Loja A', 30.0, dia=dia),        # normal, fora
        _pedido_cd(4, 'A4', 'Loja A', 99.0, dia=date(2026, 6, 14),
                   desconto=5.0, subtotal=104.0),            # outro dia, fora
    ]
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        d = briefing_dono.cancelados_descontos_detalhe(dia)
    assert len(d['cancelados']) == 1
    assert d['cancelados'][0]['codigo'] == 'A1'
    assert d['cancelados'][0]['valor'] == 50.0
    assert d['cancelados'][0]['caixa'] == 'CX1'
    assert d['cancelados'][0]['nf'] is True
    assert d['cancelados_valor'] == 50.0
    assert len(d['descontos']) == 1
    assert d['descontos'][0]['codigo'] == 'A2'
    assert d['descontos'][0]['desconto'] == 10.0
    assert d['descontos'][0]['subtotal'] == 50.0
    assert d['descontos'][0]['total'] == 40.0
    assert d['desconto_total'] == 10.0


def test_detalhe_desconto_de_cancelado_nao_conta(app):
    """Desconto de um pedido CANCELADO não entra na lista de descontos."""
    from datetime import date

    from app.services import briefing_dono
    dia = date(2026, 6, 15)
    pedidos = [_pedido_cd(9, 'C9', 'Loja A', 20.0, dia=dia,
                          canceled=True, desconto=7.0, subtotal=27.0)]
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        d = briefing_dono.cancelados_descontos_detalhe(dia)
    assert len(d['cancelados']) == 1
    assert d['descontos'] == [] and d['desconto_total'] == 0.0


def test_rota_detalhe_owner_ve_json(app, owner_user, cliente):
    from app.utils import hoje
    _login(cliente, owner_user)
    pedidos = [_pedido_cd(1, 'H1', 'Loja A', 50.0, dia=hoje(), hh='13',
                          canceled=True, nf=False)]
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        r = cliente.get('/admin/vendas/cancelados-descontos?dia=' + hoje().isoformat())
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['dia'] == hoje().isoformat()
    assert len(j['cancelados']) == 1 and j['cancelados'][0]['codigo'] == 'H1'


def test_rota_detalhe_nao_owner_403(app, admin_user, cliente):
    _login(cliente, admin_user)
    assert cliente.get('/admin/vendas/cancelados-descontos').status_code == 403


def test_rota_detalhe_dia_fora_da_janela_400(app, owner_user, cliente):
    """Só hoje/ontem — dia arbitrário não pode disparar consulta ampla à API."""
    _login(cliente, owner_user)
    r = cliente.get('/admin/vendas/cancelados-descontos?dia=2020-01-01')
    assert r.status_code == 400


def test_rota_detalhe_dia_malformado_400(app, owner_user, cliente):
    """Data inválida → 400 explícito (não cai silenciosamente pra hoje)."""
    _login(cliente, owner_user)
    r = cliente.get('/admin/vendas/cancelados-descontos?dia=banana')
    assert r.status_code == 400


def test_detalhe_pedido_torto_nao_derruba_o_modal(app):
    """Campo malformado (total não-numérico, cashier não-dict) é tolerado —
    o cancelado aparece com valor 0 e o resto do dia continua; nunca 502."""
    from datetime import date

    from app.services import briefing_dono
    dia = date(2026, 6, 15)
    torto = _pedido_cd(7, 'BAD', 'Loja A', 0.0, dia=dia, canceled=True)
    torto['total'] = 'R$ dez'                    # lixo não-numérico
    torto['cashier'] = 'nao-e-dict'              # cashier não-dict
    bom = _pedido_cd(8, 'OK', 'Loja A', 25.0, dia=dia, desconto=5.0, subtotal=30.0)
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=[torto, bom]):
        d = briefing_dono.cancelados_descontos_detalhe(dia)
    # o pedido bom (desconto) aparece intacto
    assert len(d['descontos']) == 1 and d['descontos'][0]['codigo'] == 'OK'
    assert d['desconto_total'] == 5.0
    # o torto foi tolerado (valor 0, caixa None), não derrubou a consulta
    assert len(d['cancelados']) == 1
    assert d['cancelados'][0]['codigo'] == 'BAD'
    assert d['cancelados'][0]['valor'] == 0.0
    assert d['cancelados'][0]['caixa'] is None


def test_rota_detalhe_seru_fora_502(app, owner_user, cliente):
    from app.utils import hoje
    _login(cliente, owner_user)
    with patch('app.services.seru.listar_pedidos_completo',
               side_effect=RuntimeError('boom')):
        r = cliente.get('/admin/vendas/cancelados-descontos?dia=' + hoje().isoformat())
    assert r.status_code == 502
    assert r.get_json()['ok'] is False


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


# ── home: vendas TOTAIS (só dono) ────────────────────────────────────────────

def test_home_dono_ve_vendas_totais(app, owner_user, cliente):
    from app.models import VendaSeruDiaria
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=800)
    db.session.add(VendaSeruDiaria(data=ontem, loja_seru='Loja A',
                                   seru_nome='Croissant', qtd=1,
                                   faturamento=Decimal('800'), n_pedidos=10))
    db.session.commit()
    _login(cliente, owner_user)
    with patch('app.services.vendas_diarias.garantir_capturado') as gc:
        body = cliente.get('/').get_data(as_text=True)
    gc.assert_not_called()                       # home nunca bate na API
    # ATENÇÃO: não usar 'Vendas de ontem' como marcador do painel — a
    # sidebar tem um title= com esse texto e o assert passaria sempre.
    assert 'Vendas de hoje' in body              # seção de hoje (painel)
    assert 'R$ 800' in body                      # total de ontem
    assert 'Loja A' in body
    # snapshot presente → sem aviso de captura pendente
    assert 'snapshot de ontem' not in body


def test_home_admin_comum_nao_ve_vendas(app, admin_user, cliente):
    """Faturamento é o cockpit pessoal do dono — admin comum não vê o bloco
    (mesmo gate do /admin/briefing)."""
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=800)
    _login(cliente, admin_user)
    body = cliente.get('/').get_data(as_text=True)
    assert 'Vendas de hoje' not in body
    assert 'R$ 800' not in body


def test_home_dono_sem_snapshot_avisa(app, owner_user, cliente):
    """Sem snapshot de ontem, a home avisa em vez de fingir R$ 0 — o
    contrato de DINHEIRO vale nas DUAS interfaces (v2 default e a
    clássica via cookie)."""
    _login(cliente, owner_user)
    body = cliente.get('/').get_data(as_text=True)          # v2 (default)
    assert 'pode estar incompleto' in body
    app.config['UI_V2_ENABLED'] = False                     # clássica
    body = cliente.get('/').get_data(as_text=True)
    assert 'Ontem' in body
    assert 'snapshot de ontem' in body


def test_home_dono_ve_vendas_de_hoje(app, owner_user, cliente):
    """A home mostra HOJE (parcial) em destaque além de ontem."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    _venda_dia(hoje(), loja_seru='Loja A', fat=450)
    _login(cliente, owner_user)
    with patch('app.services.vendas_diarias.garantir_capturado') as gc:
        body = cliente.get('/').get_data(as_text=True)
    gc.assert_not_called()
    assert 'Vendas de hoje' in body
    assert 'R$ 450' in body
    assert 'parciais, atualiza a cada ~15 min' in body


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


# ── PDV do Tiny (Cantina) no cockpit do dono (01/08/2026) ────────────────────
#
# Pergunta do dono: "e como eu sei o faturamento da cantina?". A Cantina vende
# pelo PDV do TINY, não pelo Seru — o painel 💰 da home mostrava a padaria
# inteira MENOS ela, e o "Total" saía subestimado.

def _venda_tiny(data, valor=100, pid='t1', loja=None, cancelada=False):
    from app.models import AppConfig, Loja, TinyPedidoProcessado
    from app.services import tiny_pdv_sync
    from app.utils import agora
    if loja is None:
        loja = Loja.query.filter_by(nome='Cantina').first()
        if loja is None:
            loja = Loja(nome='Cantina', ativa=True)
            db.session.add(loja)
            db.session.commit()
        AppConfig.set(tiny_pdv_sync._CFG_LOJA, loja.id)
    db.session.add(TinyPedidoProcessado(
        tiny_pedido_id=pid, loja_id=loja.id, data_pedido=data,
        valor=Decimal(valor), situacao='Faturado', n_itens_total=1,
        n_itens_baixados=1, cancelado_em=agora() if cancelada else None))
    db.session.commit()
    return loja


def test_vendas_ontem_inclui_o_pdv_do_tiny(app):
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=1000)
    _venda_tiny(ontem, valor=250, pid='t1')
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['tiny_total'] == 250.0
    assert v['pdv_total'] == 1250.0            # Seru + Tiny
    assert v['total_geral'] == 1250.0
    cantina = next(x for x in v['por_loja'] if x['loja'] == 'Cantina')
    assert cantina['faturamento'] == 250.0


def test_vendas_ontem_compara_a_cantina_com_a_semana_passada(app):
    """A Cantina entra na MESMA regra das outras lojas: sábado vs sábado."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_tiny(ontem, valor=300, pid='t1')
    _venda_tiny(ontem - timedelta(days=7), valor=200, pid='t0')
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    lj = next(x for x in v['por_loja'] if x['loja'] == 'Cantina')
    assert lj['base'] == 200.0
    assert lj['delta_pct'] == 50.0


def test_cantina_sem_historico_fica_sem_comparacao(app):
    """A integração nasceu em 27/07 e o sync só cobre ontem+hoje: no começo
    não existe a semana passada. Isso é FALTA DE HISTÓRICO, não queda — o
    delta vem None ("sem comparação"), nunca -100%."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_tiny(ontem, valor=300, pid='t1')
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    lj = next(x for x in v['por_loja'] if x['loja'] == 'Cantina')
    assert lj['base'] is None and lj['delta_pct'] is None


def test_venda_tiny_cancelada_nao_conta_no_cockpit(app):
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_tiny(ontem, valor=300, pid='t1')
    _venda_tiny(ontem, valor=999, pid='t2', cancelada=True)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['tiny_total'] == 300.0


def test_vendas_hoje_inclui_o_pdv_do_tiny(app):
    from app.services import briefing_dono
    _venda_dia(hoje(), loja_seru='Loja A', fat=500)
    _venda_tiny(hoje(), valor=120, pid='t1')
    v = briefing_dono.vendas_hoje()
    assert v['tiny_total'] == 120.0
    assert v['pdv_total'] == 620.0
    assert v['total_geral'] == 620.0
    assert any(x['loja'] == 'Cantina' for x in v['por_loja'])


def test_sem_venda_no_tiny_o_cockpit_nao_muda(app):
    """Regressão: sem Tiny configurado/importado o painel é o de sempre e
    `tiny_total` fica em 0 (nenhuma linha nova, nenhum aviso na tela)."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=1000)
    with patch('app.services.vendas_diarias.garantir_capturado'):
        v = briefing_dono.vendas_ontem()
    assert v['tiny_total'] == 0
    assert v['pdv_total'] == 1000.0
    assert [x['loja'] for x in v['por_loja']] == ['Loja A']


def test_texto_do_whatsapp_explicita_o_tiny(app):
    """O dono compara o número do briefing com o /pdv/ (que é SÓ Seru) — sem
    a menção, a diferença viraria caça ao erro."""
    from app.services import briefing_dono
    ontem = hoje() - timedelta(days=1)
    _venda_dia(ontem, loja_seru='Loja A', fat=1000)
    _venda_tiny(ontem, valor=250, pid='t1')
    with patch('app.services.vendas_diarias.garantir_capturado'):
        texto = briefing_dono.montar_texto()
    assert 'inclui Tiny' in texto
    assert 'Cantina' in texto
