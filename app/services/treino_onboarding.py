"""v2 §16.1 e §16.3 — onboarding automático por cargo + progressão.

§16.1: na admissão, o funcionário JÁ vê as trilhas exigidas do cargo dele
(nada de atribuição manual — vem do mapa cargo↔trilha).
§16.3: progressão = o funcionário está APTO no cargo quando concluiu (tem selo
de) todas as trilhas obrigatórias do cargo. Liga os selos à evolução de cargo.
"""
from app.extensions import db
from app.models import TreinoSelo, TreinoTrilha, TreinoTrilhaCargo


def trilhas_do_cargo(cargo_id, so_obrigatorias=False):
    """Trilhas ATIVAS exigidas por um cargo."""
    if not cargo_id:
        return []
    q = (db.session.query(TreinoTrilha)
         .join(TreinoTrilhaCargo,
               TreinoTrilhaCargo.trilha_id == TreinoTrilha.id)
         .filter(TreinoTrilhaCargo.cargo_id == cargo_id,
                 TreinoTrilha.ativa.is_(True)))
    if so_obrigatorias:
        q = q.filter(TreinoTrilhaCargo.obrigatoria.is_(True))
    return q.order_by(TreinoTrilha.ordem).all()


def onboarding_do_funcionario(funcionario):
    """Trilhas de onboarding do funcionário = as do cargo dele (§16.1)."""
    return trilhas_do_cargo(getattr(funcionario, 'cargo_id', None))


def definir_cargos_da_trilha(trilha_id, cargo_ids):
    """Substitui o conjunto de cargos que exigem a trilha (idempotente)."""
    cargo_ids = {int(c) for c in cargo_ids if str(c).strip()}
    atuais = {m.cargo_id: m for m in TreinoTrilhaCargo.query.filter_by(
        trilha_id=trilha_id).all()}
    for cid, m in atuais.items():
        if cid not in cargo_ids:
            db.session.delete(m)
    for cid in cargo_ids:
        if cid not in atuais:
            db.session.add(TreinoTrilhaCargo(trilha_id=trilha_id, cargo_id=cid))
    db.session.commit()


def progressao(funcionario):
    """Estado de progressão do funcionário no cargo atual (§16.3): as trilhas
    obrigatórias do cargo e se cada uma já tem selo; `apto` quando todas têm."""
    cargo_id = getattr(funcionario, 'cargo_id', None)
    exigidas = trilhas_do_cargo(cargo_id, so_obrigatorias=True)
    com_selo = {s.trilha_id for s in TreinoSelo.query.filter_by(
        funcionario_id=funcionario.id).all()}
    itens = [{'trilha': t, 'tem_selo': t.id in com_selo} for t in exigidas]
    apto = bool(exigidas) and all(i['tem_selo'] for i in itens)
    return {'cargo': getattr(funcionario, 'cargo', None), 'itens': itens,
            'apto': apto, 'total': len(itens),
            'concluidas': sum(1 for i in itens if i['tem_selo'])}
