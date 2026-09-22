"""Proteção do pedido vale na transação que grava, inclusive limpeza por zero."""
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Receita
from app.services import auto_pedidos, pedido_lock, pedidos_semana, previsao_producao
from app.services.pedido_merge import OBSERVACAO_RASCUNHO_AUTO
from app.utils import hoje


def _rascunho(loja):
    r = Receita(nome='Choconana', peso_base=1000, rendimento_qtd=10,
                rendimento_unidade='un', sugerir_pedido_loja=True)
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='pendente', observacao=OBSERVACAO_RASCUNHO_AUTO)
    p.itens.append(PedidoItem(receita=r, quantidade=40))
    db.session.add_all([r, p])
    db.session.commit()
    return r, p


def _humano_na_outra_transacao(pid, uid, acao):
    # Outra conexão confirma o gesto enquanto o motor ainda possui o snapshot
    # antigo em memória, antes de conseguir a trava e reler o pedido.
    with Session(db.engine) as session:
        p = session.get(PedidoLoja, pid)
        p.modificado_por_id = uid
        p.status = 'cancelado' if acao == 'cancelar' else 'confirmado'
        p.itens[0].quantidade = 7
        session.commit()


@pytest.mark.parametrize('acao', ['editar', 'confirmar', 'cancelar'])
def test_grade_rele_apos_obter_trava_sem_sobrescrever_ou_ressuscitar(
        app, loja, admin_user, monkeypatch, congela_hoje, acao):
    congela_hoje()
    with app.app_context():
        r, p = _rascunho(loja)
        pid, uid, rid, lid, data = p.id, admin_user.id, r.id, loja.id, p.data_entrega
        assert p.itens[0].quantidade == 40
        chamou = []

        def obter_trava(ids):
            chamou.append(list(ids))
            if len(chamou) == 1:
                _humano_na_outra_transacao(pid, uid, acao)

        monkeypatch.setattr(pedido_lock, 'serializar_lojas', obter_trava)
        out = pedidos_semana.aplicar_grade([{
            'loja_id': lid, 'data_entrega': data,
            'itens': [{'receita_id': rid, 'qtd': 20}]}], user_id=None)
        db.session.expire_all()
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 7
        assert db.session.get(PedidoLoja, pid).modificado_por_id == uid
        assert PedidoLoja.query.count() == 1
        assert out['pulados_humano'] == 1
        assert out['atualizados'] == out['criados'] == 0
        assert chamou[0] == [lid]
        if acao == 'cancelar':
            assert db.session.get(PedidoLoja, pid).status == 'cancelado'


def test_limpeza_por_zero_preserva_pedido_adotado_antes_da_trava(
        app, loja, admin_user, monkeypatch, congela_hoje):
    congela_hoje()
    with app.app_context():
        r, p = _rascunho(loja)
        pid, uid, rid, lid, data = p.id, admin_user.id, r.id, loja.id, p.data_entrega
        chamado = []

        def obter_trava(ids):
            chamado.append(list(ids))
            if len(chamado) == 1:
                _humano_na_outra_transacao(pid, uid, 'editar')

        monkeypatch.setattr(pedido_lock, 'serializar_lojas', obter_trava)
        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda', lambda **kw: {
            'dias': [{'data': data.isoformat()}], 'lojas': [
                {'loja_id': lid, 'produtos': [{'receita_id': rid, 'por_dia': [0]}]}]})
        out = auto_pedidos.gerar_pedidos_automaticos()
        db.session.expire_all()
        p = db.session.get(PedidoLoja, pid)
        assert p.status == 'confirmado' and p.itens[0].quantidade == 7
        assert out['rascunhos_cancelados_zero'] == 0


def test_absorcao_e_grade_permanecem_na_mesma_transacao_ate_commit(
        app, loja, admin_user, monkeypatch, congela_hoje):
    congela_hoje()
    with app.app_context():
        _r, p = _rascunho(loja)
        data = p.data_entrega
        db.session.add(PedidoLoja(loja_id=loja.id, data_entrega=data,
                                  status='confirmado', criado_por=admin_user.id))
        db.session.commit()
        etapas = []
        monkeypatch.setattr(pedido_lock, 'serializar_lojas',
                            lambda ids: etapas.append(('lock', tuple(ids))))

        def prever(**kw):
            etapas.append(('previsao',))
            assert not any(e[0] == 'commit' for e in etapas)
            assert db.session.get(PedidoLoja, p.id).status == 'cancelado'
            return {'dias': [{'data': data.isoformat()}], 'lojas': []}

        def commit(session):
            etapas.append(('commit',))

        monkeypatch.setattr(previsao_producao, 'sugerir_pedidos_por_venda', prever)
        event.listen(db.session(), 'after_commit', commit)
        try:
            out = auto_pedidos.gerar_pedidos_automaticos()
        finally:
            event.remove(db.session(), 'after_commit', commit)
        assert out['rascunhos_absorvidos'] == 1
        assert etapas[0][0] == 'lock'
        assert etapas[-1] == ('commit',)
        assert sum(e[0] == 'commit' for e in etapas) == 1
