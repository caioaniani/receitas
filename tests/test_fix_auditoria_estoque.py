"""Testes de regressao pros bugs de estoque/dinheiro descobertos na
auditoria continuada (B1, B2, B3, B6, B7, B8, B10).

Cada teste verifica o cenario exato que escapava antes do fix.
"""
import pytest


# ──────────────────────────────────────────────────────────────────────
# B1 — Estorno Seru com fator != 1
# ──────────────────────────────────────────────────────────────────────

def test_b1_estorno_seru_com_fator(app, admin_user, loja, catalogo):
    """Estorno tem que pegar refs 'Seru #123 (fator 0.2)' tambem.

    Antes: filtro `==` exato so achava 'Seru #123', deixava 'Seru #123 (fator X)'
    sem reverter. Estoque baixado para sempre.
    """
    from app.extensions import db
    from app.models import (EstoqueLoja, MovEstoqueLoja, SeruPedidoProcessado)
    from app.services.seru_sync import _estornar_pedido

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=10)
    db.session.add(el)
    db.session.commit()

    # Simula baixa com fator (referencia tem sufixo)
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id,
        tipo='venda_seru',
        quantidade=2,
        referencia='Seru #999 (fator 0.2)',
        usuario_id=admin_user.id,
    ))
    el.quantidade = 8  # post-baixa
    reg = SeruPedidoProcessado(
        seru_pedido_id='999', loja_id=loja.id,
    )
    db.session.add(reg)
    db.session.commit()

    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()
    db.session.refresh(el)

    assert el.quantidade == 10, (
        f'estorno nao restaurou estoque: ficou {el.quantidade}, esperado 10'
    )
    assert reg.estornado_em is not None


def test_b1_estorno_seru_com_cesta(app, admin_user, loja, catalogo):
    """Estorno tem que pegar refs 'Seru #123 [Cesta → cesta] X'."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja, SeruPedidoProcessado
    from app.services.seru_sync import _estornar_pedido

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=20)
    db.session.add(el)
    db.session.commit()
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=5,
        referencia='Seru #888 [Family Box → cesta] Sourdough',
        usuario_id=admin_user.id,
    ))
    el.quantidade = 15
    reg = SeruPedidoProcessado(seru_pedido_id='888', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()

    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()
    db.session.refresh(el)
    assert el.quantidade == 20, f'cesta nao restaurada: {el.quantidade}'


# ──────────────────────────────────────────────────────────────────────
# B8 — estornado_em so se algo foi revertido
# ──────────────────────────────────────────────────────────────────────

def test_b8_estornado_em_nao_marca_se_nada_pra_reverter(app, admin_user, loja):
    """Pedido sem nenhuma mov real (so venda_seru_sem_estoque) nao deve
    marcar estornado_em — fica None pra distinguir 'estornado de fato'."""
    from app.extensions import db
    from app.models import SeruPedidoProcessado
    from app.services.seru_sync import _estornar_pedido

    reg = SeruPedidoProcessado(seru_pedido_id='777', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()

    # Nenhuma MovEstoqueLoja existe pra esse pedido
    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()

    assert reg.estornado_em is None, (
        f'estornado_em foi marcado sem nada pra estornar: {reg.estornado_em}'
    )


# ──────────────────────────────────────────────────────────────────────
# B2 — Cancelar B2B de cesta restaura componentes
# ──────────────────────────────────────────────────────────────────────

def test_b2_cancelar_b2b_de_cesta_restaura_componentes(app, admin_user, catalogo):
    """Venda B2B de cesta baixa componentes; cancelamento tem que restaurar
    esses componentes. Antes nao restaurava nada — buscava por produto_id
    da cesta no EstoqueProducao e nao achava."""
    from app.extensions import db
    from app.models import EstoqueProducao, Produto, ProdutoItem
    from app.services import vendas_b2b as svc

    # Cria cesta (Produto com ProdutoItem apontando pra Receita)
    cesta = Produto(nome='Box Sourdough', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add(ProdutoItem(
        produto_id=cesta.id, tipo='receita',
        item_nome=catalogo['receita'].nome, quantidade=2,
    ))
    # Estoque do componente (receita)
    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='Hotel',
        itens=[{'tipo': 'produto', 'id': cesta.id, 'quantidade': 3,
                'preco_unitario': 100.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 14, f'baixa de cesta: {ep.quantidade}'

    svc.cancelar_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 20, (
        f'cancel de cesta nao restaurou componentes: {ep.quantidade}'
    )


# ──────────────────────────────────────────────────────────────────────
# B3 — Cancelar B2B parcial nao cria estoque do nada
# ──────────────────────────────────────────────────────────────────────

def test_b3_cancelar_b2b_parcial_nao_infla(app, admin_user, catalogo):
    """Venda de 10 com so 5 em estoque: baixa 5 + registra 5 sem_estoque.
    Cancel tem que restaurar APENAS os 5 que foram baixados — antes
    somava 10, inflando estoque do nada."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=5)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='Hotel',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 10, 'preco_unitario': 8.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 0, f'baixa parcial deveria zerar: {ep.quantidade}'

    svc.cancelar_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 5, (
        f'cancel inflou estoque: ficou {ep.quantidade}, '
        'devia voltar a 5 (so o que foi efetivamente baixado)'
    )


# ──────────────────────────────────────────────────────────────────────
# B6 — Upload duplicado nao dobra VendaManualLoja
# ──────────────────────────────────────────────────────────────────────

