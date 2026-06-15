"""Testa o extrator de NF/boleto por IA (mock do cliente Anthropic).

Atualizado 14/06/2026: estrategia mudou pra Opus 4.8 DIRETO — sem cascata
Sonnet -> Opus. Decisao do dono. Os testes refletem a nova realidade.
"""
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


def test_modelo_default_e_opus_4_8():
    from app.services import conta_pagar_ia
    assert conta_pagar_ia.MODELO == 'claude-opus-4-8', \
        f'modelo mudou: {conta_pagar_ia.MODELO}'


def test_extrai_nota_fiscal_direto_no_opus(monkeypatch):
    """Sem cascata: 1 chamada so, Opus 4.8."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    payload = {'tipo_documento': 'nota_fiscal', 'fornecedor': 'Moinho X',
               'valor_total': 1234.50, 'nf_numero': '999',
               'itens': [{'nome': 'Farinha', 'quantidade': 10}]}
    client, chamadas = _fake_client({conta_pagar_ia.MODELO: payload})

    with patch('anthropic.Anthropic', return_value=client):
        out = conta_pagar_ia.extrair_documento(b'fakejpeg', 'image/jpeg')

    assert out['tipo_documento'] == 'nota_fiscal'
    assert out['fornecedor'] == 'Moinho X'
    assert out['valor_total'] == 1234.50
    assert out['modelo_usado'] == conta_pagar_ia.MODELO
    assert chamadas == [conta_pagar_ia.MODELO]


def test_extrai_boleto_direto_no_opus(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    payload = {'tipo_documento': 'boleto', 'fornecedor': 'Energia SA',
               'valor_total': 500.0, 'codigo_barras': '12345678901234',
               'vencimento': '2026-06-10'}
    client, chamadas = _fake_client({conta_pagar_ia.MODELO: payload})

    with patch('anthropic.Anthropic', return_value=client):
        out = conta_pagar_ia.extrair_documento(b'fakejpeg', 'image/jpeg')

    assert out['modelo_usado'] == conta_pagar_ia.MODELO
    assert out['codigo_barras'] == '12345678901234'
    assert len(chamadas) == 1


def test_NAO_ha_mais_cascata_sonnet_opus():
    """Trava: a funcao SONNET, OPUS, e _faltou_campo_critico nao podem
    voltar — eram dead code da estrategia antiga."""
    from app.services import conta_pagar_ia
    assert not hasattr(conta_pagar_ia, 'SONNET')
    assert not hasattr(conta_pagar_ia, 'OPUS')
    assert not hasattr(conta_pagar_ia, '_faltou_campo_critico')


def test_pdf_usa_document_block(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    from app.services import conta_pagar_ia

    payload = {'tipo_documento': 'boleto', 'fornecedor': 'Y',
               'valor_total': 99.0, 'codigo_barras': '1'}
    client, _ = _fake_client({conta_pagar_ia.MODELO: payload})
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
