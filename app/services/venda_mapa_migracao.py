"""Migracao para o motor unico de baixa (`baixa_venda`).

Duas etapas, ambas IDEMPOTENTES, sem destruir os dados velhos:

1. `backfill_venda_mapa()` — copia SeruProdutoMap -> VendaMapa(canal='seru') e
   LojaProdutoMap -> VendaMapa(canal='lote'). Aditivo: roda quantas vezes quiser
   (upsert por (canal, nome_externo)). NAO muda comportamento — so popula a
   tabela nova em paralelo.

2. `migrar_fracoes_para_debito_estoque()` — soma os acumuladores velhos
   (SeruDebito + LojaDebito) por (loja, ITEM FISICO) para o DebitoEstoque novo, e
   ZERA os velhos. Roda no CUTOVER de cada canal (junto da troca do pipeline pro
   motor), pra nao perder fracao consumida-mas-nao-baixada. Idempotente porque
   zera a fonte; some >= 1 vira inteiro baixado na hora (fracao representa
   estoque ja consumido fisicamente).
"""
from app.extensions import db
from app.models import DebitoEstoque, LojaProdutoMap, MovEstoqueLoja, SeruProdutoMap, VendaMapa
from app.services.estoque_helpers import obter_linha_loja


def _upsert_venda_mapa(canal, nome_externo, *, sku=None, receita_id=None,
                       produto_id=None, materia_prima_id=None, ignorar=False,
                       fator=1.0, primeira_visto_em=None, confirmado_em=None,
                       confirmado_por=None):
    vm = VendaMapa.query.filter_by(canal=canal, nome_externo=nome_externo).first()
    novo = vm is None
    if novo:
        vm = VendaMapa(canal=canal, nome_externo=nome_externo)
        db.session.add(vm)
    vm.sku = sku
    vm.receita_id = receita_id
    vm.produto_id = produto_id
    vm.materia_prima_id = materia_prima_id
    vm.ignorar = bool(ignorar)
    vm.fator_quantidade = float(fator or 1.0)
    if primeira_visto_em:
        vm.primeira_visto_em = primeira_visto_em
    vm.confirmado_em = confirmado_em
    vm.confirmado_por = confirmado_por
    return novo


def backfill_venda_mapa():
    """Copia os mapas velhos pro VendaMapa unificado. Idempotente. Retorna
    {'seru_novos': n, 'lote_novos': n}."""
    seru_novos = lote_novos = 0
    for sm in SeruProdutoMap.query.all():
        if _upsert_venda_mapa(
                'seru', sm.seru_nome, sku=sm.seru_sku, receita_id=sm.receita_id,
                produto_id=sm.produto_id, ignorar=sm.ignorar,
                fator=sm.fator_quantidade, primeira_visto_em=sm.primeira_visto_em,
                confirmado_em=sm.confirmado_em, confirmado_por=sm.confirmado_por):
            seru_novos += 1
    for lm in LojaProdutoMap.query.all():
        if _upsert_venda_mapa(
                'lote', lm.nome_digitado, receita_id=lm.receita_id,
                produto_id=lm.produto_id, materia_prima_id=lm.materia_prima_id,
                ignorar=lm.ignorar, fator=lm.fator_quantidade,
                primeira_visto_em=lm.primeira_visto_em,
                confirmado_em=lm.confirmado_em, confirmado_por=lm.confirmado_por):
            lote_novos += 1
    db.session.commit()
    return {'seru_novos': seru_novos, 'lote_novos': lote_novos}


def _col_item_do_mapa(mapa):
    if getattr(mapa, 'receita_id', None):
        return 'receita_id', mapa.receita_id
    if getattr(mapa, 'produto_id', None):
        return 'produto_id', mapa.produto_id
    if getattr(mapa, 'materia_prima_id', None):
        return 'materia_prima_id', mapa.materia_prima_id
    return None, None


def _acumular(destino, loja_id, col, item_id, fracao):
    destino[(loja_id, col, item_id)] = destino.get((loja_id, col, item_id), 0.0) + fracao


