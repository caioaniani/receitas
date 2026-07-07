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


# Tipos de movimento da CONVERSÃO de sobra pra receita de retorno (par
# saída/entrada na MESMA loja). A exclusão do desperdício desfaz o par.
TIPO_CONVERSAO_SAIDA = 'sobra_retorno'
TIPO_CONVERSAO_SAIDA_SEM_ESTOQUE = 'sobra_retorno_sem_estoque'
TIPO_CONVERSAO_ENTRADA = 'sobra_retorno_entrada'


def converter_sobra_para_retorno(loja_id, receita_id, qtd, usuario_id,
                                 desperdicio_id=None):
    """Converte a sobra reaproveitável NO ESTOQUE DA LOJA: baixa a receita
    original e credita a receita de RETORNO na mesma loja (decisão do dono
    03/07/2026). A sobra deixa de contar como produto fresco vendável já no
    registro — os produtos Nutella são compostos do retorno (a venda baixa
    dali) e a retirada de sobras coleta o retorno.

    Só converte quando `retorno_receita_id` está configurado (reaproveitável
    sem retorno segue o comportamento antigo: registro sem movimento).
    A ENTRADA no retorno é pela quantidade INTEIRA declarada (a sobra física
    existe); a SAÍDA do original é limitada ao saldo — a falta vira mov
    `sobra_retorno_sem_estoque` visível (saldo estava subcontado), nunca
    negativa. Mesmo padrão da devolução (devolucao.py). Todos os movimentos
    carregam `desperdicio_id` — a exclusão do desperdício desfaz o par.

    NÃO commita (quem chama controla a transação). Retorna
    {'destino', 'destino_id', 'baixado', 'faltou', 'creditado'} ou None
    quando não há retorno configurado."""
    from app.models import EstoqueLoja, MovEstoqueLoja, Receita
    from app.services.estoque_helpers import serializar_baixa_estoque
    rec = db.session.get(Receita, receita_id) if receita_id else None
    if rec is None or not rec.retorno_receita_id or qtd <= 0:
        return None
    destino = rec.retorno_receita
    serializar_baixa_estoque()  # antes do UPDATE nas 2 linhas (mesmo deadlock)

    def _linha(rid):
        el = EstoqueLoja.query.filter_by(loja_id=loja_id,
                                         receita_id=rid).first()
        if not el:
            el = EstoqueLoja(loja_id=loja_id, receita_id=rid, quantidade=0)
            db.session.add(el)
            db.session.flush()
        return el

    el_orig = _linha(rec.id)
    saldo = el_orig.quantidade or 0
    baixado = min(qtd, saldo)
    faltou = qtd - baixado
    el_orig.quantidade = saldo - baixado
    if baixado > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el_orig.id, tipo=TIPO_CONVERSAO_SAIDA,
            quantidade=baixado,
            referencia=f'Sobra convertida em {destino.nome}',
            usuario_id=usuario_id, desperdicio_id=desperdicio_id))
    if faltou > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el_orig.id,
            tipo=TIPO_CONVERSAO_SAIDA_SEM_ESTOQUE, quantidade=faltou,
            referencia=(f'Sobra convertida em {destino.nome} — saldo '
                        f'subcontado ({faltou})'),
            usuario_id=usuario_id, desperdicio_id=desperdicio_id))

    el_ret = _linha(destino.id)
    el_ret.quantidade = (el_ret.quantidade or 0) + qtd
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el_ret.id, tipo=TIPO_CONVERSAO_ENTRADA,
        quantidade=qtd,
        referencia=f'Sobra de {rec.nome} convertida',
        usuario_id=usuario_id, desperdicio_id=desperdicio_id))

    return {'destino': destino.nome, 'destino_id': destino.id,
            'baixado': baixado, 'faltou': faltou, 'creditado': qtd}
