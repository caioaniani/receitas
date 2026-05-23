"""Testa o extrator de NF/boleto por IA (mock do cliente Anthropic)."""
import json
from unittest.mock import MagicMock, patch


class _FakeBlock:
    def __init__(self, text):
        self.type = 'text'
        self.text = text


def _fake_response(payload):
    r = MagicMock()
    r.content = [_FakeBlock(json.dumps(payload))]
    return r


def _fake_client(respostas_por_modelo):
    """respostas_por_modelo: dict model_id -> payload dict."""
    client = MagicMock()
    chamadas = []

    def create(model, **kwargs):
        chamadas.append(model)
        return _fake_response(respostas_por_modelo[model])

    client.messages.create.side_effect = create
    return client, chamadas


def test_extrai_nota_fiscal_com_sonnet(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    payload = {'tipo_documento': 'nota_fiscal', 'fornecedor': 'Moinho X',
               'valor_total': 1234.50, 'nf_numero': '999',
               'itens': [{'nome': 'Farinha', 'quantidade': 10}]}
    client, chamadas = _fake_client({conta_pagar_ia.SONNET: payload})

    with patch('anthropic.Anthropic', return_value=client):
        out = conta_pagar_ia.extrair_documento(b'fakejpeg', 'image/jpeg')

    assert out['tipo_documento'] == 'nota_fiscal'
    assert out['fornecedor'] == 'Moinho X'
    assert out['valor_total'] == 1234.50
    assert out['modelo_usado'] == conta_pagar_ia.SONNET
    assert chamadas == [conta_pagar_ia.SONNET]  # nao precisou de Opus


def test_boleto_sem_codigo_barras_cai_no_opus(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    # Sonnet: boleto sem codigo de barras → critico faltando → Opus
    sonnet_payload = {'tipo_documento': 'boleto', 'fornecedor': 'Energia SA',
                      'valor_total': 500.0}
    opus_payload = {'tipo_documento': 'boleto', 'fornecedor': 'Energia SA',
                    'valor_total': 500.0, 'codigo_barras': '12345678901234',
                    'vencimento': '2026-06-10'}
    client, chamadas = _fake_client({
        conta_pagar_ia.SONNET: sonnet_payload,
        conta_pagar_ia.OPUS: opus_payload,
    })

    with patch('anthropic.Anthropic', return_value=client):
        out = conta_pagar_ia.extrair_documento(b'fakejpeg', 'image/jpeg')

    assert out['modelo_usado'] == conta_pagar_ia.OPUS
    assert out['codigo_barras'] == '12345678901234'
    assert chamadas == [conta_pagar_ia.SONNET, conta_pagar_ia.OPUS]


def test_falta_valor_cai_no_opus(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    client, chamadas = _fake_client({
        conta_pagar_ia.SONNET: {'tipo_documento': 'nota_fiscal',
                                'fornecedor': 'X'},  # sem valor_total
        conta_pagar_ia.OPUS: {'tipo_documento': 'nota_fiscal',
                              'fornecedor': 'X', 'valor_total': 10.0},
    })
    with patch('anthropic.Anthropic', return_value=client):
        out = conta_pagar_ia.extrair_documento(b'x', 'image/jpeg')
    assert out['valor_total'] == 10.0
    assert chamadas[-1] == conta_pagar_ia.OPUS


def test_pdf_usa_document_block(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    payload = {'tipo_documento': 'boleto', 'fornecedor': 'Y',
               'valor_total': 99.0, 'codigo_barras': '1'}
    client, _ = _fake_client({conta_pagar_ia.SONNET: payload})
    with patch('anthropic.Anthropic', return_value=client):
        conta_pagar_ia.extrair_documento(b'%PDF-1.4 fake', 'application/pdf')

    # Confere que o content block foi 'document' (PDF), nao 'image'
    _, kwargs = client.messages.create.call_args
    bloco = kwargs['messages'][0]['content'][0]
    assert bloco['type'] == 'document'
    assert bloco['source']['media_type'] == 'application/pdf'


def test_sem_api_key(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    from app.services import conta_pagar_ia
    out = conta_pagar_ia.extrair_documento(b'x', 'image/jpeg')
    assert 'erro' in out