def migrar_fracoes_para_debito_estoque(*, canais=('seru', 'lote'),
                                       usuario_id=None):
    """Soma SeruDebito/LojaDebito por (loja, item) -> DebitoEstoque e zera os
    velhos. Some >= 1 baixa o inteiro na hora (era estoque ja consumido).
    Idempotente: zera a fonte. Retorna {'itens': n, 'inteiros_baixados': n}."""
    from app.models import LojaDebito, SeruDebito

    por_item = {}
    fontes = []
    if 'seru' in canais:
        for d in SeruDebito.query.filter(SeruDebito.fracao_pendente > 0).all():
            mapa = SeruProdutoMap.query.get(d.seru_produto_map_id)
            col, item_id = _col_item_do_mapa(mapa) if mapa else (None, None)
            if col:
                _acumular(por_item, d.loja_id, col, item_id, d.fracao_pendente or 0.0)
            fontes.append(d)
    if 'lote' in canais:
        for d in LojaDebito.query.filter(LojaDebito.fracao_pendente > 0).all():
            mapa = LojaProdutoMap.query.get(d.loja_produto_map_id)
            col, item_id = _col_item_do_mapa(mapa) if mapa else (None, None)
            if col:
                _acumular(por_item, d.loja_id, col, item_id, d.fracao_pendente or 0.0)
            fontes.append(d)

    inteiros_baixados = 0
    for (loja_id, col, item_id), total in por_item.items():
        filtro = {'receita_id': None, 'produto_id': None, 'materia_prima_id': None}
        filtro[col] = item_id
        deb = DebitoEstoque.query.filter_by(loja_id=loja_id, **filtro).first()
        if deb is None:
            deb = DebitoEstoque(loja_id=loja_id, fracao_pendente=0.0, **filtro)
            db.session.add(deb)
            db.session.flush()
        total += deb.fracao_pendente or 0.0
        inteiros = int(total + 1e-9)
        if inteiros > 0:
            el = obter_linha_loja(loja_id=loja_id, usuario_id=usuario_id,
                                  **{col: item_id})
            baixa = min(inteiros, el.quantidade or 0)
            el.quantidade = (el.quantidade or 0) - baixa
            if baixa > 0:
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo='migracao_debito', quantidade=baixa,
                    referencia='Migracao SeruDebito/LojaDebito -> DebitoEstoque',
                    usuario_id=usuario_id))
            inteiros_baixados += baixa
        deb.fracao_pendente = max(0.0, round(total - inteiros, 6))

    # Zera as fontes (retiradas de uso). Mantem as linhas pra historico.
    for d in fontes:
        d.fracao_pendente = 0.0

    # Converte os SeruDebitoMov ainda NAO estornados em DebitoEstoqueMov, pra
    # que o estorno de pedidos baixados ANTES do cutover funcione pelo motor
    # novo (estornar_venda olha DebitoEstoqueMov por (canal, pedido_ref)).
    movs_migrados = 0
    if 'seru' in canais:
        from app.models import DebitoEstoqueMov, SeruDebitoMov
        for sm in SeruDebitoMov.query.filter_by(estornado_em=None).all():
            mapa = SeruProdutoMap.query.get(sm.seru_produto_map_id)
            col, item_id = _col_item_do_mapa(mapa) if mapa else (None, None)
            if not col:
                continue
            pedido_ref = 'seru:%s' % sm.seru_pedido_id
            ja = DebitoEstoqueMov.query.filter_by(
                canal='seru', pedido_ref=pedido_ref, loja_id=sm.loja_id,
                fracao=sm.fracao, **{col: item_id}).first()
            if ja:
                continue
            filtro = {'receita_id': None, 'produto_id': None,
                      'materia_prima_id': None}
            filtro[col] = item_id
            db.session.add(DebitoEstoqueMov(
                loja_id=sm.loja_id, canal='seru', pedido_ref=pedido_ref,
                fracao=sm.fracao, **filtro))
            # Marca como tratado: a fracao agora vive no DebitoEstoqueMov, entao
            # o fallback legado (`seru_sync._estornar_fracoes_legado`) nao deve
            # reverte-lo de novo. `agora` importado pra carimbar.
            from app.utils import agora
            sm.estornado_em = agora()
            movs_migrados += 1

    db.session.commit()
    return {'itens': len(por_item), 'inteiros_baixados': inteiros_baixados,
            'movs_migrados': movs_migrados}
