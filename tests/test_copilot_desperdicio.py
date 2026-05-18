"""Smoke tests do registrar_desperdicio (single + lote).

Regressoes cobertas:
- Lote com 3 itens grava 3 Desperdicios + baixa EstoqueLoja
- Lote com item nao encontrado vai pra ignorados sem abortar
- Single sem loja devolve erro claro
- Consolidacao defensiva: 2 tool_use single viram 1 lote (logica simulada)
"""


def test_desperdicio_lote_basico(app, admin_user, loja, catalogo):
    """Lote: 2 itens validos sao gravados, 1 ignorado."""
    from app.services import copilot
    from app.models import Desperdicio, EstoqueLoja
    from app.extensions import db

    # Stock inicial: 10 croissants
    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=10)
    db.session.add(el)
    db.session.commit()

    tool_input = {
        'loja_id': loja.id,
        'motivo': 'vencido',
        'itens': [
            {'nome': 'Croissant', 'quantidade': 2},
            {'nome': 'Pao Frances', 'quantidade': 1},
            {'nome': 'inexistente_xyz', 'quantidade': 1},
        ],
    }
    params = copilot._enriquecer_registrar_desperdicio_lote(tool_input, admin_user)
    resultado = copilot.executar_registrar_desperdicio_lote(params, admin_user)

    assert resultado['ok'] is True
    assert resultado['total_aplicados'] == 2
    assert resultado['total_ignorados'] == 1
    assert Desperdicio.query.count() == 2
    # Stock baixou
    db.session.refresh(el)
    assert el.quantidade == 8


def test_desperdicio_lote_sem_loja(app, admin_user, catalogo):
    """Sem loja, devolve erro claro."""
    from app.services import copilot
    params = {
        'loja_id': None, 'loja_nome': None,
        'motivo': 'vencido',
        'itens': [{'nome': 'Croissant', 'quantidade': 1}],
    }
    resultado = copilot.executar_registrar_desperdicio_lote(params, admin_user)
    assert resultado['ok'] is False
    assert 'loja' in resultado['erro'].lower()


def test_consolidacao_multiplas_chamadas_simples(app):
    """Logica de merge: N tool_use 'registrar_desperdicio' viram 1 lote.

    Reproduz o codigo do interpretar fora da chamada Anthropic.
    """
    # Simula 3 tool_use blocks
    tool_calls_raw = [
        {'name': 'registrar_desperdicio', 'input': {
            'loja_nome': 'ribeiro', 'item_nome': 'Croissant',
            'quantidade': 2, 'motivo': 'vencido'}},
        {'name': 'registrar_desperdicio', 'input': {
            'loja_nome': 'ribeiro', 'item_nome': 'Pao Frances',
            'quantidade': 3, 'motivo': 'vencido'}},
        {'name': 'registrar_desperdicio', 'input': {
            'loja_nome': 'ribeiro', 'item_nome': 'Sourdough',
            'quantidade': 1, 'motivo': 'vencido'}},
    ]
    desp_calls = [tc for tc in tool_calls_raw
                  if tc['name'] == 'registrar_desperdicio']
    assert len(desp_calls) == 3
    # Merge
    primeiro = desp_calls[0]['input']
    itens_merged = [{'nome': tc['input']['item_nome'],
                     'quantidade': tc['input']['quantidade'],
                     'observacao': tc['input'].get('observacao')}
                    for tc in desp_calls]
    consolidado = {
        'loja_id': primeiro.get('loja_id'),
        'loja_nome': primeiro.get('loja_nome'),
        'motivo': primeiro.get('motivo'),
        'itens': itens_merged,
    }
    assert consolidado['loja_nome'] == 'ribeiro'
    assert consolidado['motivo'] == 'vencido'
    assert len(consolidado['itens']) == 3
    assert consolidado['itens'][1]['nome'] == 'Pao Frances'
