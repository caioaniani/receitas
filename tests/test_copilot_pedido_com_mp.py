"""Copilot: criar/editar pedido aceita MateriaPrima como item.

Bug que isso cobre: o `_resolver_produto` (e por extensao o executor de
criar_pedido/editar_pedido) ignorava MateriaPrima. Loja pede MP normalmente
(queijo pra salada, lagarto cozido, saco de pao de queijo), entao tem que
funcionar — a tela web ja aceitava, o copilot estava bloqueando.
"""
from datetime import date, timedelta


def _setup_basico(app):
    """Cria 1 receita, 1 produto, 1 MP, 1 loja, 1 admin (pra ter user)."""
    from app.extensions import db
    from app.models import Loja, MateriaPrima, Produto, Receita, Usuario
    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        receita = Receita(nome='Croissant', categoria='Croissants',
                          rendimento_qtd=1, rendimento_unidade='un',
                          peso_base=80.0)
        produto = Produto(nome='Cesta Especial', ativo=True)
        mp = MateriaPrima(nome='Mussarela de Búfala', unidade='kg',
                          custo_por_kg=50.0)
        admin = Usuario(login='adm', nome='Admin', papel='admin', loja_id=None)
        admin.set_senha('x')
        db.session.add_all([loja, receita, produto, mp, admin])
        db.session.commit()
        return {'loja_id': loja.id, 'receita_id': receita.id,
                'produto_id': produto.id, 'mp_id': mp.id, 'admin_id': admin.id}


def test_resolver_acha_materia_prima(app):
    """O resolver do PEDIDO acha MP (exato, fuzzy e via rapidfuzz)."""
    from app.services.copilot import _resolver_item_pedido
    ids = _setup_basico(app)
    with app.app_context():
        # exato (case-insensitive)
        ms = _resolver_item_pedido('mussarela de búfala')
        assert any(m['tipo'] == 'mp' and m['id'] == ids['mp_id']
                   and m['match'] == 'exato' for m in ms)
        # fuzzy via ilike
        ms2 = _resolver_item_pedido('mussarela')
        assert any(m['tipo'] == 'mp' and m['id'] == ids['mp_id'] for m in ms2)
        # tambem retorna Produto/Receita quando aplicavel (nao engole os outros)
        ms3 = _resolver_item_pedido('Croissant')
        assert any(m['tipo'] == 'receita' for m in ms3)


def test_resolver_produto_original_NAO_inclui_mp(app):
    """Regressao: B2B e ajuste_estoque seguem chamando _resolver_produto que
    NAO deve incluir MP — pra essas tools, MP ainda nao se aplica."""
    from app.services.copilot import _resolver_produto
    _setup_basico(app)
    with app.app_context():
        ms = _resolver_produto('Mussarela de Búfala')
        assert all(m['tipo'] != 'mp' for m in ms)


def test_executar_criar_pedido_com_mp(app):
    """Pedido criado pelo copilot com 1 MP grava PedidoItem com materia_prima_id."""
    from app.models import PedidoItem, Usuario
    from app.services.copilot import _enriquecer_criar_pedido, executar_criar_pedido
    ids = _setup_basico(app)
    with app.app_context():
        user = Usuario.query.get(ids['admin_id'])
        amanha = (date.today() + timedelta(days=1)).isoformat()
        # tool_input cru, como o LLM mandaria
        tool_in = {
            'loja_id': ids['loja_id'],
            'data_entrega': amanha,
            'itens': [
                {'nome': 'Mussarela de Búfala', 'quantidade': 2},
                {'nome': 'Croissant', 'quantidade': 5},
            ],
        }
        enr = _enriquecer_criar_pedido(tool_in)
        # ambos resolvidos
        resolvidos = [it['resolvido'] for it in enr['itens']]
        assert resolvidos[0] and resolvidos[0]['tipo'] == 'mp'
        assert resolvidos[1] and resolvidos[1]['tipo'] == 'receita'
        # executa
        res = executar_criar_pedido(enr, user)
        assert res['ok'], f"falhou: {res.get('erro')}"
        # PedidoItem foi criado com materia_prima_id setado pra MP, e receita_id pra croissant
        itens = PedidoItem.query.filter_by(pedido_id=res['pedido_id']).all()
        assert len(itens) == 2
        por_tipo = {('mp' if i.materia_prima_id else
                     'receita' if i.receita_id else 'produto'): i for i in itens}
        assert por_tipo['mp'].materia_prima_id == ids['mp_id']
        assert por_tipo['mp'].quantidade == 2
        assert por_tipo['receita'].receita_id == ids['receita_id']
        assert por_tipo['receita'].quantidade == 5


def test_executar_editar_pedido_adiciona_mp(app):
    """Editar pedido pra trocar/adicionar uma MP grava materia_prima_id."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja, Usuario
    from app.services.copilot import _enriquecer_editar_pedido, executar_editar_pedido
    ids = _setup_basico(app)
    with app.app_context():
        # cria pedido inicial com 1 receita
        ped = PedidoLoja(loja_id=ids['loja_id'],
                         data_entrega=date.today() + timedelta(days=1),
                         status='confirmado', criado_por=ids['admin_id'])
        db.session.add(ped)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=ped.id,
                                  receita_id=ids['receita_id'], quantidade=3))
        db.session.commit()
        ped_id = ped.id
        user = Usuario.query.get(ids['admin_id'])
        # edita: substitui a composicao por 1 receita + 1 MP
        tool_in = {
            'pedido_id': ped_id,
            'itens': [
                {'nome': 'Croissant', 'quantidade': 3},
                {'nome': 'Mussarela de Búfala', 'quantidade': 1},
            ],
        }
        enr = _enriquecer_editar_pedido(tool_in)
        res = executar_editar_pedido(enr, user)
        assert res['ok'], f"falhou: {res.get('erro')}"
        itens = PedidoItem.query.filter_by(pedido_id=ped_id).all()
        assert len(itens) == 2
        # tem 1 com materia_prima_id setado
        tem_mp = [i for i in itens if i.materia_prima_id == ids['mp_id']]
        assert len(tem_mp) == 1 and tem_mp[0].quantidade == 1
