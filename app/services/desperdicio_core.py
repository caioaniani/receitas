"""Regras compartilhadas de desperdício/perda (03/07/2026).

A tela /pedidos/desperdicio e o copilot aplicavam regras DIFERENTES pro mesmo
ato: a tela ignorava a flag `reaproveitavel` e gravava motivos legados
('vencido'/'estragado'/'queimado'), então o MESMO croissant baixava estoque
pela tela e não baixava pelo copilot — e o motivo gravado divergia por canal,
quebrando qualquer relatório. Este módulo é a fonte única das duas regras:

- `normalizar_motivo`: motivo legado → canônico (`constants.DESPERDICIO_MOTIVOS`).
- `reaproveita_sem_baixa`: True quando o item (Receita/Produto com flag
  `reaproveitavel=True`) + motivo reaproveitável ('validade'/'nao_vendeu')
  devem registrar o desperdício SEM baixar o estoque — o item vira outra
  coisa em vez de virar lixo (decisão do dono 02/07/2026, ciclo
  croissant→almond). Matéria-prima nunca reaproveita.
"""
from app.constants import (
    DESPERDICIO_MOTIVOS,
    DESPERDICIO_MOTIVOS_REAPROVEITAVEIS,
)
from app.extensions import db

# Motivos que a UI antiga (e conversas antigas do bot) usavam.
_MOTIVOS_LEGADOS = {
    'vencido': 'validade',
    'estragado': 'estragou',
    'queimado': 'queimou',
}


def normalizar_motivo(motivo, default='validade'):
    """Motivo canônico (sempre um de DESPERDICIO_MOTIVOS)."""
    m = (motivo or '').strip().lower()
    m = _MOTIVOS_LEGADOS.get(m, m)
    return m if m in DESPERDICIO_MOTIVOS else default


def reaproveita_sem_baixa(tipo_item, item_id, motivo):
    """True se este desperdício deve ser registrado SEM baixar o estoque
    (item reaproveitável + motivo reaproveitável). `motivo` já normalizado."""
    if motivo not in DESPERDICIO_MOTIVOS_REAPROVEITAVEIS:
        return False
    if tipo_item == 'receita':
        from app.models import Receita
        obj = db.session.get(Receita, item_id) if item_id else None
    elif tipo_item == 'produto':
        from app.models import Produto
        obj = db.session.get(Produto, item_id) if item_id else None
    else:
        return False
    return bool(obj and getattr(obj, 'reaproveitavel', False))
