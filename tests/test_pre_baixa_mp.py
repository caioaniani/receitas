"""Pré-baixa de MP da ordem de produção enviada (pedido do dono 07/07/2026).

ENVIAR o plano ao padeiro reserva a MP da falta (baixa provisória, mesma
explosão de ficha da baixa real e da calculadora de compras); CONFIRMAR a
produção converte em baixa real e estorna a reserva na mesma transação —
o estoque líquido não muda na confirmação, só troca de natureza.

Regime: ordem enviada ANTES da feature (sem linhas PreBaixaMP) fica fora —
produzir/dispensar nela não pré-baixa retroativo. Reconciliador idempotente:
`producao.sincronizar_pre_baixa_mp`.

Ficha dos cenários: 1000 g de "Farinha Pre" (mp_direto) por batida,
peso_unitario 100 g → rendimento massa crua = 10 un/batida → 1 un = 100 g.
"""
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    Loja,
    MateriaPrima,
    MovimentacaoEstoque,
    PedidoItem,
    PedidoLoja,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaIngrediente,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()

ESTOQUE_INICIAL = 10000.0
G_POR_UN = 100.0


def _cenario(qtd=50, dias_entrega=2):
    mp = MateriaPrima(nome='Farinha Pre', unidade='g', custo_por_kg=5.0,
                      estoque_atual=ESTOQUE_INICIAL)
    r = Receita(nome='Pao Pre Baixa', categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0, peso_unitario=100.0)
    loja = Loja(nome='Loja PB', ativa=True)
    db.session.add_all([mp, r, loja])
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, ingrediente_nome=mp.nome,
                                      tipo='mp_direto', porcentagem=1000.0))
    dd = hoje() + timedelta(days=dias_entrega)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=qtd))
    db.session.commit()
    return mp, r, _dia_producao()


def _dia_producao(horizonte=7):
    """Dia em que o cronograma agendou a produção (receita com ficha é
    antecipada em relação à entrega — o teste não deve fixar a regra de
    antecedência, só seguir o grid)."""
    from app.services.previsao_producao import cronograma_producao
    crono = cronograma_producao(horizonte_dias=horizonte)
    for rec in crono['receitas']:
        for c in rec['por_dia']:
            if c.get('qtd'):
                return date.fromisoformat(c['data'])
    raise AssertionError('cronograma sem produção agendada')


def _falta_total(plano):
    return sum(max(0, int(it.qtd_alvo or 0) - int(it.produzido_qtd or 0))
               for it in plano.itens if it.dispensada_em is None)


def _movs(prefixo):
    return (MovimentacaoEstoque.query
            .filter(MovimentacaoEstoque.referencia.like(prefixo + '%')).all())


def test_enviar_cria_pre_baixa(app, admin_user):
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert plano is not None and plano.enviado_ao_padeiro is True
        esperado = _falta_total(plano) * G_POR_UN
        assert esperado > 0
        pb = PreBaixaMP.query.filter_by(plano_id=plano.id,
                                        materia_prima_id=mp.id).one()
        assert pb.quantidade == esperado
        saidas = _movs('Pré-baixa produção')
        assert len(saidas) == 1
        assert saidas[0].tipo == 'saida' and saidas[0].quantidade == esperado
        assert mp.estoque_atual == ESTOQUE_INICIAL - esperado


def test_reenviar_sem_mudanca_e_idempotente(app, admin_user):
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        estoque_1 = mp.estoque_atual
        n_movs_1 = MovimentacaoEstoque.query.count()
        plano2 = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert plano2.id == plano.id
        assert MovimentacaoEstoque.query.count() == n_movs_1   # nenhum novo
        assert mp.estoque_atual == estoque_1


def test_produzir_converte_pre_baixa_em_baixa_real(app, admin_user):
    """Confirmar N unidades: baixa real de N + estorno da pré-baixa de N —
    o estoque líquido NÃO muda na confirmação (já estava reservado)."""
    from app.services.producao import enviar_plano_do_dia, produzir_item_plano
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvo = _falta_total(plano)
        assert alvo > 20
        item = plano.itens[0]
        res = produzir_item_plano(item.id, 20, admin_user.id)
        assert res['ok'], res
        # estorno da pré-baixa das 20 un confirmadas
        estornos = _movs('Estorno pré-baixa produção')
        assert len(estornos) == 1
        assert estornos[0].tipo == 'entrada'
        assert estornos[0].quantidade == 20 * G_POR_UN
        # baixa real das 20 un (comportamento que já existia)
        reais = _movs('Produção %s' % r.nome)
        assert len(reais) == 1 and reais[0].tipo == 'saida'
        assert reais[0].quantidade == 20 * G_POR_UN
        # reserva restante = falta restante; líquido = alvo inteiro
        pb = PreBaixaMP.query.filter_by(plano_id=plano.id).one()
        assert pb.quantidade == (alvo - 20) * G_POR_UN
        assert mp.estoque_atual == ESTOQUE_INICIAL - alvo * G_POR_UN


def test_produzir_tudo_zera_reserva(app, admin_user):
    from app.services.producao import enviar_plano_do_dia, produzir_item_plano
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvo = _falta_total(plano)
        item = plano.itens[0]
        assert produzir_item_plano(item.id, alvo, admin_user.id)['ok']
        pb = PreBaixaMP.query.filter_by(plano_id=plano.id).one()
        assert pb.quantidade == 0          # linha fica (marcador de regime)
        assert mp.estoque_atual == ESTOQUE_INICIAL - alvo * G_POR_UN


