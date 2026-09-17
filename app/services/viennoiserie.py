"""Batimento compartilhado de viennoiserie, sem mudar a unidade histórica.

Ordens novas contam BATIMENTOS. As fichas de montagem continuam consumindo
bolas equivalentes; somente o adaptador de estoque converte a massa exata.
Não transformar Danish/Pain/Croissant em batimentos individuais de farinha.
"""

from copy import deepcopy
from decimal import Decimal

from app.services.bateladas_paes import _texto

TIPO_MASSA = 'massa_viennoiserie'
FARINHA_G = 25000


def eh_massa_compartilhada(rec):
    if rec is None or _texto(rec.nome) != 'massa para folhar':
        return False
    return any((i.tipo or 'mp') in ('mp', 'mp_direto')
               and _texto(i.ingrediente_nome).startswith('farinha')
               for i in rec.ingredientes)


def eh_item_massa(it):
    snap = getattr(it, 'batelada_padrao', None)
    return bool(snap and snap.dados.get('tipo') == TIPO_MASSA)


def padrao_massa(rec, *, resolver_estoque=True):
    from app.services.bateladas_paes import padrao_receita
    if not eh_massa_compartilhada(rec):
        return None
    dados = padrao_receita(rec, resolver_estoque=resolver_estoque,
                           farinha_g=FARINHA_G)
    from app.services.estoque_massa import peso_bola_g
    peso = peso_bola_g(rec)
    if peso <= 0:
        raise ValueError('Informe o peso da unidade histórica da massa para folhar.')
    if dados['subs']:
        raise ValueError('Revise a ficha da massa para folhar: o batimento '
                         'compartilhado precisa de ingredientes diretos.')
    dados.update(tipo=TIPO_MASSA, unidades=1, unidade_producao='batimentos',
                 peso_bola_g=float(peso),
                 rendimento_teorico=float(Decimal(str(dados['massa_g'])) / peso))
    return dados


def quantidade_em_bolas(it, quantidade):
    """WIP/estoque falam em bolas antigas; ordens com snapshot, em batimentos."""
    if eh_item_massa(it):
        return float(Decimal(str(quantidade or 0)) *
                     Decimal(str(it.batelada_padrao.dados['massa_g'])) /
                     Decimal(str(it.batelada_padrao.dados['peso_bola_g'])))
    return int(quantidade or 0)


def pendente_em_bolas(celula):
    from app.services.cronograma_bateladas import quantidade_pendente
    quantidade = quantidade_pendente(celula)
    dados = celula.get('batelada_padrao') or {}
    if dados.get('tipo') == TIPO_MASSA:
        return float(Decimal(quantidade) * Decimal(str(dados['massa_g'])) /
                     Decimal(str(dados['peso_bola_g'])))
    return quantidade


def travar_massas_consumidas(rec, snapshot=None):
    """Ordem global Receita→estoques→MP evita ciclo entre massa e montagem."""
    from app.models import Receita
    from app.services.estoque_massa import eh_massa_folhar
    from app.utils import SUB_RECEITA_TIPOS
    receitas = {r.id: r for r in Receita.query.all()}
    ids = {rec.id} if eh_massa_folhar(rec) else set()
    for sub in (snapshot or {}).get('subs', []):
        if eh_massa_folhar(receitas.get(sub['id'])):
            ids.add(sub['id'])
    for ing in rec.ingredientes:
        if ing.tipo not in SUB_RECEITA_TIPOS:
            continue
        sub = receitas.get(ing.sub_receita_id)
        if sub is None:
            sub = next((r for r in receitas.values()
                        if r.nome.strip().lower() == (ing.ingrediente_nome or '').strip().lower()), None)
        if eh_massa_folhar(sub):
            ids.add(sub.id)
    if ids:
        Receita.query.filter(Receita.id.in_(ids)).order_by(Receita.id).with_for_update().all()


def instalar_batelada(it, celula):
    """Chamada somente na criação/envio explícito; GET não congela ficha."""
    from app.extensions import db
    from app.models import PlanejamentoItemBatelada
    meta = celula.get('massa_viennoiserie')
    if not meta:
        return False
    if it.batelada_padrao is None:
        dados = padrao_massa(it.receita)
        if dados is None:
            raise ValueError('A massa compartilhada da ordem não foi encontrada.')
        it.batelada_padrao = PlanejamentoItemBatelada(dados=dados, bateladas=0)
        db.session.add(it.batelada_padrao)
    elif not eh_item_massa(it):
        raise ValueError('Esta ordem usa outra unidade; revise a ordem antes de converter.')
    dados = deepcopy(it.batelada_padrao.dados)
    dados['distribuicao'] = meta.get('distribuicao', [])
    dados['produtos'] = meta.get('produtos', [])
    dados['residuo_g'] = meta.get('residuo_g', 0)
    it.batelada_padrao.dados = dados
    it.batelada_padrao.bateladas = int(it.qtd_alvo or 0)
    return True
