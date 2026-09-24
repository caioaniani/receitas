"""Geracao de pedidos da semana em RASCUNHO a partir da sugestao do historico
(Fatia 2). O sistema propoe (previsao_producao.sugerir_pedidos_semana), o admin
ajusta na tela e gera; a criacao real dos PedidoLoja acontece aqui, sempre como
status 'pendente' (rascunho) — pra o admin revisar e confirmar depois.

`aplicar_grade` estende o gerar: dia que JA tem pedido EDITAVEL (pendente/
confirmado — mesma regra da rota /pedidos/<id>/editar) tem os itens
ATUALIZADOS a partir da grade (a tela da media destrava essas celulas).
"""
from app.constants import STATUS_PEDIDO_EDITAVEIS as STATUS_EDITAVEIS
from app.constants import STATUS_PEDIDO_ENTREGUES
from app.extensions import db
from app.models import PedidoItem, PedidoLoja
from app.services.pedido_lock import protegido_do_motor, travar_pedidos_lojas
from app.services.previsao_producao import invalidar_sugestao_cache
from app.utils import agora, hoje


class PedidoLoteInvalidoError(ValueError):
    """A grade contém item inelegível ou quantidade incompatível com o lote."""


class PedidoCorteError(ValueError):
    """A seleção inclui uma data fechada pelo corte dos pedidos."""


def _validar_corte_da_grade(pedidos, datas_adicionais=()):
    """Recusa a seleção inteira, inclusive se o corte chegou durante a escrita.

    Os chamadores adquirem a trava antes de validar. A mesma verificação
    antes do commit desfaz toda a transação se ela atravessou o corte.
    """
    from app.services.pedido_corte import bloqueio_do_corte

    bloqueado, mensagem = bloqueio_do_corte(
        [ped.get('data_entrega') for ped in pedidos] + list(datas_adicionais))
    if bloqueado:
        db.session.rollback()
        raise PedidoCorteError(mensagem)


def _validar_lotes_da_grade(pedidos):
    """Valida toda a seleção antes de criar ou modificar qualquer pedido."""
    from app.services.pedido_loja_catalogo import validar_itens_loja
    from app.services.pedido_lote import violacoes_por_ids

    # Qtd zero é remoção; a trava impede incluir/manter minis na seleção.
    try:
        validar_itens_loja(it for ped in pedidos for it in (ped.get('itens') or [])
                          if int(it.get('qtd') or 0) > 0)
    except ValueError as exc:
        raise PedidoLoteInvalidoError(str(exc)) from exc

    itens = [{'receita_id': int(it['receita_id']), 'quantidade': it.get('qtd')}
             for ped in pedidos for it in (ped.get('itens') or [])
             if it.get('receita_id')]
    erros = violacoes_por_ids(itens)
    if erros:
        raise PedidoLoteInvalidoError(' '.join(erros))
    from app.models import Produto
    from app.services.cestas import produto_reposicao_direta

    ids = {int(it['produto_id']) for ped in pedidos for it in (ped.get('itens') or [])
           if it.get('produto_id') and int(it.get('qtd') or 0) > 0}
    produtos = {p.id: p for p in Produto.query.filter(Produto.id.in_(ids)).all()} if ids else {}
    for pid in ids:
        if not produto_reposicao_direta(produtos.get(pid)):
            raise PedidoLoteInvalidoError(
                'A reposição direta exige produto ativo e sem composição. '
                'Para cestas, configure a reposição dos componentes.')


