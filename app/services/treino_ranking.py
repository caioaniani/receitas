"""Fase 5 — ranking, níveis e ligas (§6, §7).

Ranking PÚBLICO = por UNIDADE, normalizado (pontos/nº de ativos) — sem
normalização a maior loja ganha sempre (§7). Ranking INDIVIDUAL = privado (só o
próprio e gestor/admin). Usa o `unidade_id` CONGELADO no evento, então
transferência não distorce histórico (§5.1, critério 17). Nível por pontos na
temporada (§6).
"""
from app.extensions import db
from app.models import Loja, TreinoEventoPontos
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg

NIVEIS = ('Bronze', 'Prata', 'Ouro', 'Diamante')


def nivel(pontos):
    """Nível pelo total de pontos na temporada (§6)."""
    if pontos >= cfg.valor('NIVEL_DIAMANTE'):
        return 'Diamante'
    if pontos >= cfg.valor('NIVEL_OURO'):
        return 'Ouro'
    if pontos >= cfg.valor('NIVEL_PRATA'):
        return 'Prata'
    return 'Bronze'


def _ativos_por_unidade(unidade_id):
    if not unidade_id:
        return 0
    loja = db.session.get(Loja, unidade_id)
    return sum(1 for f in loja.funcionarios if f.ativo) if loja else 0


def ranking_unidades(temporada_id):
    """Lista de unidades ordenada por pontos NORMALIZADOS (pontos/ativos).
    NÃO expõe pontuação individual (critério 19) — só agregado por loja."""
    linhas = (db.session.query(
        TreinoEventoPontos.unidade_id,
        db.func.coalesce(db.func.sum(TreinoEventoPontos.pontos), 0))
        .filter(TreinoEventoPontos.temporada_id == temporada_id,
                TreinoEventoPontos.unidade_id.isnot(None))
        .group_by(TreinoEventoPontos.unidade_id).all())
    out = []
    for unidade_id, total in linhas:
        loja = db.session.get(Loja, unidade_id)
        n = _ativos_por_unidade(unidade_id)
        out.append({
            'unidade_id': unidade_id,
            'unidade': loja.nome if loja else '—',
            'pontos': int(total or 0),
            'ativos': n,
            'normalizado': round((total or 0) / n, 1) if n else 0,
        })
    out.sort(key=lambda x: x['normalizado'], reverse=True)
    for i, linha in enumerate(out, 1):
        linha['posicao'] = i
    return out


def posicao_individual(funcionario, temporada_id):
    """Visão PRIVADA do próprio funcionário: pontos, nível e posição dentro da
    própria unidade (não exposta publicamente)."""
    meus = ledger.saldo(funcionario.id, temporada_id)
    unidade = ledger.unidade_do_funcionario(funcionario)
    posicao = None
    total_unidade = None
    if unidade is not None:
        colegas = [f for f in unidade.funcionarios if f.ativo]
        pontos = sorted(
            (ledger.saldo(f.id, temporada_id) for f in colegas), reverse=True)
        total_unidade = len(pontos)
        posicao = next((i for i, p in enumerate(pontos, 1) if p <= meus), None)
    return {'pontos': meus, 'nivel': nivel(meus), 'posicao_na_unidade': posicao,
            'total_na_unidade': total_unidade,
            'unidade': unidade.nome if unidade else None}