def test_b6_upload_duplicado_nao_dobra(app, admin_user, loja, catalogo):
    """Aplicar o MESMO xlsx 2x nao cria 2 linhas em VendaManualLoja."""
    from datetime import date
    from app.extensions import db
    from app.models import VendaManualLoja
    from app.services import vendas_manuais as svc

    parseados = [{
        'linha_n': 1, 'data_venda': date(2026, 5, 1),
        'nome': catalogo['receita'].nome, 'quantidade': 12,
    }]
    res1 = svc.aplicar_vendas_xlsx(parseados, loja.id, admin_user)
    assert len(res1['aplicados']) == 1

    res2 = svc.aplicar_vendas_xlsx(parseados, loja.id, admin_user)
    # Segundo upload tem que detectar duplicata
    assert len(res2['aplicados']) == 0, 'duplicata foi aceita'
    assert any(i.get('motivo') == 'duplicata_ja_lancada'
               for i in res2['ignorados'])

    total_no_banco = VendaManualLoja.query.filter_by(
        loja_id=loja.id, receita_id=catalogo['receita'].id,
    ).count()
    assert total_no_banco == 1, (
        f'tinha que ter 1 linha unica, tem {total_no_banco}'
    )


# ──────────────────────────────────────────────────────────────────────
# B7 — Entrada de pendente repetido nao duplica linha
# ──────────────────────────────────────────────────────────────────────

def test_b7_entrada_pendente_reusa_linha(app, admin_user, loja):
    """Entrada em lote com mesmo nome pendente 2x tem que reusar a mesma
    linha de EstoqueLoja, nao criar duas."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import estoque_loja_lote as svc

    # Item nao existe no catalogo → vira pendente
    itens = [{'linha': 'x', 'nome': 'Produto Misterioso XYZ',
              'quantidade': 5, 'resolvido': None}]
    res1 = svc.aplicar_entrada_lote(itens, loja.id, admin_user)
    assert len(res1['aplicados']) == 1

    # Mesma entrada de novo — tem que SOMAR na linha existente, nao duplicar
    itens2 = [{'linha': 'x', 'nome': 'Produto Misterioso XYZ',
                'quantidade': 3, 'resolvido': None}]
    svc.aplicar_entrada_lote(itens2, loja.id, admin_user)

    linhas = EstoqueLoja.query.filter_by(
        loja_id=loja.id, nome_pendente='Produto Misterioso XYZ',
    ).all()
    assert len(linhas) == 1, f'fragmentou em {len(linhas)} linhas'
    assert linhas[0].quantidade == 8, (
        f'qtd nao somou: {linhas[0].quantidade} (esperado 5+3=8)'
    )


def test_b7_balanco_congelados_pendente_reusa_linha(app, admin_user):
    """Balanco repetido com mesmo nome pendente reusa linha (sobrescreve
    quantidade, como faz pra itens resolvidos)."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import estoque_congelados as svc

    itens = [{'linha': 'x', 'nome': 'Pao Fantasma 500g',
              'quantidade': 10, 'resolvido': None}]
    svc.aplicar_balanco(itens, admin_user)

    itens2 = [{'linha': 'x', 'nome': 'Pao Fantasma 500g',
                'quantidade': 25, 'resolvido': None}]
    svc.aplicar_balanco(itens2, admin_user)

    linhas = EstoqueProducao.query.filter_by(
        nome_pendente='Pao Fantasma 500g',
    ).all()
    assert len(linhas) == 1, f'fragmentou em {len(linhas)} linhas'
    # Balanco SOBRESCREVE: deve ficar 25, nao 10+25=35
    assert linhas[0].quantidade == 25, (
        f'balanco somou em vez de sobrescrever: {linhas[0].quantidade}'
    )


# ──────────────────────────────────────────────────────────────────────
# B10 — Sugestao de pedido subtrai estornos
# ──────────────────────────────────────────────────────────────────────

def test_b10_sugestao_subtrai_estornos(app, admin_user, loja, catalogo):
    """Se uma venda de 50 foi estornada, a media nao deve incluir os 50.

    Verifica que `venda_seru_estorno` entra na agregacao com sinal
    negativo (cancela a contribuicao da venda original).
    """
    from datetime import date, datetime, timedelta
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.vendas_manuais import sugerir_pedido

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=0)
    db.session.add(el)
    db.session.commit()

    base_dt = datetime(2026, 5, 1, 12, 0)
    # Venda de 50
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=50,
        referencia='Seru #1', data=base_dt,
    ))
    # Estorno de 50 — venda cancelada
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru_estorno', quantidade=50,
        referencia='Estorno Seru #1 (cancelada)',
        data=base_dt + timedelta(hours=1),
    ))
    db.session.commit()

    res = sugerir_pedido(
        loja.id,
        data_inicio=date(2026, 5, 1),
        data_fim=date(2026, 5, 7),
        data_pedido=date(2026, 5, 8),
    )

    # Total de vendas reais (50 - 50) = 0 → nao tem venda → sem sugestao
    chave = ('receita', catalogo['receita'].id)
    itens_dict = {(it['tipo'], it['item_id']): it
                  for it in res.get('itens', [])}
    if chave in itens_dict:
        it = itens_dict[chave]
        assert it['qtd_vendida'] == 0, (
            f'estorno nao subtraiu: qtd_vendida={it["qtd_vendida"]}'
        )
