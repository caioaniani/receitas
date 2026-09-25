"""Fermentação: consumo fechado por loja, regra semanal e entrega idempotente.

Usa snapshots e composições reais no banco de teste. Toda chamada ao Slack
é bloqueada por padrão e substituída explicitamente nos testes de envio.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    FermentacaoEnvio,
    Loja,
    Produto,
    ProdutoItem,
    Receita,
    SeruLojaMap,
    VendaMapa,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.services import fermentacao

ALVO = date(2026, 9, 26)
SABADOS = [date(2026, 8, 8), date(2026, 8, 15), date(2026, 8, 22),
           date(2026, 8, 29), date(2026, 9, 5), date(2026, 9, 12),
           date(2026, 9, 19)]


@pytest.fixture(autouse=True)
def _sem_rede_e_relogio_fixo(monkeypatch, congela_hoje):
    congela_hoje(2026, 9, 25, 11)

    def proibido(*args, **kwargs):
        pytest.fail('Teste de fermentação tentou enviar mensagem sem mock explícito.')

    monkeypatch.setattr('app.services.slack.post_message', proibido)
    monkeypatch.setattr('app.services.slack.update_message', proibido)


def _receita(nome):
    receita = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add(receita)
    db.session.flush()
    return receita


def _loja(nome):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(SeruLojaMap(
        seru_company_name=nome, loja_id=loja.id, ignorar=False,
        confirmado_em=datetime(2026, 8, 1, 9)))
    return loja


def _linha(loja, dia, nome, qtd):
    linha = VendaSeruDiaria(
        data=dia, loja_seru=loja.nome, loja_id=loja.id, seru_nome=nome,
        qtd=qtd, atualizado_em=datetime.combine(dia + timedelta(days=1), time(3)))
    db.session.add(linha)
    return linha


def _dia(loja, dia, croissant, pain):
    db.session.add(VendaSeruDiaLoja(
        data=dia, loja_seru=loja.nome, loja_id=loja.id, n_pedidos=1,
        atualizado_em=datetime.combine(dia + timedelta(days=1), time(3))))
    _linha(loja, dia, 'Croissant Francês', croissant)
    _linha(loja, dia, 'Pain au Chocolat', pain)


@pytest.fixture
def cenario(app):
    croissant = _receita('Croissant Tradicional')
    pain = _receita('Pain au Chocolat')
    db.session.add_all([
        VendaMapa(canal='seru', nome_externo='Croissant Francês',
                  receita_id=croissant.id, fator_quantidade=1),
        VendaMapa(canal='seru', nome_externo='Pain au Chocolat',
                  receita_id=pain.id, fator_quantidade=1),
    ])
    ribeiro = _loja('Ribeiro do Vale')
    anesio = _loja('Anésio Pinto Rosa')
    for dia, cro, pa in zip(SABADOS, [39, 42, 47, 51, 52, 56, 60],
                           [2, 9, 4, 8, 3, 6, 5]):
        _dia(ribeiro, dia, cro, pa)
    for dia, cro, pa in zip(SABADOS[-3:], [4, 7, 9], [1, 2, 4]):
        _dia(anesio, dia, cro, pa)
    db.session.commit()
    return {'ribeiro': ribeiro, 'anesio': anesio,
            'croissant': croissant, 'pain': pain}


def _resultado_loja(resultado, loja):
    return next(item for item in resultado['lojas'] if item['loja_id'] == loja.id)


@pytest.mark.parametrize('deslocamento', range(7))
def test_sete_ocorrencias_sao_do_mesmo_dia_da_semana(deslocamento):
    alvo = ALVO + timedelta(days=deslocamento)
    datas = fermentacao.datas_base(alvo, semanas=7)
    assert datas == [alvo - timedelta(weeks=n) for n in range(7, 0, -1)]
    assert len(set(datas)) == 7
    assert all(dia.weekday() == alvo.weekday() and dia < alvo for dia in datas)
    if deslocamento == 0:
        assert datas == SABADOS


def test_ribeiro_maior_e_quarto_maior_com_teto_e_pain_independente(cenario):
    resultado = fermentacao.calcular(ALVO)
    assert resultado['ok'], resultado['erros']
    ribeiro = _resultado_loja(resultado, cenario['ribeiro'])
    assert ribeiro['metodo'] == 'maior_quarto_7'
    assert ribeiro['semanas'] == 7
    assert [d['data'] for d in ribeiro['dias']] == [d.isoformat() for d in SABADOS]
    assert ribeiro['croissant'] == 56  # (60 + 51) / 2 = 55,5, arredondado para cima.
    assert ribeiro['pain'] == 7        # (9 + 5) / 2, ordenação própria do pain.
    detalhe = ribeiro['calculos']['croissant']
    assert list(map(Decimal, detalhe['ordenadas'])) == list(map(Decimal, [60, 56, 52, 51, 47, 42, 39]))
    assert Decimal(detalhe['maior']) == 60
    assert Decimal(detalhe['quarto_maior']) == 51
    assert Decimal(detalhe['referencia']) == Decimal('55.5')
    assert 'media_croissant' not in ribeiro


def test_ribeiro_ignora_oitavo_sabado_e_dias_da_semana_diferentes(cenario):
    ribeiro = cenario['ribeiro']
    _dia(ribeiro, date(2026, 8, 1), 9999, 9999)
    _dia(ribeiro, date(2026, 9, 18), 9999, 9999)
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert resultado['ok'], resultado['erros']
    calculo = _resultado_loja(resultado, ribeiro)
    assert calculo['croissant'] == 56
    assert calculo['pain'] == 7


def test_quarta_posicao_conta_empates_e_zero_fechado_e_valido(cenario):
    ribeiro = cenario['ribeiro']
    for dia, qtd in zip(SABADOS, [0, 3, 3, 9, 9, 9, 9]):
        VendaSeruDiaria.query.filter_by(
            loja_id=ribeiro.id, data=dia, seru_nome='Croissant Francês').one().qtd = qtd
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert resultado['ok'], resultado['erros']
    calculo = _resultado_loja(resultado, ribeiro)
    assert calculo['croissant'] == 9
    assert Decimal(calculo['calculos']['croissant']['quarto_maior']) == 9
    assert Decimal(calculo['dias'][0]['croissant']) == 0


def test_anesio_preserva_media_de_tres_ocorrencias(cenario):
    anesio = cenario['anesio']
    _dia(anesio, SABADOS[-4], 9999, 9999)
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert resultado['ok'], resultado['erros']
    calculo = _resultado_loja(resultado, anesio)
    assert calculo['metodo'] == 'media_3'
    assert calculo['semanas'] == 3
    assert [d['data'] for d in calculo['dias']] == [d.isoformat() for d in SABADOS[-3:]]
    assert calculo['croissant'] == 7
    assert calculo['pain'] == 3
    assert Decimal(calculo['media_croissant']) == Decimal(20) / 3


@pytest.mark.parametrize('problema', ['sem_total', 'sem_itens', 'intradia', 'venda_sem_produto'])
def test_historico_incompleto_bloqueia_lista_sem_substituir_por_zero(cenario, problema):
    loja = cenario['ribeiro']
    dia = SABADOS[0]
    if problema == 'sem_total':
        db.session.delete(VendaSeruDiaLoja.query.filter_by(loja_id=loja.id, data=dia).one())
    elif problema == 'sem_itens':
        for linha in VendaSeruDiaria.query.filter_by(loja_id=loja.id, data=dia).all():
            db.session.delete(linha)
    elif problema == 'intradia':
        VendaSeruDiaria.query.filter_by(loja_id=loja.id, data=dia).first().atualizado_em = datetime.combine(dia, time(20))
    else:
        db.session.add(VendaSeruDiaBreakdown(
            data=dia, loja_seru=loja.nome, dimensao='sem_itens', chave='', valor=1))
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert not resultado['ok']
    assert any('Ribeiro do Vale, 08/08' in erro for erro in resultado['erros'])
    assert 'Não foi possível calcular' in resultado['texto']
    assert '56 croissants' not in resultado['texto']


def test_pagina_nao_mostra_orientacao_parcial_quando_calculo_tem_erro(app, cenario):
    from flask import render_template

    db.session.delete(VendaSeruDiaLoja.query.filter_by(
        loja_id=cenario['ribeiro'].id, data=SABADOS[0]).one())
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert not resultado['ok']
    assert _resultado_loja(resultado, cenario['anesio'])['croissant'] == 7
    with app.test_request_context('/admin/slack/fermentacao'):
        html = render_template('main/slack_fermentacao.html', calculo=resultado,
                               envio=None, agendador_ativo=True)
    assert 'Não foi possível calcular' in html
    assert 'unidades para fermentar' not in html


def test_consumo_inclui_lanches_chapa_e_nutella_exclui_almond_e_outros_retornos(cenario):
    ribeiro, croissant = cenario['ribeiro'], cenario['croissant']
    retorno = _receita('Croissant Tradicional — Retorno')
    croissant.retorno_receita_id = retorno.id
    produtos = [
        ('Lanche de croissant', croissant, 2, 4),
        ('Croissant na chapa', croissant, 1, 3),
        ('Croissant de nutella', retorno, 1, 2),
        ('Croissant Nutella com morango', retorno, 1, 3),
        ('Croissant Almond', croissant, 1, 100),
        ('Torrada de retorno', retorno, 1, 100),
    ]
    for nome, componente, fator, vendas in produtos:
        produto = Produto(nome=nome, menu_configuravel=False)
        db.session.add(produto)
        db.session.flush()
        db.session.add(ProdutoItem(
            produto_id=produto.id, tipo='receita', receita_id=componente.id,
            item_nome=componente.nome, quantidade=fator))
        db.session.add(VendaMapa(canal='seru', nome_externo=nome,
                                produto_id=produto.id, fator_quantidade=1))
        for dia in SABADOS:
            _linha(ribeiro, dia, nome, vendas)
    for linha in VendaSeruDiaria.query.filter_by(
            loja_id=ribeiro.id, seru_nome='Croissant Francês').all():
        linha.qtd = 10
    db.session.commit()
    resultado = fermentacao.calcular(ALVO)
    assert resultado['ok'], resultado['erros']
    calculo = _resultado_loja(resultado, ribeiro)
    assert calculo['croissant'] == 26  # 10 simples + 8 lanche + 3 chapa + 2 + 3 Nutella.
    assert all(Decimal(dia['croissant']) == 26 for dia in calculo['dias'])
    assert calculo['pain'] == 7
    fontes = calculo['dias'][0]['fontes']
    assert any(fonte['nome'] == 'Croissant de nutella' and any(
        c['grupo'] == 'croissant' for c in fonte['componentes']) for fonte in fontes)
    assert any(fonte['nome'] == 'Croissant Almond' and all(
        c['grupo'] is None for c in fonte['componentes']) for fonte in fontes)


@pytest.fixture
def slack_habilitado(app, monkeypatch):
    app.config['SLACK_CANAL_COPILOT'] = 'canal-teste-fermentacao'
    app.config['SLACK_BOT_TOKEN'] = 'token-falso-apenas-teste'
    monkeypatch.setattr('app.services.instancia.pode_falar_com_o_mundo', lambda *a: True)


def test_envio_persiste_calculo_e_repeticao_nao_publica_de_novo(cenario, slack_habilitado, monkeypatch):
    chamadas = []

    def publicar(canal, texto, **kwargs):
        chamadas.append((canal, texto, kwargs))
        assert db.session.get(FermentacaoEnvio, ALVO).estado == 'enviando'
        return {'ok': True, 'ts': '123.456'}

    monkeypatch.setattr('app.services.slack.post_message', publicar)
    assert fermentacao.enviar_amanha()['estado'] == 'enviado'
    assert fermentacao.enviar_amanha()['estado'] == 'enviado'
    assert len(chamadas) == 1
    assert chamadas[0][2] == {'retry': False}
    assert FermentacaoEnvio.query.count() == 1
    envio = db.session.get(FermentacaoEnvio, ALVO)
    assert envio.slack_ts == '123.456'
    assert _resultado_loja(envio.calculo, cenario['ribeiro'])['croissant'] == 56


def test_correcao_atualiza_mesma_mensagem_e_preserva_historico(cenario, slack_habilitado, monkeypatch):
    monkeypatch.setattr('app.services.slack.post_message', lambda *a, **kw: {'ok': True, 'ts': '123.456'})
    fermentacao.enviar_amanha()
    envio = db.session.get(FermentacaoEnvio, ALVO)
    texto_original = envio.texto
    linha = VendaSeruDiaria.query.filter_by(
        loja_id=cenario['ribeiro'].id, data=SABADOS[-1], seru_nome='Croissant Francês').one()
    linha.qtd = 81
    db.session.commit()
    chamadas = []
    monkeypatch.setattr('app.services.slack.update_message', lambda canal, ts, **kw:
                        chamadas.append((canal, ts, kw['text'])) or {'ok': True})
    assert fermentacao.enviar_amanha(corrigir=True)['estado'] == 'enviado'
    assert len(chamadas) == 1
    assert chamadas[0][:2] == ('canal-teste-fermentacao', '123.456')
    assert chamadas[0][2] != texto_original
    assert envio.calculo['historico_correcoes'][0]['texto'] == texto_original
    assert _resultado_loja(envio.calculo, cenario['ribeiro'])['croissant'] == 66
    fermentacao.enviar_amanha(corrigir=True)
    assert len(chamadas) == 1
    assert FermentacaoEnvio.query.count() == 1


def test_correcao_incerta_repete_texto_persistido_no_mesmo_ts(cenario, slack_habilitado, monkeypatch):
    monkeypatch.setattr('app.services.slack.post_message', lambda *a, **kw: {'ok': True, 'ts': '123.456'})
    fermentacao.enviar_amanha()
    linha = VendaSeruDiaria.query.filter_by(
        loja_id=cenario['ribeiro'].id, data=SABADOS[-1], seru_nome='Croissant Francês').one()
    linha.qtd = 81
    db.session.commit()
    chamadas = []

    def atualizar(canal, ts, **kw):
        chamadas.append((canal, ts, kw['text']))
        return {'ok': len(chamadas) > 1}

    monkeypatch.setattr('app.services.slack.update_message', atualizar)
    assert fermentacao.enviar_amanha(corrigir=True)['estado'] == 'correcao_incerta'
    linha.qtd = 99  # Retry conclui a tentativa anterior, mesmo se o histórico mudou.
    db.session.commit()
    assert fermentacao.enviar_amanha(corrigir=True)['estado'] == 'enviado'
    assert len(chamadas) == 2
    assert chamadas[0] == chamadas[1]
    envio = db.session.get(FermentacaoEnvio, ALVO)
    assert 'correcao_pendente' not in envio.calculo
    assert len(envio.calculo['historico_correcoes']) == 1
    assert _resultado_loja(envio.calculo, cenario['ribeiro'])['croissant'] == 66
