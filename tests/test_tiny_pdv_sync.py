"""Importacao das vendas do PDV do TINY (27/07/2026, caso Cantina).

A Cantina vende pelo Tiny, nao pelo Seru — as vendas dela eram invisiveis
(nao baixavam EstoqueLoja, nao entravam em faturamento/previsao). Este
service espelha o `seru_sync`.

Contrato que torna seguro importar por `pedidos.pesquisa.php`: o NOSSO
sistema so cria NOTA no Tiny, nunca pedido (`tiny.incluir_pedido` sem
chamador) — logo pedido = venda de PDV, sem colidir com a baixa do site/B2B.

A API do Tiny e SEMPRE mockada (padrao da casa).
"""
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    Receita,
    TinyPedidoProcessado,
    VendaMapa,
)
from app.services import tiny_pdv_sync

_DIA = date(2026, 7, 25)


def _cantina():
    lj = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
    db.session.add(lj)
    db.session.commit()
    AppConfig.set(tiny_pdv_sync._CFG_LOJA, lj.id)
    db.session.commit()
    return lj


def _receita(nome='Croissant Francês'):
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _estoque(loja, receita, qtd):
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _pedido(pid='909754567', numero='99263', situacao='Faturado', valor=73):
    return {'id': pid, 'numero': numero, 'situacao': situacao,
            'valor': valor, 'data_pedido': '25/07/2026',
            'nome': 'Consumidor Final'}


def _itens(nome='CROISSANT FRANCÊS CANTINA', qtd=2.0, pid='635556424'):
    return [{'tiny_produto_id': pid, 'codigo': '12', 'nome': nome,
             'quantidade': qtd, 'valor_unitario': '18.00'}]


@pytest.fixture
def tiny_mock(monkeypatch):
    """Mocka o cliente do Tiny. `estado` controla o que a API devolve."""
    estado = {'pedidos': [], 'itens': {}, 'disponivel': True}

    monkeypatch.setattr(tiny_pdv_sync, 'CANAL', 'tiny')
    import app.services.tiny as tiny_mod
    monkeypatch.setattr(tiny_mod, 'disponivel', lambda: estado['disponivel'])
    monkeypatch.setattr(tiny_mod, 'listar_pedidos_periodo',
                        lambda di, df, **kw: estado['pedidos'])
    monkeypatch.setattr(tiny_mod, 'itens_do_pedido',
                        lambda pid: estado['itens'].get(str(pid)))
    return estado


# ── guarda de configuracao ──────────────────────────────────────────

def test_sem_loja_configurada_nao_baixa_nada(app, tiny_mock):
    """Baixar na loja errada e pior que nao baixar: sem AppConfig o sync
    recusa e diz por que."""
    with app.app_context():
        r = _receita()
        lj = Loja(nome='Cantina', ativa=True)
        db.session.add(lj)
        db.session.commit()
        _estoque(lj, r, 10)
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens()}
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['erro'] and 'tiny_pdv_loja_id' in st['erro']
        assert MovEstoqueLoja.query.count() == 0


# ── baixa de estoque ────────────────────────────────────────────────

def test_venda_do_pdv_baixa_estoque_da_cantina(app, tiny_mock):
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=2.0)}

        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['erro'] is None
        assert st['baixados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 8
        mov = MovEstoqueLoja.query.filter_by(tipo='venda_tiny').first()
        assert mov is not None
        assert 'Tiny #909754567' in mov.referencia


def test_idempotente_nao_baixa_duas_vezes(app, tiny_mock):
    """O cron roda a cada N minutos sobre a MESMA janela."""
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=2.0)}

        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        st2 = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st2['ja_processados'] == 1
        assert st2['baixados'] == 0
        db.session.refresh(el)
        assert el.quantidade == 8          # continua 8, nao 6


def test_fator_do_mapa_multiplica(app, tiny_mock):
    """'CONE DE PÃO DE QUEIJO COM 5 UN' = 5 unidades por venda."""
    with app.app_context():
        lj = _cantina()
        r = _receita('Pão de Queijo')
        el = _estoque(lj, r, 50)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CONE DE PÃO DE QUEIJO COM 5 UN  CANTINA',
                                 receita_id=r.id, fator_quantidade=5.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(
            nome='CONE DE PÃO DE QUEIJO COM 5 UN  CANTINA', qtd=2.0)}

        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        db.session.refresh(el)
        assert el.quantidade == 40         # 2 cones x 5 un


