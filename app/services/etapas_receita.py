"""Etapas de produção de receita — fonte ÚNICA de parse/salvamento (14/07/2026).

Antes estes helpers viviam privados no blueprint de receitas (editor do admin).
Com a ficha de preparo do padeiro (/padeiro/fichas) passando a gravar as MESMAS
etapas, o parse do form e o replace no banco centralizam aqui — os dois editores
não podem divergir (regra do CLAUDE.md sobre implementações duplicadas).

Cada etapa: nome + duração (min) + tipo de trabalho (padeiro/máquina/descanso,
via RECURSO_MAP) + `descricao` (o QUE fazer na etapa — passo a passo que o
padeiro preenche; alimenta o drawer de mise en place e o fluxograma).
"""
from app.extensions import db
from app.models import ReceitaEtapa

# Tipo de trabalho (select do form) -> (equipamento, ativa).
#  - padeiro: mão de obra (ocupa a pessoa).
#  - amassadeira/forno: máquina trabalha sozinha (1 de cada na padaria).
#  - camara_fria/descanso: fermentação/descanso passivo — não ocupa ninguém.
#  - congelar: passo FINAL (freezer) — produto pronto e congelado; não é
#    fermentação (não vira marcador de câmara fria nem antecipa a produção).
RECURSO_MAP = {
    'padeiro': (None, True),
    'amassadeira': ('amassadeira', True),
    'forno': ('forno', True),
    'camara_fria': ('camara_fria', False),
    'congelar': ('congelar', False),
    'descanso': (None, False),
}

DESCRICAO_MAX = 2000


def recurso_de_etapa(e):
    """Valor do select 'tipo de trabalho' a partir da etapa salva."""
    if e.equipamento in ('amassadeira', 'forno', 'camara_fria', 'congelar'):
        return e.equipamento
    return 'padeiro' if e.ativa else 'descanso'


def parse_etapas_form(form):
    """Lê as linhas de etapa do form → lista de dicts
    {nome, duracao_min, equipamento, ativa, descricao}, pulando linhas sem
    nome. Usado pelo editor do admin E pela ficha do padeiro."""
    out = []
    nomes = form.getlist('nome[]')
    duracoes = form.getlist('duracao[]')
    recursos = form.getlist('recurso[]')
    descricoes = form.getlist('descricao[]')
    for i, nome in enumerate(nomes):
        nome = (nome or '').strip()
        if not nome:
            continue            # linha vazia = ignora
        dur_raw = duracoes[i] if i < len(duracoes) else 0
        try:
            dur_min = max(0, min(int(dur_raw or 0), 100000))
        except (TypeError, ValueError):
            dur_min = 0
        recurso = recursos[i] if i < len(recursos) else ''
        equip, ativa = RECURSO_MAP.get(recurso, (None, True))
        desc = (descricoes[i] if i < len(descricoes) else '') or ''
        desc = desc.strip()[:DESCRICAO_MAX] or None
        out.append({'nome': nome[:80], 'duracao_min': dur_min,
                    'equipamento': equip, 'ativa': ativa, 'descricao': desc})
    return out


def de_tuplas(padrao):
    """Converte o padrão da categoria (tuplas (nome, dur, equip, ativa) de
    app/constants.py) pro formato dict do serviço — sem descrição."""
    return [{'nome': nome, 'duracao_min': dur, 'equipamento': equip,
             'ativa': ativa, 'descricao': None}
            for nome, dur, equip, ativa in padrao]


def set_etapas(receita_id, etapas):
    """Substitui as etapas de uma receita pela lista de dicts (ordem = ordem
    da lista). Não commita — o chamador fecha a transação."""
    ReceitaEtapa.query.filter_by(receita_id=receita_id).delete()
    for i, e in enumerate(etapas):
        db.session.add(ReceitaEtapa(
            receita_id=receita_id, ordem=i, nome=e['nome'],
            duracao_min=e.get('duracao_min') or 0,
            equipamento=e.get('equipamento'),
            ativa=bool(e.get('ativa', True)),
            descricao=e.get('descricao')))


def listar(receita_id):
    """Etapas da receita na ordem cadastrada."""
    return (ReceitaEtapa.query.filter_by(receita_id=receita_id)
            .order_by(ReceitaEtapa.ordem).all())
