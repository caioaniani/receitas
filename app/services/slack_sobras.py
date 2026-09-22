"""Escolha explícita entre nova sobra e retirada de estoque já convertido.

O modelo propõe os itens, mas não decide sozinho se deve somar retorno.
As opções são calculadas novamente no clique, inclusive para tokens antigos.
"""
from app.extensions import db

TIPOS_SOBRA = {'registrar_desperdicio', 'registrar_desperdicio_lote'}
ACAO_NOVA_SOBRA = 'copilot_nova_sobra'
ACAO_PREPARAR_RETIRADA = 'copilot_preparar_retirada'


def opcoes_sobra(tipo, params, user):
    """Lê o efeito real da proposta; saldo existente nunca impede nova sobra."""
    if tipo not in TIPOS_SOBRA:
        return None

    from sqlalchemy import func

    from app.models import EstoqueLoja, Receita
    from app.services import copilot
    from app.services.desperdicio_core import normalizar_motivo

    loja = copilot._resolver_loja_para_user(
        params.get('loja_id'), params.get('loja_nome'), user)
    motivo = normalizar_motivo(params.get('motivo'))
    itens = (params.get('itens') or []) if tipo.endswith('_lote') else [{
        'nome': params.get('item_nome'), 'quantidade': params.get('quantidade')}]
    conversoes = []
    for item in itens:
        nome = (item.get('nome') or '').strip()
        try:
            qtd = int(item.get('quantidade') or 0)
        except (TypeError, ValueError):
            continue
        if not nome or qtd <= 0:
            continue
        resolvido = item.get('resolvido') or {}
        if resolvido.get('id'):
            match = (resolvido.get('tipo'), resolvido['id'],
                     resolvido.get('nome') or nome)
        else:
            match = copilot._resolver_item_qualquer(nome)
        if not match:
            continue
        tipo_item, item_id, nome_ok = match
        sugestao = copilot._retirada_sugerida_preview(
            tipo_item, item_id, nome_ok, qtd, motivo)
        if not sugestao:
            continue
        receita = db.session.get(Receita, item_id)
        retorno = receita.retorno_receita
        if retorno is None:
            continue
        saldo = None
        if loja:
            saldo = db.session.query(func.coalesce(func.sum(EstoqueLoja.quantidade), 0)).filter(
                EstoqueLoja.loja_id == loja.id,
                EstoqueLoja.receita_id == retorno.id).scalar()
        conversoes.append({
            'nome': nome_ok, 'receita_id': item_id, 'quantidade': qtd,
            'retorno_id': retorno.id, 'retorno_nome': retorno.nome,
            'saldo_retorno': saldo,
        })
    if not conversoes:
        return None
    return {
        'conversoes': conversoes,
        # Nunca descartar silenciosamente os outros itens de um lote misto.
        'pode_retirar': bool(loja and len(conversoes) == len(itens)),
        'loja_id': loja.id if loja else None,
        'loja_nome': loja.nome if loja else None,
    }


def parametros_retirada(opcoes, params, user):
    """Prepara outra prévia, sem registrar sobra nem movimentar estoque."""
    if not opcoes or not opcoes['pode_retirar']:
        raise ValueError('Envie a retirada separada dos demais itens de sobra ou perda.')
    from datetime import timedelta

    from app.utils import hoje

    # Preservar a identidade original evita novo fuzzy match e deixa o
    # executor canônico mapear origem → retorno UMA vez (inclusive cadeias).
    itens = [{
        'nome': item['nome'], 'quantidade': item['quantidade'],
        'resolvido': {'tipo': 'receita', 'id': item['receita_id'], 'nome': item['nome']},
        'destino_industria': item['retorno_nome'],
        'estoque_atual': item['saldo_retorno'],
    } for item in opcoes['conversoes']]
    out = {
        'loja_id': opcoes['loja_id'], 'loja_nome': opcoes['loja_nome'], 'itens': itens,
        'data_retirada': (hoje() + timedelta(days=1)).isoformat(),
        'observacao': params.get('observacao'),
    }
    for chave in ('imagens', '_n_imagens', '_foto_anterior', '_origem_slack'):
        if chave in params:
            out[chave] = params[chave]
    return out
