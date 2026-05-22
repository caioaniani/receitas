"""Helpers de baixa em EstoqueLoja com prioridade por estado.

Quando uma loja vende um produto via Seru/VNDA, o sistema baixa do estoque
seguindo a ordem de prioridade dos estados (ver `app/constants.py`):

  assado → backup → NULL (padrao)

Motivo: o que esta pronto pra vitrine (assado) sai primeiro. Quando esgota
e ainda tem demanda, baixa do backup (loja assou pra repor — sem registro
da transicao, simplificacao operacional). Por ultimo o estado padrao (cru,
raro pra vitrine direta).

Esse helper eh reusado em `seru_sync._baixar_item` e `vnda_sync._baixar_componente`
pra evitar divergencia de logica.
"""
from app.extensions import db
from app.models import EstoqueLoja, MovEstoqueLoja

# Ordem de baixa: assado primeiro (vitrine pronta), backup depois, NULL por ultimo.
_PRIORIDADE_BAIXA = ('assado', 'backup', None)


def baixar_loja_por_prioridade(filtro_base, inteiros, *,
                                tipo_mov, referencia, sem_estoque_tipo,
                                usuario_id):
    """Baixa `inteiros` unidades de EstoqueLoja respeitando prioridade de estado.

    Args:
      filtro_base: dict com `loja_id` + (`receita_id` OU `produto_id` OU
        `materia_prima_id`). NAO deve incluir `estado` — o helper itera nele.
      inteiros: quantidade total a baixar (>= 1).
      tipo_mov: tipo do `MovEstoqueLoja` quando baixa OK (ex: 'venda_seru').
      referencia: string que descreve a venda (ex: 'Seru #123').
      sem_estoque_tipo: tipo do `MovEstoqueLoja` quando falta (ex:
        'venda_seru_sem_estoque'). Registrado na linha com estado NULL
        (default) — onde o saldo esperado deveria ter ficado.
      usuario_id: pra audit.

    Retorna {baixado: int, faltou: int}.
    """
    if inteiros <= 0:
        return {'baixado': 0, 'faltou': 0}

    baixado_total = 0
    restante = inteiros

    for estado in _PRIORIDADE_BAIXA:
        if restante <= 0:
            break
        # SQLAlchemy: filter_by trata `estado=None` corretamente como `IS NULL`.
        el = EstoqueLoja.query.filter_by(**filtro_base, estado=estado).first()
        if not el or (el.quantidade or 0) <= 0:
            continue
        atual = el.quantidade or 0
        baixa = min(restante, atual)
        el.quantidade = atual - baixa
        from app.constants import estado_label
        tag = estado_label(estado)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=tipo_mov, quantidade=baixa,
            referencia=(f'{referencia} {tag}'.rstrip()),
            usuario_id=usuario_id,
        ))
        baixado_total += baixa
        restante -= baixa

    if restante > 0:
        # Saldo negativo registra na linha com estado NULL (cria se nao existir).
        el = EstoqueLoja.query.filter_by(**filtro_base, estado=None).first()
        if not el:
            el = EstoqueLoja(**filtro_base, estado=None, quantidade=0)
            db.session.add(el)
            db.session.flush()
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=sem_estoque_tipo,
            quantidade=restante,
            referencia=f'{referencia} — sem estoque suficiente',
            usuario_id=usuario_id,
        ))

    return {'baixado': baixado_total, 'faltou': restante}