def criar_pedidos_rascunho(pedidos, user_id):
    """Cria PedidoLoja em rascunho ('pendente') a partir de uma lista
    [{loja_id, data_entrega(date), itens: [{receita_id OU materia_prima_id,
    qtd}]}]. Item de MP cobre insumo comprado que a loja pede e a industria
    envia sem produzir (ex: pao de queijo congelado).

    Salvaguardas:
    - Pula (loja, data) que JA tem pedido nao-cancelado — anti-duplicacao,
      re-checado no banco no momento da criacao (cobre corrida com a tela).
    - Ignora itens com qtd <= 0 e pedidos que ficam sem item.

    Retorna {'criados': n, 'pulados_existentes': n, 'itens': n}.
    """
    pedidos = list(pedidos)
    travar_pedidos_lojas(ped.get('loja_id') for ped in pedidos)
    _validar_corte_da_grade(pedidos)
    _validar_lotes_da_grade(pedidos)
    criados = pulados = total_itens = 0
    hoje_d = hoje()
    for ped in pedidos:
        loja_id = ped.get('loja_id')
        data_ent = ped.get('data_entrega')
        itens = [it for it in (ped.get('itens') or [])
                 if (it.get('receita_id') or it.get('materia_prima_id') or it.get('produto_id'))
                 and int(it.get('qtd') or 0) > 0]
        if not loja_id or not data_ent or not itens:
            continue
        # Anti-duplicacao: re-checa no banco (a tela ja sinaliza, mas pode
        # ter mudado entre o GET e o POST). Pedido finalizado ANTES da data
        # (entrega antecipada/emergencia) nao conta como "o pedido do dia" —
        # mesma regra da grade (previsao_producao._cond_sem_entrega_antecipada).
        q_existe = (PedidoLoja.query
                    .filter(PedidoLoja.loja_id == loja_id,
                            PedidoLoja.data_entrega == data_ent,
                            PedidoLoja.status != 'cancelado'))
        if data_ent > hoje_d:
            q_existe = q_existe.filter(
                ~PedidoLoja.status.in_(STATUS_PEDIDO_ENTREGUES))
        existe = q_existe.first()
        if existe:
            pulados += 1
            continue
        from app.services.pedido_merge import OBSERVACAO_RASCUNHO_AUTO
        pedido = PedidoLoja(
            loja_id=loja_id,
            data_entrega=data_ent,
            data_pedido=hoje_d,
            status='pendente',
            criado_por=user_id,
            observacao=OBSERVACAO_RASCUNHO_AUTO,
        )
        db.session.add(pedido)
        db.session.flush()
        for it in itens:
            rid = it.get('receita_id')
            mid = it.get('materia_prima_id')
            pid = it.get('produto_id')
            db.session.add(PedidoItem(
                pedido_id=pedido.id,
                receita_id=int(rid) if rid else None,
                materia_prima_id=int(mid) if mid else None,
                produto_id=int(pid) if pid else None,
                quantidade=int(it['qtd']),
            ))
            total_itens += 1
        criados += 1

    # A escrita pendente também pode esperar no banco e atravessar o corte.
    db.session.flush()
    _validar_corte_da_grade(pedidos)
    db.session.commit()
    invalidar_sugestao_cache()
    return {'criados': criados, 'pulados_existentes': pulados,
            'itens': total_itens}


def _sincronizar_itens(pedido, itens, user_id):
    """Ajusta os itens de um pedido EDITAVEL a partir da grade.

    Semantica de EDICAO PARCIAL (a grade so mostra parte do mundo da loja):
    - qtd DIFERENTE da atual -> ajusta;
    - qtd 0 e o item existe -> REMOVE;
    - qtd > 0 e o item nao existe -> adiciona;
    - item do pedido que NAO veio no form -> fica intacto;
    - item com MAIS DE UMA linha no pedido (estados assado/backup) -> NAO toca
      (a grade mostra a soma; ajustar as parcelas e ambiguo — use a edicao do
      pedido). Conta em `ambiguos`.
    Carimba modificado_em/por (mesma trilha da rota /pedidos/<id>/editar; o
    AuditLog automatico captura as mudancas). Retorna (ajustados, ambiguos).
    """
    # Defesa também para chamadores diretos: a decisão e a escrita pertencem
    # à mesma transação, sob a mesma trava usada pelos gestos humanos.
    travar_pedidos_lojas([pedido.loja_id])
    if user_id is None:
        db.session.refresh(pedido)
        db.session.expire(pedido, ['itens'])
        if protegido_do_motor(pedido):
            return 0, 0
    _validar_corte_da_grade([{'data_entrega': pedido.data_entrega}])
    _validar_lotes_da_grade([{'itens': itens}])
    por_chave = {}
    duplicados = set()
    for it in pedido.itens:
        chave = ('r', it.receita_id) if it.receita_id else \
                ('m', it.materia_prima_id) if it.materia_prima_id else \
                ('p', it.produto_id)
        if chave in por_chave:
            duplicados.add(chave)
        por_chave[chave] = it

    ajustados = ambiguos = 0
    for it in itens:
        rid = it.get('receita_id')
        mid = it.get('materia_prima_id')
        pid = it.get('produto_id')
        if not rid and not mid and not pid:
            continue
        chave = ('r', int(rid)) if rid else ('m', int(mid)) if mid else ('p', int(pid))
        qtd = int(it.get('qtd') or 0)
        if chave in duplicados:
            atual_soma = sum(x.quantidade or 0 for x in pedido.itens
                             if (('r', x.receita_id) if x.receita_id else
                                 ('m', x.materia_prima_id) if x.materia_prima_id else
                                 ('p', x.produto_id)) == chave)
            if qtd != atual_soma:
                ambiguos += 1
            continue
        atual = por_chave.get(chave)
        if atual is None:
            if qtd > 0:
                db.session.add(PedidoItem(
                    pedido_id=pedido.id,
                    receita_id=int(rid) if rid else None,
                    materia_prima_id=int(mid) if mid else None,
                    produto_id=int(pid) if pid else None,
                    quantidade=qtd,
                ))
                ajustados += 1
        elif qtd <= 0:
            db.session.delete(atual)
            ajustados += 1
        elif int(atual.quantidade or 0) != qtd:
            atual.quantidade = qtd
            ajustados += 1
    if ajustados:
        pedido.modificado_em = agora()
        # O cron (user_id=None) NUNCA apaga um carimbo humano existente: na
        # corrida "humano adota o rascunho entre o snapshot de proteção e o
        # commit do cron", zerar o carimbo deixaria o pedido re-sincronizável
        # pra sempre (achado da revisão rodada 2). Humano sempre carimba.
        if user_id is not None or pedido.modificado_por_id is None:
            pedido.modificado_por_id = user_id
    return ajustados, ambiguos


