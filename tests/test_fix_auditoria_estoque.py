"""Testes de regressao pros bugs de estoque/dinheiro descobertos na
auditoria continuada (B1, B2, B3, B4, B6, B7, B8, B9, B10).

Cada teste verifica o cenario exato que escapava antes do fix.
"""


# ──────────────────────────────────────────────────────────────────────
# B1 — Estorno Seru com fator != 1
# ──────────────────────────────────────────────────────────────────────

def test_b1_estorno_seru_com_fator(app, admin_user, loja, catalogo):
    """Estorno tem que pegar refs 'Seru #123 (fator 0.2)' tambem.

    Antes: filtro `==` exato so achava 'Seru #123', deixava 'Seru #123 (fator X)'
    sem reverter. Estoque baixado para sempre.
    """
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja, SeruPedidoProcessado
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
# B4 — Parcela quita com 3 pagamentos de R$ 33,33
# ──────────────────────────────────────────────────────────────────────

def test_b4_tres_parcelas_de_33_33_quitam_100_reais(app, admin_user, catalogo):
    """R$ 100 em 3 parcelas: 33.33 + 33.33 + 33.34. Cliente paga o valor
    EXATO de cada parcela. Todas tem que quitar.

    Antes do Numeric: a comparacao `>= 100` falhava por imprecisao de
    float na SOMA dos pagamentos. Agora com Decimal eh exato — nao
    precisa tolerancia.
    """
    from datetime import date

    from app.extensions import db
    from app.models import EstoqueProducao, VendaB2BParcela
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='Hotel',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 1, 'preco_unitario': 100.0}],
        parcelas=[
            {'vencimento': date(2026, 6, 1), 'valor': 33.33,
             'forma_pagamento': 'pix'},
            {'vencimento': date(2026, 7, 1), 'valor': 33.33,
             'forma_pagamento': 'pix'},
            {'vencimento': date(2026, 8, 1), 'valor': 33.34,
             'forma_pagamento': 'pix'},
        ],
        user=admin_user,
    )
    assert len(venda.parcelas) == 3

    # Cliente paga o valor EXATO de cada parcela (33.33, 33.33, 33.34)
    for p in sorted(venda.parcelas, key=lambda x: x.numero):
        svc.receber_pagamento(p, float(p.valor), forma_pagamento='pix')

    db.session.expire_all()
    parcelas = VendaB2BParcela.query.filter_by(venda_id=venda.id).all()
    pagas = [p for p in parcelas if p.pago_em is not None]
    assert len(pagas) == 3, (
        f'so {len(pagas)} de 3 parcelas quitaram'
    )


def test_b4_pagamento_curto_nao_quita_sem_querer(app, admin_user, catalogo):
    """Tolerancia eh de meio centavo. Pagamento R$ 0,02 abaixo NAO quita."""
    from datetime import date

    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='Hotel',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 1, 'preco_unitario': 100.0}],
        parcelas=[{'vencimento': date(2026, 6, 1), 'valor': 100.0}],
        user=admin_user,
    )
    parcela = venda.parcelas[0]
    svc.receber_pagamento(parcela, 99.98)  # falta 2 centavos
    db.session.refresh(parcela)
    assert parcela.pago_em is None, (
        'parcela quitou com falta de 2 centavos — tolerancia muito permissiva'
    )


# ──────────────────────────────────────────────────────────────────────
# B6 — Upload duplicado nao dobra VendaManualLoja
# ──────────────────────────────────────────────────────────────────────

def test_b6_upload_duplicado_nao_dobra(app, admin_user, loja, catalogo):
    """Aplicar o MESMO xlsx 2x nao cria 2 linhas em VendaManualLoja."""
    from datetime import date

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
    )

    # Vendas reais (50 - 50) = 0. A chave pode aparecer com qtd 0 ou nao
    # aparecer; o que NAO pode acontecer eh aparecer com qtd > 0.
    chave = ('receita', catalogo['receita'].id)
    for it in res.get('itens', []):
        if (it['tipo'], it['id']) == chave:
            assert it['vendas_periodo'] == 0, (
                f'estorno nao subtraiu: vendas_periodo={it["vendas_periodo"]}'
            )
            break


# ──────────────────────────────────────────────────────────────────────
# B9 — Estorno de venda fracionada reverte contribuicao no acumulador
# ──────────────────────────────────────────────────────────────────────

