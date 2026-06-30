"""Tela de mapeamentos da saida em lote mostra as LOJAS que ja usaram cada
mapeamento (via LojaDebito, criado pra toda saida aplicada)."""


def test_mapeamento_mostra_lojas_que_usaram(app, admin_user):
    from app.extensions import db
    from app.models import Loja, LojaDebito, LojaProdutoMap, Receita
    with app.app_context():
        r = Receita(nome='Brioche', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        l1 = Loja(nome='Loja Anesio', ativa=True)
        l2 = Loja(nome='Loja Nebraska', ativa=True)
        l3 = Loja(nome='Loja Sem Uso', ativa=True)
        db.session.add_all([r, l1, l2, l3])
        db.session.commit()
        m = LojaProdutoMap(nome_digitado='BRIOCHE 500G', receita_id=r.id,
                           confirmado_por=admin_user.id)
        db.session.add(m)
        db.session.commit()
        # l1 e l2 usaram o mapeamento; l3 nao
        db.session.add_all([
            LojaDebito(loja_id=l1.id, loja_produto_map_id=m.id, fracao_pendente=0.0),
            LojaDebito(loja_id=l2.id, loja_produto_map_id=m.id, fracao_pendente=0.0),
        ])
        db.session.commit()

        c = app.test_client()
        c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
               follow_redirects=True)
        h = c.get('/pedidos/estoque-loja/mapeamentos').get_data(as_text=True)
        assert h.count('BRIOCHE 500G') >= 1
        assert 'Loja Anesio' in h
        assert 'Loja Nebraska' in h
        assert 'Loja Sem Uso' not in h          # nunca usou -> nao aparece
