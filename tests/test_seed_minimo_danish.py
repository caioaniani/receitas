"""Seed do colchão de danishes ASSADAS (dono 17/08/2026): estoque_minimo=2
nas 5 danishes em cada loja ativa que abre todo dia — o motor venda+estoque
repõe o piso diariamente e pede a mais quando a média de venda passa dele.

O seed roda UMA vez (marker em AppConfig), nunca sobrescreve mínimo já
definido pelo dono e ignora Industria/loja de funcionamento restrito."""
from app.extensions import db
from app.migrations_legacy import SEED_MINIMO_DANISH, _seed_minimo_danish
from app.models import AppConfig, EstoqueLoja, Loja, Receita

NOMES = ['Danish de Calabresa', 'Danish de queijo branco',
         'Danish de Muçarela de Búfala', 'Danish de alho poró',
         'Danish de Maçã']


def _cenario():
    receitas = []
    for nome in NOMES:
        r = Receita(nome=nome, categoria='Danishes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0,
                    estado_padrao='assado')
        db.session.add(r)
        receitas.append(r)
    lojas = {
        'anesio': Loja(nome='Anesio', ativa=True),
        'ribeiro': Loja(nome='Ribeiro do Vale', ativa=True),
        'cantina': Loja(nome='Cantina', ativa=True, dias_funcionamento='56'),
        'industria': Loja(nome='Industria', ativa=True),
        'fechada': Loja(nome='Loja Fechada', ativa=False),
    }
    db.session.add_all(lojas.values())
    db.session.commit()
    return receitas, lojas


def _minimos(loja):
    return {el.receita_id: int(el.estoque_minimo or 0)
            for el in EstoqueLoja.query.filter_by(loja_id=loja.id)}


def test_seed_poe_minimo_2_nas_lojas_diarias(app):
    with app.app_context():
        receitas, lojas = _cenario()
        _seed_minimo_danish(app)
        for chave in ('anesio', 'ribeiro'):
            mins = _minimos(lojas[chave])
            assert len(mins) == 5
            assert all(v == 2 for v in mins.values())
        assert AppConfig.get(SEED_MINIMO_DANISH['chave'])


def test_seed_ignora_industria_cantina_e_inativa(app):
    """Industria (Loja só de RH), loja de funcionamento restrito (Cantina,
    sáb/dom — colchão diário não se aplica) e loja inativa ficam fora."""
    with app.app_context():
        _, lojas = _cenario()
        _seed_minimo_danish(app)
        for chave in ('cantina', 'industria', 'fechada'):
            assert _minimos(lojas[chave]) == {}


def test_seed_nao_sobrescreve_minimo_do_dono(app):
    """Mínimo já definido (> 0) é do dono — o seed mantém."""
    with app.app_context():
        receitas, lojas = _cenario()
        db.session.add(EstoqueLoja(loja_id=lojas['anesio'].id,
                                   receita_id=receitas[0].id,
                                   quantidade=3, estoque_minimo=5))
        db.session.commit()
        _seed_minimo_danish(app)
        mins = _minimos(lojas['anesio'])
        assert mins[receitas[0].id] == 5           # valor do dono mantido
        assert sum(1 for v in mins.values() if v == 2) == 4


def test_seed_reusa_linha_existente_sem_duplicar(app):
    with app.app_context():
        receitas, lojas = _cenario()
        db.session.add(EstoqueLoja(loja_id=lojas['anesio'].id,
                                   receita_id=receitas[1].id, quantidade=7))
        db.session.commit()
        _seed_minimo_danish(app)
        linhas = EstoqueLoja.query.filter_by(
            loja_id=lojas['anesio'].id, receita_id=receitas[1].id).all()
        assert len(linhas) == 1                    # reusa, não duplica
        assert int(linhas[0].quantidade) == 7      # saldo físico intocado
        assert int(linhas[0].estoque_minimo) == 2


def test_seed_roda_uma_vez(app):
    """Marker: segunda execução é no-op — apagar o mínimo depois é decisão
    do dono e o seed não ressuscita."""
    with app.app_context():
        receitas, lojas = _cenario()
        _seed_minimo_danish(app)
        el = EstoqueLoja.query.filter_by(loja_id=lojas['anesio'].id,
                                         receita_id=receitas[0].id).one()
        el.estoque_minimo = None                   # dono tirou o piso
        db.session.commit()
        _seed_minimo_danish(app)
        db.session.refresh(el)
        assert el.estoque_minimo is None           # seed não ressuscitou


def test_receita_arquivada_fica_fora(app):
    with app.app_context():
        receitas, lojas = _cenario()
        from app.utils import agora
        receitas[2].arquivada_em = agora()
        db.session.commit()
        _seed_minimo_danish(app)
        mins = _minimos(lojas['anesio'])
        assert receitas[2].id not in mins
        assert len(mins) == 4