def test_sem_estoque_registra_e_nao_fica_negativo(app, tiny_mock):
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 1)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=3.0)}

        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        db.session.refresh(el)
        assert el.quantidade >= 0
        assert MovEstoqueLoja.query.filter_by(
            tipo='venda_tiny_sem_estoque').count() >= 1


# ── produto novo / pendente ─────────────────────────────────────────

def test_produto_sem_mapa_vira_pendente_e_nao_trava_a_venda(app, tiny_mock):
    """O 'CAFÉ EXPRESSO' apareceu no fim de semana sem sufixo CANTINA e sem
    cadastro: tem que virar pendente, nao explodir."""
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': (
            _itens(qtd=1.0) + [{'tiny_produto_id': '703178642',
                                'codigo': '9', 'nome': 'CAFÉ EXPRESSO',
                                'quantidade': 1.0, 'valor_unitario': '8.00'}])}

        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['baixados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 9          # o croissant baixou
        pend = [m.nome_externo for m in tiny_pdv_sync.pendentes_de_mapeamento()]
        assert 'CAFÉ EXPRESSO' in pend
        assert st['mapas_pendentes'] == 1


def test_mapa_novo_guarda_o_id_do_tiny(app, tiny_mock):
    """O nome muda quando o dono renomeia no Tiny; o id_produto nao."""
    with app.app_context():
        _cantina()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=1.0)}
        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        mapa = VendaMapa.query.filter_by(
            canal='tiny', nome_externo='CROISSANT FRANCÊS CANTINA').first()
        assert mapa is not None
        assert mapa.sku == '635556424'


# ── falhas e cancelamento ───────────────────────────────────────────

def test_detalhe_indisponivel_nao_marca_processado(app, tiny_mock):
    """Falha de rede nao pode fazer a venda sumir pra sempre."""
    with app.app_context():
        _cantina()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {}            # itens_do_pedido -> None
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['pendentes_detalhe'] == 1
        assert TinyPedidoProcessado.query.count() == 0