def aplicar_grade(pedidos, user_id, *, datas_adicionais_corte=()):
    """Aplica a grade da tela de pedidos da semana: (loja, dia) SEM pedido vira
    rascunho novo (como `criar_pedidos_rascunho`); dia COM pedido EDITAVEL
    (pendente/confirmado, e um so) tem os itens sincronizados. Pedido alem de
    confirmado, ou dia com MAIS de um pedido, nao e tocado.

    O cron informa em `datas_adicionais_corte` os dias em que já absorveu ou
    cancelou rascunhos nesta transação. Eles também precisam continuar abertos
    até o commit, mesmo quando não sobrou nenhuma linha na grade.

    Retorna {'criados', 'itens', 'atualizados', 'itens_ajustados',
             'itens_ambiguos', 'pulados_nao_editavel', 'pulados_multiplos'}.
    """
    pedidos = list(pedidos)
    datas_adicionais_corte = tuple(datas_adicionais_corte)
    travar_pedidos_lojas(ped.get('loja_id') for ped in pedidos)
    _validar_corte_da_grade(pedidos, datas_adicionais_corte)
    _validar_lotes_da_grade(pedidos)
    hoje_d = hoje()
    out = {'criados': 0, 'itens': 0, 'atualizados': 0, 'itens_ajustados': 0,
           'itens_ambiguos': 0, 'pulados_nao_editavel': 0,
           'pulados_multiplos': 0, 'pulados_humano': 0}
    for ped in pedidos:
        loja_id = ped.get('loja_id')
        data_ent = ped.get('data_entrega')
        itens = [it for it in (ped.get('itens') or [])
                 if it.get('receita_id') or it.get('materia_prima_id') or it.get('produto_id')]
        if not loja_id or not data_ent or not itens:
            continue
        todos = (PedidoLoja.query.populate_existing()
                      .filter(PedidoLoja.loja_id == loja_id,
                              PedidoLoja.data_entrega == data_ent)
                      .all())
        if user_id is None and any(protegido_do_motor(p, hoje_d) for p in todos):
            out['pulados_humano'] += 1
            continue
        existentes = [p for p in todos if p.status != 'cancelado']
        if data_ent > hoje_d:
            # Entrega antecipada (finalizado antes da data, ex: pedido de
            # emergencia da madrugada entregue no caminhao de hoje) nao e
            # "o pedido do dia": nao bloqueia criar o pedido real nem e
            # alvo de atualizacao. Caso Anesio 08/07/2026.
            existentes = [p for p in existentes
                          if p.status not in STATUS_PEDIDO_ENTREGUES]
        if not existentes:
            novos = [it for it in itens if int(it.get('qtd') or 0) > 0]
            if not novos:
                continue
            from app.services.pedido_merge import OBSERVACAO_RASCUNHO_AUTO
            pedido = PedidoLoja(
                loja_id=loja_id, data_entrega=data_ent, data_pedido=hoje_d,
                status='pendente', criado_por=user_id,
                observacao=OBSERVACAO_RASCUNHO_AUTO,
            )
            db.session.add(pedido)
            db.session.flush()
            for it in novos:
                rid = it.get('receita_id')
                mid = it.get('materia_prima_id')
                pid = it.get('produto_id')
                db.session.add(PedidoItem(
                    pedido_id=pedido.id,
                    receita_id=int(rid) if rid else None,
                    materia_prima_id=int(mid) if mid else None,
                    produto_id=int(pid) if pid else None,
                    quantidade=int(it['qtd']),
                ))
                out['itens'] += 1
            out['criados'] += 1
            continue
        if len(existentes) > 1:
            out['pulados_multiplos'] += 1
            continue
        pedido = existentes[0]
        if pedido.status not in STATUS_EDITAVEIS:
            out['pulados_nao_editavel'] += 1
            continue
        ajustados, ambiguos = _sincronizar_itens(pedido, itens, user_id)
        out['itens_ajustados'] += ajustados
        out['itens_ambiguos'] += ambiguos
        if ajustados:
            out['atualizados'] += 1

    db.session.flush()
    _validar_corte_da_grade(pedidos, datas_adicionais_corte)
    db.session.commit()
    invalidar_sugestao_cache()
    return out
