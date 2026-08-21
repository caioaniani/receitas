"""v2 §16.1 e §16.3 — onboarding automático por cargo + progressão.

§16.1: na admissão, o funcionário JÁ vê as trilhas exigidas do cargo dele
(nada de atribuição manual — vem do mapa cargo↔trilha).
§16.3: progressão = o funcionário está APTO no cargo quando concluiu (tem selo
de) todas as trilhas obrigatórias do cargo. Liga os selos à evolução de cargo.
"""
from app.extensions import db
from app.models import Cargo, TreinoSelo, TreinoTrilha, TreinoTrilhaCargo


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


def onboarding_lote(funcionarios):
    """Trilhas ativas do cargo para uma lista de funcionários, sem N+1."""
    funcionarios = list(funcionarios)
    cargo_ids = {f.cargo_id for f in funcionarios
                 if getattr(f, 'cargo_id', None)}
    por_cargo = {}
    if cargo_ids:
        rows = (db.session.query(TreinoTrilhaCargo.cargo_id, TreinoTrilha)
                .join(TreinoTrilha,
                      TreinoTrilha.id == TreinoTrilhaCargo.trilha_id)
                .filter(TreinoTrilhaCargo.cargo_id.in_(cargo_ids),
                        TreinoTrilha.ativa.is_(True))
                .order_by(TreinoTrilha.ordem).all())
        for cargo_id, trilha in rows:
            por_cargo.setdefault(cargo_id, []).append(trilha)
    return {f.id: por_cargo.get(getattr(f, 'cargo_id', None), [])
            for f in funcionarios}


def definir_cargos_da_trilha(trilha_id, cargo_ids):
    """Substitui o conjunto de cargos que exigem a trilha (idempotente).

    Os ids vêm do form (`request.form.getlist`) — parse tolerante (ignora não
    numérico) e SÓ aceita cargos que existem de fato (evita FK órfã / 500 em
    Postgres com id inventado por POST forjado)."""
    pedidos = set()
    for c in cargo_ids:
        try:
            pedidos.add(int(str(c).strip()))
        except (TypeError, ValueError):
            continue
    validos = {c.id for c in Cargo.query.filter(
        Cargo.id.in_(pedidos)).all()} if pedidos else set()
    atuais = {m.cargo_id: m for m in TreinoTrilhaCargo.query.filter_by(
        trilha_id=trilha_id).all()}
    for cid, m in atuais.items():
        if cid not in validos:
            db.session.delete(m)
    for cid in validos:
        if cid not in atuais:
            db.session.add(TreinoTrilhaCargo(trilha_id=trilha_id, cargo_id=cid))
    db.session.commit()


def _montar_progressao(funcionario, exigidas, com_selo_ids):
    itens = [{'trilha': t, 'tem_selo': t.id in com_selo_ids} for t in exigidas]
    apto = bool(exigidas) and all(i['tem_selo'] for i in itens)
    return {'cargo': getattr(funcionario, 'cargo', None), 'itens': itens,
            'apto': apto, 'total': len(itens),
            'concluidas': sum(1 for i in itens if i['tem_selo'])}


def progressao(funcionario):
    """Estado de progressão do funcionário no cargo atual (§16.3): as trilhas
    obrigatórias do cargo e se cada uma já tem selo; `apto` quando todas têm."""
    cargo_id = getattr(funcionario, 'cargo_id', None)
    exigidas = trilhas_do_cargo(cargo_id, so_obrigatorias=True)
    com_selo = {s.trilha_id for s in TreinoSelo.query.filter_by(
        funcionario_id=funcionario.id).all()}
    return _montar_progressao(funcionario, exigidas, com_selo)


def progressao_lote(funcionarios):
    """Progressão de VÁRIOS funcionários sem N+1 (relatório do gestor): agrega
    trilhas-por-cargo e selos em 2 queries e resolve em memória.
    Devolve dict {funcionario_id: progressao}."""
    funcionarios = list(funcionarios)
    if not funcionarios:
        return {}
    cargo_ids = {f.cargo_id for f in funcionarios if getattr(f, 'cargo_id', None)}
    func_ids = [f.id for f in funcionarios]
    # trilhas obrigatórias ATIVAS por cargo (1 query)
    exig_por_cargo = {}
    if cargo_ids:
        rows = (db.session.query(TreinoTrilhaCargo.cargo_id, TreinoTrilha)
                .join(TreinoTrilha,
                      TreinoTrilha.id == TreinoTrilhaCargo.trilha_id)
                .filter(TreinoTrilhaCargo.cargo_id.in_(cargo_ids),
                        TreinoTrilhaCargo.obrigatoria.is_(True),
                        TreinoTrilha.ativa.is_(True))
                .order_by(TreinoTrilha.ordem).all())
        for cid, trilha in rows:
            exig_por_cargo.setdefault(cid, []).append(trilha)
    # selos por funcionário (1 query)
    selos_por_func = {}
    for s in TreinoSelo.query.filter(
            TreinoSelo.funcionario_id.in_(func_ids)).all():
        selos_por_func.setdefault(s.funcionario_id, set()).add(s.trilha_id)
    out = {}
    for f in funcionarios:
        exigidas = exig_por_cargo.get(getattr(f, 'cargo_id', None), [])
        out[f.id] = _montar_progressao(
            f, exigidas, selos_por_func.get(f.id, set()))
    return out
