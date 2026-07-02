"""Geracao de pedidos da semana em RASCUNHO a partir da sugestao do historico
(Fatia 2). O sistema propoe (previsao_producao.sugerir_pedidos_semana), o admin
ajusta na tela e gera; a criacao real dos PedidoLoja acontece aqui, sempre como
status 'pendente' (rascunho) — pra o admin revisar e confirmar depois.

`aplicar_grade` estende o gerar: dia que JA tem pedido EDITAVEL (pendente/
confirmado — mesma regra da rota /pedidos/<id>/editar) tem os itens
ATUALIZADOS a partir da grade (a tela da media destrava essas celulas).
"""
from app.extensions import db
from app.models import PedidoItem, PedidoLoja
from app.services.previsao_producao import invalidar_sugestao_cache
from app.utils import agora, hoje

# Mesma regra da rota oficial de edicao (/pedidos/<id>/editar): depois de
# separado o pedido ja esta no fluxo fisico — cancela e recria, nao edita.
STATUS_EDITAVEIS = ('pendente', 'confirmado')


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
    criados = pulados = total_itens = 0
    hoje_d = hoje()
    for ped in pedidos:
        loja_id = ped.get('loja_id')
        data_ent = ped.get('data_entrega')
        itens = [it for it in (ped.get('itens') or [])
                 if (it.get('receita_id') or it.get('materia_prima_id'))
                 and int(it.get('qtd') or 0) > 0]
        if not loja_id or not data_ent or not itens:
            continue
        # Anti-duplicacao: re-checa no banco (a tela ja sinaliza, mas pode
        # ter mudado entre o GET e o POST).
        existe = (PedidoLoja.query
                  .filter(PedidoLoja.loja_id == loja_id,
                          PedidoLoja.data_entrega == data_ent,
                          PedidoLoja.status != 'cancelado')
                  .first())
        if existe:
            pulados += 1
            continue
        pedido = PedidoLoja(
            loja_id=loja_id,
            data_entrega=data_ent,
            data_pedido=hoje_d,
            status='pendente',
            criado_por=user_id,
            observacao='Gerado do histórico (rascunho) — revisar e confirmar.',
        )
        db.session.add(pedido)
        db.session.flush()
        for it in itens:
            rid = it.get('receita_id')
            mid = it.get('materia_prima_id')
            db.session.add(PedidoItem(
                pedido_id=pedido.id,
                receita_id=int(rid) if rid else None,
                materia_prima_id=int(mid) if mid else None,
                quantidade=int(it['qtd']),
            ))
            total_itens += 1
        criados += 1

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
        if not rid and not mid:
            continue
        chave = ('r', int(rid)) if rid else ('m', int(mid))
        qtd = int(it.get('qtd') or 0)
        if chave in duplicados:
            atual_soma = sum(x.quantidade or 0 for x in pedido.itens
                             if (('r', x.receita_id) if x.receita_id else
                                 ('m', x.materia_prima_id)) == chave)
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
        pedido.modificado_por_id = user_id
    return ajustados, ambiguos


def aplicar_grade(pedidos, user_id):
    """Aplica a grade da tela de pedidos da semana: (loja, dia) SEM pedido vira
    rascunho novo (como `criar_pedidos_rascunho`); dia COM pedido EDITAVEL
    (pendente/confirmado, e um so) tem os itens sincronizados. Pedido alem de
    confirmado, ou dia com MAIS de um pedido, nao e tocado.

    Retorna {'criados', 'itens', 'atualizados', 'itens_ajustados',
             'itens_ambiguos', 'pulados_nao_editavel', 'pulados_multiplos'}.
    """
    hoje_d = hoje()
    out = {'criados': 0, 'itens': 0, 'atualizados': 0, 'itens_ajustados': 0,
           'itens_ambiguos': 0, 'pulados_nao_editavel': 0,
           'pulados_multiplos': 0}
    for ped in pedidos:
        loja_id = ped.get('loja_id')
        data_ent = ped.get('data_entrega')
        itens = [it for it in (ped.get('itens') or [])
                 if it.get('receita_id') or it.get('materia_prima_id')]
        if not loja_id or not data_ent or not itens:
            continue
        existentes = (PedidoLoja.query
                      .filter(PedidoLoja.loja_id == loja_id,
                              PedidoLoja.data_entrega == data_ent,
                              PedidoLoja.status != 'cancelado')
                      .all())
        if not existentes:
            novos = [it for it in itens if int(it.get('qtd') or 0) > 0]
            if not novos:
                continue
            pedido = PedidoLoja(
                loja_id=loja_id, data_entrega=data_ent, data_pedido=hoje_d,
                status='pendente', criado_por=user_id,
                observacao='Gerado do histórico (rascunho) — revisar e confirmar.',
            )
            db.session.add(pedido)
            db.session.flush()
            for it in novos:
                rid = it.get('receita_id')
                mid = it.get('materia_prima_id')
                db.session.add(PedidoItem(
                    pedido_id=pedido.id,
                    receita_id=int(rid) if rid else None,
                    materia_prima_id=int(mid) if mid else None,
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

    db.session.commit()
    invalidar_sugestao_cache()
    return out