def test_b9_estorno_fracao_sem_inteiro_baixado(app, admin_user, loja, catalogo):
    """Pedido contribuiu 0.2 (fator), acumulador foi pra 0.2, NADA baixou
    de estoque. Cancelamento tem que zerar a contribuicao do acumulador.

    Antes do B9: fracao_pendente ficava em 0.2 pra sempre.
    """
    from app.extensions import db
    from app.models import SeruDebito, SeruDebitoMov, SeruPedidoProcessado, SeruProdutoMap
    from app.services.seru_sync import _baixar_item, _estornar_pedido

    mapping = SeruProdutoMap(
        seru_nome='NOZES COM MANTEIGA',
        receita_id=catalogo['receita'].id,
        fator_quantidade=0.2,
    )
    db.session.add(mapping)
    db.session.commit()

    # 1 venda × fator 0.2 = 0.2 → tudo no acumulador
    res = _baixar_item(loja.id, mapping, qtd=1,
                       seru_pedido_id='X1', user_id=admin_user.id)
    db.session.commit()
    assert res['baixado'] is False, 'nao deveria baixar inteiro ainda'

    debito = SeruDebito.query.filter_by(
        loja_id=loja.id, seru_produto_map_id=mapping.id).first()
    assert abs(debito.fracao_pendente - 0.2) < 1e-6

    fm = SeruDebitoMov.query.filter_by(seru_pedido_id='X1').first()
    assert fm is not None, 'SeruDebitoMov nao foi gravado'
    assert abs(fm.fracao - 0.2) < 1e-6

    # Cancela e estorna
    reg = SeruPedidoProcessado(seru_pedido_id='X1', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()
    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()

    db.session.refresh(debito)
    assert abs(debito.fracao_pendente) < 1e-6, (
        f'fracao deveria voltar a 0, esta em {debito.fracao_pendente}'
    )
    db.session.refresh(fm)
    assert fm.estornado_em is not None
    assert reg.estornado_em is not None


def test_b9_estorno_apos_acumulador_zerar_devolve_inteiro(app, admin_user, loja, catalogo):
    """Pedido X1 contribui 0.4. Depois, pedido X2 contribui 0.7 → acumulador
    vai pra 1.1, baixa 1 inteiro, fica 0.1.

    Se X1 eh cancelado AGORA: fracao_pendente (0.2) - contribuicao (0.4)
    = -0.2 → devolve 1 inteiro ao estoque, fracao_pendente fica 0.8.
    """
    from app.extensions import db
    from app.models import EstoqueLoja, SeruDebito, SeruPedidoProcessado, SeruProdutoMap
    from app.services.seru_sync import _baixar_item, _estornar_pedido

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=10)
    db.session.add(el)

    mapping = SeruProdutoMap(
        seru_nome='X', receita_id=catalogo['receita'].id,
        fator_quantidade=0.4,
    )
    db.session.add(mapping)
    db.session.commit()

    # X1: qtd=1 × 0.4 = 0.4
    _baixar_item(loja.id, mapping, qtd=1,
                 seru_pedido_id='X1', user_id=admin_user.id)
    db.session.commit()

    # X2: qtd=2 × 0.4 = 0.8 → acumulado 1.2, baixa 1 inteiro, fica 0.2
    _baixar_item(loja.id, mapping, qtd=2,
                 seru_pedido_id='X2', user_id=admin_user.id)
    db.session.commit()

    db.session.refresh(el)
    assert el.quantidade == 9, f'devia ter baixado 1 inteiro: {el.quantidade}'

    debito = SeruDebito.query.filter_by(
        loja_id=loja.id, seru_produto_map_id=mapping.id).first()
    assert abs(debito.fracao_pendente - 0.2) < 1e-6, debito.fracao_pendente

    # Cancela X1
    reg = SeruPedidoProcessado(seru_pedido_id='X1', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()
    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()

    # Esperado: estoque +1 (foi pra 10), fracao_pendente = 0.8
    db.session.refresh(el)
    db.session.refresh(debito)
    assert el.quantidade == 10, (
        f'estoque deveria voltar a 10 (devolveu 1 inteiro): {el.quantidade}'
    )
    assert abs(debito.fracao_pendente - 0.8) < 1e-6, (
        f'fracao residual deveria ser 0.8: {debito.fracao_pendente}'
    )


def test_b9_estorno_idempotente(app, admin_user, loja, catalogo):
    """Rodar _estornar_pedido 2x nao duplica devolucao."""
    from app.extensions import db
    from app.models import SeruDebito, SeruPedidoProcessado, SeruProdutoMap
    from app.services.seru_sync import _baixar_item, _estornar_pedido

    mapping = SeruProdutoMap(
        seru_nome='X', receita_id=catalogo['receita'].id,
        fator_quantidade=0.2,
    )
    db.session.add(mapping)
    db.session.commit()

    _baixar_item(loja.id, mapping, qtd=1,
                 seru_pedido_id='X1', user_id=admin_user.id)
    db.session.commit()

    reg = SeruPedidoProcessado(seru_pedido_id='X1', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()

    _estornar_pedido(reg, [loja], admin_user.id)
    db.session.commit()
    _estornar_pedido(reg, [loja], admin_user.id)  # 2a vez
    db.session.commit()

    debito = SeruDebito.query.filter_by(
        loja_id=loja.id, seru_produto_map_id=mapping.id).first()
    # Acumulador deveria continuar em 0 (nao ficou negativo)
    assert abs(debito.fracao_pendente) < 1e-6, (
        f'estorno duplo bagunçou acumulador: {debito.fracao_pendente}'
    )
