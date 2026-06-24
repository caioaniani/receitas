"""VNDA APOSENTADO em 24/06/2026.

Antes esses testes validavam desempacotamento de cesta via API VNDA. Hoje
travam que a funcao publica continua existindo (chamadores nao quebram) mas
nao consulta nada: sempre vazia, sem importar mapping de cesta.
"""
from datetime import date


def test_agregar_vendas_vnda_ignora_cestas(app):
    """Mesmo com mapping de cesta + mock da API VNDA, o resultado deve ser
    vazio: a funcao nao chega mais a olhar mappings nem a chamar a API."""
    from unittest.mock import patch

    from app.services.vendas_manuais import _agregar_vendas_vnda_api
    with patch('app.services.vnda._buscar_pedidos_janela',
               side_effect=AssertionError('VNDA aposentado: API nao deve ser chamada')):
        vendas, aviso = _agregar_vendas_vnda_api(
            date(2026, 4, 1), date(2026, 4, 30))
    assert vendas == {}
    assert 'aposentado' in (aviso or '').lower()
