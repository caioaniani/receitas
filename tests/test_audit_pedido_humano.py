"""Histórico de edição de pedido em linguagem humana (03/07/2026).

Antes: editar itens de um pedido gerava no /audit (1) uma linha do pedido
"(sem mudanças detectadas)" — só modificado_em/por mudavam — e (2) linhas de
pedido_item genéricas ("criou item do pedido #987", id do ITEM), que nem
apareciam no "histórico completo" do pedido (filtrado por pedido_loja).
Agora: modificado_por_id é suprimido do diff, os itens ganham frases humanas
("João adicionou 50x Croissant ao pedido #322"), a linha do pedido aponta
pros itens, e o histórico completo do pedido INCLUI as linhas de item.
"""
import json
from types import SimpleNamespace

from app.extensions import db
from app.models import AuditLog, Receita


def _log(tabela, acao, antes=None, depois=None, usuario=None, rid=None):
    return SimpleNamespace(tabela=tabela, acao=acao, registro_id=rid,
                           usuario=usuario, antes=antes, depois=depois)


def _user(nome='João'):
    return SimpleNamespace(nome=nome)


def test_item_adicionado_vira_frase_humana(app):
    from app.services.historico_humano import traduzir_audit
    with app.app_context():
        r = Receita(nome='Croissant Audit', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        depois = {'id': 987, 'pedido_id': 322, 'receita_id': r.id,
                  'produto_id': None, 'quantidade': 50, 'estado': None}
        t = traduzir_audit(_log('pedido_item', 'insert', usuario=_user()),
                           None, depois)
        assert t['frase'] == 'João adicionou 50x Croissant Audit ao pedido #322'


def test_item_removido_e_alterado(app):
    from app.services.historico_humano import traduzir_audit
    with app.app_context():
        r = Receita(nome='Baguete Audit', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        antes = {'id': 987, 'pedido_id': 322, 'receita_id': r.id,
                 'quantidade': 20, 'estado': None}
        t = traduzir_audit(_log('pedido_item', 'delete', usuario=_user()),
                           antes, None)
        assert t['frase'] == 'João removeu 20x Baguete Audit do pedido #322'

        depois = dict(antes, quantidade=35)
        t2 = traduzir_audit(_log('pedido_item', 'update', usuario=_user()),
                            antes, depois)
        assert 'alterou Baguete Audit — pedido #322' in t2['frase']
        assert 'quantidade: 20 → 35' in t2['frase']
        # FKs de vínculo não aparecem como "mudança".
        assert all(m['campo'] == 'quantidade' for m in t2['mudancas'])


def test_pedido_so_com_campos_tecnicos_aponta_pros_itens(app):
    """O caso da tela do dono: só modificado_em/por mudaram — a frase deixa
    claro que a mudança real está nos itens, e o 'modificado por: 1 → 14'
    some do diff (ruído técnico)."""
    from app.services.historico_humano import traduzir_audit
    antes = {'id': 322, 'modificado_em': '2026-07-03T12:54:16',
             'modificado_por_id': 1}
    depois = {'id': 322, 'modificado_em': '2026-07-03T14:56:20',
              'modificado_por_id': 14}
    t = traduzir_audit(_log('pedido_loja', 'update', usuario=_user(), rid=322),
                       antes, depois)
    assert t['mudancas'] == []
    assert 'mudanças nos ITENS' in t['frase']
    assert 'sem mudanças detectadas' not in t['frase']


def test_outras_tabelas_mantem_sem_mudancas(app):
    from app.services.historico_humano import traduzir_audit
    t = traduzir_audit(_log('fornecedor', 'update', usuario=_user(), rid=9),
                       {'id': 9, 'criado_em': 'x'}, {'id': 9, 'criado_em': 'y'})
    assert '(sem mudanças detectadas)' in t['frase']


def test_historico_completo_do_pedido_inclui_itens(app, admin_user):
    """/audit?tabela=pedido_loja&registro_id=N (link 'histórico completo')
    agora traz também as linhas de pedido_item daquele pedido."""
    with app.app_context():
        r = Receita(nome='Croissant HC', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        db.session.add_all([
            AuditLog(tabela='pedido_loja', acao='update', registro_id=322,
                     usuario_id=admin_user.id,
                     antes=json.dumps({'id': 322, 'modificado_por_id': 1}),
                     depois=json.dumps({'id': 322, 'modificado_por_id': 14})),
            AuditLog(tabela='pedido_item', acao='insert', registro_id=987,
                     usuario_id=admin_user.id, antes=None,
                     depois=json.dumps({'id': 987, 'pedido_id': 322,
                                        'receita_id': r.id,
                                        'quantidade': 50})),
            AuditLog(tabela='pedido_item', acao='insert', registro_id=988,
                     usuario_id=admin_user.id, antes=None,
                     depois=json.dumps({'id': 988, 'pedido_id': 999,
                                        'receita_id': r.id,
                                        'quantidade': 7})),
        ])
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(admin_user.id)
            sess['_fresh'] = True
        html = c.get('/audit?tabela=pedido_loja&registro_id=322') \
                .get_data(as_text=True)
        assert 'adicionou 50x Croissant HC ao pedido #322' in html
        assert '7x Croissant HC' not in html          # item de OUTRO pedido
        AuditLog.query.delete()
        db.session.commit()