def test_pedido_cancelado_nunca_baixa(app, tiny_mock):
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido(situacao='Cancelado')]
        tiny_mock['itens'] = {'909754567': _itens(qtd=2.0)}
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['ignorados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 10


def test_cancelado_depois_da_baixa_estorna(app, tiny_mock):
    """Venda faturada e depois cancelada no Tiny: o ciclo seguinte devolve."""
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=2.0)}
        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        db.session.refresh(el)
        assert el.quantidade == 8

        tiny_mock['pedidos'] = [_pedido(situacao='Cancelado')]
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['estornados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 10         # devolvido
        reg = db.session.get(TinyPedidoProcessado, '909754567')
        assert reg.estornado_em is not None


def test_situacao_desconhecida_nao_baixa_nem_marca(app, tiny_mock):
    """Orcamento/aberto: quando virar venda o proximo ciclo pega."""
    with app.app_context():
        _cantina()
        tiny_mock['pedidos'] = [_pedido(situacao='Em aberto')]
        tiny_mock['itens'] = {'909754567': _itens()}
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['ignorados'] == 1
        assert TinyPedidoProcessado.query.count() == 0


# ── a venda entra na DEMANDA (previsao) ─────────────────────────────

def test_tipos_tiny_contam_como_demanda(app):
    """Sem isso a venda da Cantina baixaria estoque mas nao alimentaria a
    previsao de producao — o erro que a integracao existe pra corrigir."""
    from app.constants import (
        VENDA_TIPOS_DEMANDA_COM_ESTORNO,
        VENDA_TIPOS_DEMANDA_LOJA,
        VENDA_TIPOS_LOJA,
    )
    assert 'venda_tiny' in VENDA_TIPOS_DEMANDA_LOJA
    assert 'venda_tiny_sem_estoque' in VENDA_TIPOS_DEMANDA_LOJA
    assert 'venda_tiny' in VENDA_TIPOS_LOJA
    assert 'venda_tiny_estorno' in VENDA_TIPOS_DEMANDA_COM_ESTORNO


# ── sugestao automatica de mapeamento (77 produtos) ─────────────────

def test_fator_do_nome_le_o_multiplicador():
    """'CONE DE PÃO DE QUEIJO COM 5 UN' = 5 unidades por venda."""
    f = tiny_pdv_sync.fator_do_nome
    assert f('CONE DE PÃO DE QUEIJO COM 5 UN  CANTINA') == 5.0
    assert f('CONE DE PÃO DE QUEIJO COM 10 UN CANTINA') == 10.0
    assert f('CROISSANT FRANCÊS CANTINA') == 1.0
    # gramagem/volume NAO e quantidade
    assert f('SUCO DE LARANJA NATURAL 300 ml  CANTINA') == 1.0
    assert f('BRIOCHE 500 g CANTINA') == 1.0


def test_sugestao_acerta_o_obvio(app):
    with app.app_context():
        r = _receita('Sourdough 7 Grãos')
        cat = [('receita', r.id, r.nome)]
        sug = tiny_pdv_sync.sugerir_alvo('SOURDOUGH 7 GRÃOS CANTINA', cat)
        assert sug is not None
        kind, iid, nome, score = sug
        assert (kind, iid) == ('receita', r.id)
        assert score >= tiny_pdv_sync.PISO_PREENCHE   # confianca alta


def test_sugestao_fraca_NAO_preenche(app):
    """Guarda do achado de 27/07: 'CROISSANT DE AMÊNDOAS' casava 'Creme de
    Amêndoas' com 0.50. Sugestao dessa faixa aparece como dica, mas NAO pode
    vir pre-preenchida — o dono clicaria Salvar num vinculo errado."""
    with app.app_context():
        r = _receita('Creme de Amêndoas')
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT DE AMÊNDOAS CANTINA'))
        db.session.commit()
        sugs = tiny_pdv_sync.sugestoes_pendentes()
        alvo = [s for s in sugs.values() if s['id'] == r.id]
        if alvo:                       # so vale se o matcher sugeriu mesmo
            assert alvo[0]['score'] < tiny_pdv_sync.PISO_PREENCHE
            assert alvo[0]['preenche'] is False


def test_sugestao_nao_inventa_para_bebida(app):
    """Café/suco não têm receita equivalente — sugerir qualquer coisa aqui
    seria pior que não sugerir."""
    with app.app_context():
        _receita('Croissant Tradicional')
        _receita('Sourdough Tradicional')
        cat = [('receita', 1, 'Croissant Tradicional'),
               ('receita', 2, 'Sourdough Tradicional')]
        assert tiny_pdv_sync.sugerir_alvo('CAFÉ EXPRESSO', cat) is None
        assert tiny_pdv_sync.sugerir_alvo('CAPPUCCINO', cat) is None
        assert tiny_pdv_sync.sugerir_alvo('TODDY QUENTE MÉDIO', cat) is None


def test_sugestao_nao_toca_no_banco(app):
    """`sugestoes_pendentes` so SUGERE — nao pode gravar vinculo nenhum."""
    with app.app_context():
        _receita('Sourdough 7 Grãos')
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='SOURDOUGH 7 GRÃOS CANTINA'))
        db.session.commit()
        tiny_pdv_sync.sugestoes_pendentes()
        m = VendaMapa.query.filter_by(canal='tiny').first()
        assert m.receita_id is None and m.produto_id is None


# ── aceite em lote das sugestoes de 100% ────────────────────────────

def test_aceitar_lote_aplica_so_os_100(app):
    """'SOURDOUGH 7 GRÃOS CANTINA' (100%) entra; 'SOURDOUGH DE GRÃOS SÓ
    ESQUENTADO' (92%) fica pra revisao manual."""
    with app.app_context():
        r = _receita('Sourdough 7 Grãos')
        db.session.add_all([
            VendaMapa(canal='tiny', nome_externo='SOURDOUGH 7 GRÃOS CANTINA'),
            VendaMapa(canal='tiny',
                      nome_externo='SOURDOUGH DE GRÃOS SÓ ESQUENTADO CANTINA'),
        ])
        db.session.commit()
        aplicados = tiny_pdv_sync.aceitar_sugestoes_lote(user_id=1)
        assert len(aplicados) == 1
        assert aplicados[0][0] == 'SOURDOUGH 7 GRÃOS CANTINA'
        m1 = VendaMapa.query.filter_by(
            canal='tiny', nome_externo='SOURDOUGH 7 GRÃOS CANTINA').first()
        assert m1.receita_id == r.id
        assert m1.confirmado_em is not None
        m2 = VendaMapa.query.filter_by(
            canal='tiny',
            nome_externo='SOURDOUGH DE GRÃOS SÓ ESQUENTADO CANTINA').first()
        assert m2.receita_id is None       # 92% NAO entra no lote


def test_aceitar_lote_usa_fator_do_nome(app):
    """Match perfeito com 'COM 5 UN' no nome grava fator 5."""
    with app.app_context():
        r = _receita('Cone')
        db.session.add(VendaMapa(canal='tiny', nome_externo='CONE COM 5 UN'))
        db.session.commit()
        aplicados = tiny_pdv_sync.aceitar_sugestoes_lote()
        assert len(aplicados) == 1
        m = VendaMapa.query.filter_by(canal='tiny',
                                      nome_externo='CONE COM 5 UN').first()
        assert m.receita_id == r.id
        assert m.fator_quantidade == 5.0


