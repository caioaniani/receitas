"""Testes pra _preview_editar_pedido (Block Kit do Slack).

Assertam o texto visivel pro usuario via JSON dump dos blocks. Foco em
regressoes silenciosas que o usuario so notaria vendo o preview errado.
"""
import json


def _flat(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def _params(**overrides):
    base = {
        'pedido_id': 42,
        'pedido_atual': {
            'loja_nome': 'Ribeiro do Vale',
            'data_entrega': '2026-05-25',
            'observacao': None,
            'status': 'pendente',
            'itens': [
                {'nome': 'Croissant', 'quantidade': 10, 'estado': None, 'observacao': ''},
            ],
        },
        'data_entrega': None,
        'observacao': None,
        'itens': None,
    }
    base.update(overrides)
    return base


def test_preview_header_mostra_pedido_id(app):
    from app.services.slack_blocks import _preview_editar_pedido
    blocks = _preview_editar_pedido(_params(pedido_id=42), token='TOKEN123')
    assert 'Editar pedido #42' in _flat(blocks)


def test_preview_item_backup_mostra_tag(app):
    from app.services.slack_blocks import _preview_editar_pedido
    p = _params(itens=[
        {'resolvido': {'nome': 'Croissant'}, 'nome_original': 'Croissant',
         'quantidade': 5, 'estado': 'backup', 'observacao': ''},
    ])
    assert '[BACKUP]' in _flat(_preview_editar_pedido(p, token='TOKEN123'))


def test_preview_data_igual_nao_mostra_seta(app):
    from app.services.slack_blocks import _preview_editar_pedido
    p = _params(data_entrega='2026-05-25')  # igual ao atual
    flat = _flat(_preview_editar_pedido(p, token='TOKEN123'))
    # `→` so deve aparecer em diff de campo. Itens sem alteracao nao tem seta.
    assert '→' not in flat


def test_preview_data_muda_mostra_seta(app):
    from app.services.slack_blocks import _preview_editar_pedido
    p = _params(data_entrega='2026-05-30')
    flat = _flat(_preview_editar_pedido(p, token='TOKEN123'))
    assert '2026-05-25 → *2026-05-30*' in flat


def test_preview_itens_none_diz_sem_alteracao(app):
    from app.services.slack_blocks import _preview_editar_pedido
    blocks = _preview_editar_pedido(_params(itens=None), token='TOKEN123')
    flat = _flat(blocks)
    assert 'Itens (sem alteracao)' in flat
    assert '10x Croissant' in flat


def test_preview_itens_lista_diz_novos(app):
    from app.services.slack_blocks import _preview_editar_pedido
    p = _params(itens=[
        {'resolvido': {'nome': 'Pao Frances'}, 'nome_original': 'pao',
         'quantidade': 20, 'estado': None, 'observacao': ''},
    ])
    flat = _flat(_preview_editar_pedido(p, token='TOKEN123'))
    assert 'Itens NOVOS' in flat
    assert '20x Pao Frances' in flat
    # Itens atuais NAO devem aparecer quando ha substituicao.
    assert '10x Croissant' not in flat


def test_preview_obs_vazia_nao_gera_diff_fake(app):
    from app.services.slack_blocks import _preview_editar_pedido
    p = _params(observacao='')  # atual ja eh None -> '—'; novo '' -> '—'
    flat = _flat(_preview_editar_pedido(p, token='TOKEN123'))
    assert '→' not in flat
