"""Ordem ENVIADA ao padeiro nunca muda por caminho implícito (04/07/2026).

Garantia do dono: depois do "enviar à produção", o que o padeiro vê só muda
pelo "🔄 atualizar produção" explícito daquele dia. "Limpar edições manuais"
já era seguro (apaga só CronogramaOverride); o furo era o "aprovar" — ele
reconstruía os itens a partir do grid SEM checar se o dia já tinha sido
enviado (aba desatualizada, POST repetido). Agora aprovar num dia enviado é
recusado com PlanoJaEnviadoError.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


def _cenario(qtd=50):
    r = Receita(nome='Pao Enviado', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    loja = Loja(nome='Loja Envio', ativa=True)
    db.session.add_all([r, loja])
    db.session.flush()
    d2 = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=d2,
                   data_pedido=d2)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=qtd))
    db.session.commit()
    return r, d2


def test_aprovar_em_dia_enviado_e_recusado(app, admin_user):
    from app.services.producao import (
        PlanoJaEnviadoError,
        aprovar_plano_do_dia,
        enviar_plano_do_dia,
    )
    with app.app_context():
        r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert plano.enviado_ao_padeiro is True
        alvos_antes = {it.receita_id: it.qtd_alvo for it in plano.itens}

        with pytest.raises(PlanoJaEnviadoError):
            aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)

        db.session.expire_all()
        assert plano.enviado_ao_padeiro is True
        assert {it.receita_id: it.qtd_alvo
                for it in plano.itens} == alvos_antes


def test_rota_aprovar_dia_enviado_avisa_e_nao_mexe(app, admin_user):
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario()
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvos_antes = {it.receita_id: it.qtd_alvo for it in plano.itens}
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(admin_user.id)
            sess['_fresh'] = True
        resp = c.post('/telaindustriateste/aprovar',
                      data={'data': d2.isoformat()}, follow_redirects=True)
        html = resp.get_data(as_text=True)
        assert 'já foi ENVIADO ao padeiro' in html
        db.session.expire_all()
        assert {it.receita_id: it.qtd_alvo
                for it in plano.itens} == alvos_antes


def test_limpar_edicoes_nao_toca_ordem_enviada(app, admin_user):
    """Trava de regressão do combinado: limpar overrides zera só o rascunho
    do grid; a ordem enviada mantém as quantidades editadas."""
    from app.services.cronograma_edit import editar_celula, limpar_todos_overrides
    from app.services.producao import enviar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario(qtd=50)
        # edita a célula do dia de produção (d2 - lead 0 = d2) pra 80
        res = editar_celula(r.id, d2.isoformat(), 80, horizonte_dias=7)
        assert res.get('total') == 80, res
        plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        alvo_enviado = {it.receita_id: it.qtd_alvo for it in plano.itens}
        assert alvo_enviado.get(r.id) == 80          # foi com a edição

        n, _preservados = limpar_todos_overrides()
        assert n >= 1
        db.session.expire_all()
        assert plano.enviado_ao_padeiro is True
        assert {it.receita_id: it.qtd_alvo
                for it in plano.itens} == alvo_enviado   # ordem intacta


def test_reaprovar_rascunho_continua_permitido(app, admin_user):
    """Aprovar de novo um dia ainda NÃO enviado segue funcionando (revisão
    de rascunho) — a recusa é só pra dia enviado."""
    from app.services.producao import aprovar_plano_do_dia
    with app.app_context():
        r, d2 = _cenario()
        p1 = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert p1.enviado_ao_padeiro is False
        p2 = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
        assert p2.id == p1.id and p2.enviado_ao_padeiro is False