def test_aceitar_lote_nao_sobrescreve_mapeado(app):
    """So pendentes entram — vinculo existente do dono nunca e tocado."""
    with app.app_context():
        r1 = _receita('Sourdough 7 Grãos')
        r2 = _receita('Outra Receita')
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='SOURDOUGH 7 GRÃOS CANTINA',
                                 receita_id=r2.id))
        db.session.commit()
        aplicados = tiny_pdv_sync.aceitar_sugestoes_lote()
        assert aplicados == []
        m = VendaMapa.query.filter_by(
            canal='tiny', nome_externo='SOURDOUGH 7 GRÃOS CANTINA').first()
        assert m.receita_id == r2.id       # ficou como o dono deixou
        assert r1.id != r2.id


# ── re-baixa: importou ANTES de mapear (o caso real da Cantina) ─────

def test_importar_mapear_reimportar_recupera_a_baixa(app, tiny_mock):
    """O fluxo que aconteceu de verdade: 1º import sem NENHUM mapa (pedido
    marcado com 0 baixas) → dono mapeia → 2º import RE-baixa. Sem isso as
    vendas do 1º fim de semana nunca chegariam ao estoque."""
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=2.0)}

        st1 = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st1['baixados'] == 1        # processado, mas item pulado
        db.session.refresh(el)
        assert el.quantidade == 10         # nada baixou (sem mapa)

        # dono mapeia (aqui: em lote, tanto faz o caminho)
        m = VendaMapa.query.filter_by(
            canal='tiny', nome_externo='CROISSANT FRANCÊS CANTINA').first()
        m.receita_id = r.id
        db.session.commit()

        st2 = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st2['rebaixados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 8          # a venda perdida entrou
        reg = db.session.get(TinyPedidoProcessado, '909754567')
        assert reg.n_itens_baixados > 0

        # 3º import: nada muda (idempotente de novo)
        st3 = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st3['ja_processados'] == 1
        db.session.refresh(el)
        assert el.quantidade == 8


def test_pedido_todo_ignorado_nao_fica_em_loop(app, tiny_mock):
    """Item marcado 'ignorar' de proposito (cafe): o pedido com 0 baixas NAO
    re-busca detalhe a cada ciclo pra sempre."""
    with app.app_context():
        lj = _cantina()
        tiny_mock['pedidos'] = [_pedido()]
        tiny_mock['itens'] = {'909754567': _itens(qtd=1.0)}
        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        m = VendaMapa.query.filter_by(
            canal='tiny', nome_externo='CROISSANT FRANCÊS CANTINA').first()
        m.ignorar = True
        db.session.commit()
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['ja_processados'] == 1
        assert st['rebaixados'] == 0


def test_pedido_parcial_nunca_rebaixa(app, tiny_mock):
    """Pedido que JA baixou algum item nao re-processa (nao ha idempotencia
    por item — re-baixar duplicaria o que ja saiu)."""
    with app.app_context():
        lj = _cantina()
        r = _receita()
        el = _estoque(lj, r, 10)
        db.session.add(VendaMapa(canal='tiny',
                                 nome_externo='CROISSANT FRANCÊS CANTINA',
                                 receita_id=r.id, fator_quantidade=1.0))
        db.session.commit()
        tiny_mock['pedidos'] = [_pedido()]
        # croissant mapeado + cafe sem mapa -> baixa parcial
        tiny_mock['itens'] = {'909754567': (
            _itens(qtd=2.0) + [{'tiny_produto_id': '703178642', 'codigo': '9',
                                'nome': 'CAFÉ EXPRESSO', 'quantidade': 1.0,
                                'valor_unitario': '8.00'}])}
        tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        db.session.refresh(el)
        assert el.quantidade == 8

        # mapeia o cafe depois — mesmo assim NAO re-processa (parcial)
        cafe = VendaMapa.query.filter_by(canal='tiny',
                                         nome_externo='CAFÉ EXPRESSO').first()
        cafe.receita_id = r.id
        db.session.commit()
        st = tiny_pdv_sync.processar_periodo(_DIA, _DIA)
        assert st['ja_processados'] == 1
        assert st['rebaixados'] == 0
        db.session.refresh(el)
        assert el.quantidade == 8          # croissant NAO baixou de novo