def test_dispensar_estorna_e_reverter_reaplica(app, admin_user):
    from app.services.producao import enviar_plano_do_dia
    from app.services.producao_pendente import dispensar_item, reverter_dispensa
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvo = _falta_total(plano)
        item = plano.itens[0]
        assert dispensar_item(item.id, admin_user.id)['ok']
        pb = PreBaixaMP.query.filter_by(plano_id=plano.id).one()
        assert pb.quantidade == 0
        assert mp.estoque_atual == ESTOQUE_INICIAL     # tudo devolvido
        assert reverter_dispensa(item.id)['ok']
        db.session.refresh(pb)
        assert pb.quantidade == alvo * G_POR_UN        # reservou de novo
        assert mp.estoque_atual == ESTOQUE_INICIAL - alvo * G_POR_UN


def test_ordem_antiga_sem_linhas_fica_fora_do_regime(app, admin_user):
    """Plano enviado ANTES da feature (sem linhas PreBaixaMP): produzir faz
    só a baixa real de sempre — nada de pré-baixa retroativa da falta."""
    from app.services.producao import produzir_item_plano
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = PlanejamentoProducao(data=d2, origem='cronograma',
                                     status='aprovado', criado_por=admin_user.id,
                                     enviado_ao_padeiro=True)
        db.session.add(plano)
        db.session.flush()
        item = PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                multiplicador=5, qtd_alvo=50)
        db.session.add(item)
        db.session.commit()
        assert produzir_item_plano(item.id, 10, admin_user.id)['ok']
        assert PreBaixaMP.query.count() == 0
        assert _movs('Pré-baixa produção') == []
        # só a baixa real das 10 un
        assert mp.estoque_atual == ESTOQUE_INICIAL - 10 * G_POR_UN


def test_excluir_plano_devolve_reserva(app, admin_user):
    from app.services.producao import enviar_plano_do_dia, excluir_plano_do_dia
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert PreBaixaMP.query.filter_by(plano_id=plano.id).count() == 1
        res = excluir_plano_do_dia(d2)
        assert res['ok'], res
        assert PreBaixaMP.query.count() == 0
        assert mp.estoque_atual == ESTOQUE_INICIAL
        estornos = _movs('Estorno pré-baixa produção')
        assert len(estornos) == 1 and 'ordem excluída' in estornos[0].referencia


def test_aprovar_rascunho_nao_pre_baixa(app, admin_user):
    """Aprovar cria RASCUNHO — a reserva só nasce no gesto de ENVIAR."""
    from app.services.producao import aprovar_plano_do_dia
    with app.app_context():
        mp, r, d2 = _cenario()
        plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert plano is not None and plano.enviado_ao_padeiro is False
        assert PreBaixaMP.query.count() == 0
        assert mp.estoque_atual == ESTOQUE_INICIAL


def test_reenviar_com_alvo_maior_ajusta_o_delta(app, admin_user):
    """Editar o grid pra cima e re-enviar reserva só a DIFERENÇA (movimento
    único de delta, não re-baixa o total)."""
    from app.services.cronograma_edit import editar_celula
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        mp, r, d2 = _cenario(qtd=50)
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvo_1 = _falta_total(plano)
        res = editar_celula(r.id, d2.isoformat(), alvo_1 + 30, horizonte_dias=7)
        assert res.get('total') == alvo_1 + 30, res
        enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        pb = PreBaixaMP.query.filter_by(plano_id=plano.id).one()
        assert pb.quantidade == (alvo_1 + 30) * G_POR_UN
        assert mp.estoque_atual == ESTOQUE_INICIAL - (alvo_1 + 30) * G_POR_UN
        saidas = _movs('Pré-baixa produção')
        assert len(saidas) == 2                       # inicial + só o delta
        assert saidas[1].quantidade == 30 * G_POR_UN


def test_reagendar_move_reserva_para_hoje(app, admin_user):
    """Reagendar a falta de uma ordem vencida pra HOJE: a ordem antiga
    devolve a reserva e a de hoje reserva (criar=True — pode nascer aqui)."""
    from app.services.producao import sincronizar_pre_baixa_mp
    from app.services.producao_pendente import reagendar_para_hoje
    with app.app_context():
        # Ordem vencida de ONTEM já no regime (enviada com pré-baixa). Montada
        # à mão porque o cronograma agenda produção pra HOJE — e o reagendar
        # pula item que já é do plano de hoje.
        mp, r, _ = _cenario()
        ontem = hoje() - timedelta(days=1)
        plano = PlanejamentoProducao(data=ontem, origem='cronograma',
                                     status='aprovado', criado_por=admin_user.id,
                                     enviado_ao_padeiro=True)
        db.session.add(plano)
        db.session.flush()
        item = PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                multiplicador=4, qtd_alvo=40)
        db.session.add(item)
        sincronizar_pre_baixa_mp(plano, admin_user.id, criar=True)
        db.session.commit()
        alvo = 40
        assert mp.estoque_atual == ESTOQUE_INICIAL - alvo * G_POR_UN
        res = reagendar_para_hoje([item.id], admin_user.id)
        assert res['movidos'] == 1
        plano_hoje = (PlanejamentoProducao.query
                      .filter_by(data=hoje(), origem='cronograma').one())
        pb_hoje = PreBaixaMP.query.filter_by(plano_id=plano_hoje.id).one()
        assert pb_hoje.quantidade == alvo * G_POR_UN
        # ordem de origem: nada produzido → item saiu; reserva zerada
        for pb in PreBaixaMP.query.filter_by(plano_id=plano.id).all():
            assert pb.quantidade == 0
        # líquido global: a mesma falta continua reservada (1x, não 2x)
        assert mp.estoque_atual == ESTOQUE_INICIAL - alvo * G_POR_UN
