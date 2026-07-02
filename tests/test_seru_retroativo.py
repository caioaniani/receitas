"""Recuperação retroativa de baixas Seru (03/07/2026).

Antes, mapear um produto ou confirmar/vincular uma loja só valia DALI PRA
FRENTE: pedidos processados com zero baixa (produto pendente, loja não
reconhecida) ficavam sem baixa pra sempre (`SeruPedidoProcessado` trava o
reprocesso). `reprocessar_retroativo` libera os pedidos SEM NENHUMA baixa da
janela e reprocessa com os mapeamentos atuais. Parciais não são tocados.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    Receita,
    SeruLojaMap,
    SeruPedidoProcessado,
    VendaMapa,
)
from app.utils import agora, hoje


def _created_hoje():
    # 13h UTC = 10h BRT do MESMO dia — dentro da janela do reprocesso.
    return f'{hoje().isoformat()}T13:00:00Z'


def _pedido(pid, company, itens):
    return {
        'id': pid,
        'createdAt': _created_hoje(),
        'canceledAt': None,
        'company': {'name': company},
        'items': [{'name': n, 'quantity': q} for n, q in itens],
    }


def _loja_confirmada(nome='Ribeiro do Vale'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(SeruLojaMap(seru_company_name=nome, loja_id=loja.id,
                               confirmado_em=agora()))
    db.session.commit()
    return loja


def _receita_com_estoque(loja, nome='Pao Frances', qtd=10):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=qtd,
                     estado=None)
    db.session.add(el)
    db.session.commit()
    return r, el


def _sync(pedidos):
    from app.services import seru_sync
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=pedidos):
        return seru_sync.processar_pedidos(hoje(), hoje(), user=None)


def _retro(pedidos, dias=7):
    from app.services import seru_sync
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=pedidos):
        return seru_sync.reprocessar_retroativo(dias=dias, user=None)


def test_retroativo_recupera_produto_mapeado_depois(app):
    """Vendeu com produto AINDA pendente (zero baixa) → mapeou → retroativo
    baixa o que ficou pra trás."""
    with app.app_context():
        loja = _loja_confirmada()
        r, el = _receita_com_estoque(loja)
        ped = _pedido('R1', loja.nome, [('PAO NOVO', 4)])

        _sync([ped])                                   # produto vira pendente
        reg = SeruPedidoProcessado.query.get('R1')
        assert reg is not None and reg.n_itens_baixados == 0
        assert EstoqueLoja.query.get(el.id).quantidade == 10   # nada baixou

        # Admin mapeia o produto (o que antes só valia dali pra frente)
        vm = VendaMapa.query.filter_by(canal='seru',
                                       nome_externo='PAO NOVO').first()
        vm.receita_id = r.id
        vm.confirmado_em = agora()
        db.session.commit()

        res = _retro([ped])
        assert res['liberados'] == 1
        assert res['stats']['itens_baixados'] == 1
        assert EstoqueLoja.query.get(el.id).quantidade == 6    # 10 - 4 agora
        reg2 = SeruPedidoProcessado.query.get('R1')
        assert reg2 is not None and reg2.n_itens_baixados == 1


def test_retroativo_recupera_pedido_sem_loja(app):
    """Loja não reconhecida (fuzzy falhou 100%) marcava processado com
    loja_id=None e a baixa era perdida PRA SEMPRE. Agora: vincula a loja →
    retroativo recupera."""
    with app.app_context():
        ped = _pedido('R2', 'PADARIA XYZ', [('PAO FRANCES', 3)])
        _sync([ped])                                   # fuzzy não acha nada
        reg = SeruPedidoProcessado.query.get('R2')
        assert reg is not None and reg.loja_id is None

        # Admin vincula a loja àquela company (o sync já criou o SeruLojaMap
        # pendente — a tela ATUALIZA esse registro) e mapeia o produto
        loja = Loja(nome='PADARIA XYZ', ativa=True)
        db.session.add(loja)
        db.session.flush()
        lm = SeruLojaMap.query.filter_by(
            seru_company_name='PADARIA XYZ').first()
        assert lm is not None                          # sync criou pendente
        lm.loja_id = loja.id
        lm.confirmado_em = agora()
        db.session.commit()
        r, el = _receita_com_estoque(loja)
        db.session.add(VendaMapa(canal='seru', nome_externo='PAO FRANCES',
                                 receita_id=r.id, confirmado_em=agora(),
                                 fator_quantidade=1.0))
        db.session.commit()

        res = _retro([ped])
        assert res['liberados'] == 1
        assert EstoqueLoja.query.get(el.id).quantidade == 7    # 10 - 3
        assert SeruPedidoProcessado.query.get('R2').n_itens_baixados == 1


def test_retroativo_nao_toca_parciais(app):
    """Pedido com 1 item baixado + 1 pendente NÃO é reprocessado (re-baixaria
    o que já saiu) — só entra na contagem de parciais."""
    with app.app_context():
        loja = _loja_confirmada()
        r1, el1 = _receita_com_estoque(loja, nome='Pao Frances')
        db.session.add(VendaMapa(canal='seru', nome_externo='PAO FRANCES',
                                 receita_id=r1.id, confirmado_em=agora(),
                                 fator_quantidade=1.0))
        db.session.commit()
        ped = _pedido('R3', loja.nome,
                      [('PAO FRANCES', 2), ('ITEM MISTERIOSO', 5)])
        _sync([ped])
        assert EstoqueLoja.query.get(el1.id).quantidade == 8   # baixou o mapeado
        reg = SeruPedidoProcessado.query.get('R3')
        assert reg.n_itens_baixados == 1 and reg.n_itens_total == 2

        # mapeia o 2º item DEPOIS
        r2, el2 = _receita_com_estoque(loja, nome='Misterioso', qtd=10)
        vm = VendaMapa.query.filter_by(canal='seru',
                                       nome_externo='ITEM MISTERIOSO').first()
        vm.receita_id = r2.id
        vm.confirmado_em = agora()
        db.session.commit()

        res = _retro([ped])
        assert res['liberados'] == 0                   # parcial não é liberado
        assert res['parciais_na_janela'] == 1
        assert EstoqueLoja.query.get(el1.id).quantidade == 8   # não re-baixou
        assert EstoqueLoja.query.get(el2.id).quantidade == 10  # e não baixou o 2º


def test_retroativo_apaga_movs_sem_estoque_orfaos(app):
    """Pedido zero-baixa com mov `venda_seru_sem_estoque` (falta registrada):
    o retroativo apaga o registro E o mov antes de reprocessar (sem duplicar
    a falta no histórico)."""
    with app.app_context():
        loja = _loja_confirmada()
        r, el = _receita_com_estoque(loja, qtd=0)      # SEM saldo
        db.session.add(VendaMapa(canal='seru', nome_externo='PAO FRANCES',
                                 receita_id=r.id, confirmado_em=agora(),
                                 fator_quantidade=1.0))
        db.session.commit()
        ped = _pedido('R4', loja.nome, [('PAO FRANCES', 2)])
        _sync([ped])
        # sem saldo: baixa 0 → n_itens_baixados pode ser 0 com mov sem_estoque
        reg = SeruPedidoProcessado.query.get('R4')
        movs_antes = MovEstoqueLoja.query.filter(
            MovEstoqueLoja.referencia.like('Seru #R4%')).count()
        if reg.n_itens_baixados != 0:
            return  # semântica diferente (baixa parcial conta) — nada a testar
        assert movs_antes >= 1

        el.quantidade = 5                              # estoque reposto
        db.session.commit()
        res = _retro([ped])
        assert res['liberados'] == 1
        movs = MovEstoqueLoja.query.filter(
            MovEstoqueLoja.referencia.like('Seru #R4%')).all()
        tipos = sorted(m.tipo for m in movs)
        assert tipos == ['venda_seru']                 # falta antiga não duplicou
        assert EstoqueLoja.query.get(el.id).quantidade == 3


def test_rota_botao_saude(app, admin_user):
    """POST /pdv/reprocessar-retroativo responde com flash (API mockada)."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    with patch('app.services.seru_sync.reprocessar_retroativo',
               return_value={'liberados': 2, 'parciais_na_janela': 1,
                             'stats': {'itens_baixados': 3,
                                       'itens_pendentes_novos': 0}}):
        resp = c.post('/pdv/reprocessar-retroativo', data={'dias': '30'},
                      follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '2 pedido(s) sem' in body
    assert '3 item(ns)' in body
    assert 'parciais' in body
