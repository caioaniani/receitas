"""Extracao de dados de NF/boleto por IA (Claude vision).

Baseado em `ocr_nota.py`, mas pra contas a pagar: classifica o documento
(nota fiscal ou boleto) e extrai fornecedor, valor, vencimento, codigo de
barras / linha digitavel, itens.

Estrategia de custo: tenta Sonnet primeiro (barato); se faltar campo critico
(valor_total, fornecedor, ou — pra boleto — codigo_barras/linha_digitavel),
reprocessa com Opus. Modelos configuraveis por env var (caso o id do Opus
mude).

Aceita imagem (image/*) e PDF (application/pdf — boleto as vezes eh PDF).
NAO grava nada — so extrai. Quem chamar decide o que fazer.
"""
import base64
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

SONNET = os.environ.get('OCR_MODELO_SONNET', 'claude-sonnet-4-6')
OPUS = os.environ.get('OCR_MODELO_OPUS', 'claude-opus-4-7')

SYSTEM_PROMPT = (
    "Voce extrai dados de documentos de compra (nota fiscal ou boleto) a "
    "partir de uma foto/arquivo. Devolve APENAS um JSON valido (sem markdown, "
    "sem explicacao) no formato:\n"
    "{\n"
    '  "tipo_documento": "nota_fiscal" | "boleto" | "desconhecido",\n'
    '  "fornecedor": str (nome do emitente/cedente, se identificar),\n'
    '  "valor_total": float (valor a pagar, sem "R$"),\n'
    '  "vencimento": str (YYYY-MM-DD, se houver),\n'
    '  "nf_numero": str (numero da NF, se houver),\n'
    '  "codigo_barras": str (so digitos do codigo de barras do boleto),\n'
    '  "linha_digitavel": str (linha digitavel do boleto, se houver),\n'
    '  "info_pagamento": str (pix/instrucoes, se houver),\n'
    '  "itens": [{"nome": str, "quantidade": float, "valor_unitario": float, '
    '"valor_total": float}]\n'
    "}\n"
    "Regras: numeros sem 'R$' nem unidade. Omite chaves que nao conseguir ler. "
    "Boleto: priorize valor, vencimento e codigo de barras/linha digitavel. "
    "Nota fiscal: priorize fornecedor, numero, valor total e itens. "
    "Se nao for documento de compra, retorna {\"erro\": \"nao_reconhecido\"}."
)

# Campos sem os quais a extracao eh considerada incompleta (dispara Opus).
def _faltou_campo_critico(dados):
    if dados.get('erro'):
        return True
    if not dados.get('valor_total'):
        return True
    if not dados.get('fornecedor'):
        return True
    if dados.get('tipo_documento') == 'boleto' and not (
            dados.get('codigo_barras') or dados.get('linha_digitavel')):
        return True
    return False


def _content_block(file_bytes, mimetype):
    """Monta o content block de imagem ou documento (PDF) pra Anthropic."""
    b64 = base64.b64encode(file_bytes).decode('ascii')
    mt = (mimetype or '').lower()
    if mt == 'application/pdf':
        return {'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf',
                           'data': b64}}
    if not mt.startswith('image/'):
        mt = 'image/jpeg'  # melhor esforco
    return {'type': 'image',
            'source': {'type': 'base64', 'media_type': mt, 'data': b64}}


def _chamar(client, modelo, bloco):
    response = client.messages.create(
        model=modelo,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': [
            bloco,
            {'type': 'text', 'text': 'Extrai os dados deste documento.'},
        ]}],
    )
    texto = ''.join(b.text for b in response.content if b.type == 'text').strip()
    texto = re.sub(r'^```(?:json)?\s*|\s*```$', '', texto, flags=re.MULTILINE).strip()
    return json.loads(texto)


def extrair_documento(file_bytes, mimetype='image/jpeg'):
    """Extrai dados de uma NF/boleto. Retorna dict com os campos +
    `modelo_usado`, ou `{'erro': ...}`.

    Sonnet primeiro; Opus no fallback se faltar campo critico.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'erro': 'ANTHROPIC_API_KEY nao configurada'}
    try:
        import anthropic
    except ImportError:
        return {'erro': 'biblioteca anthropic nao instalada'}
    if not file_bytes:
        return {'erro': 'arquivo vazio'}

    client = anthropic.Anthropic(api_key=api_key)
    bloco = _content_block(file_bytes, mimetype)

    # 1a tentativa: Sonnet
    try:
        dados = _chamar(client, SONNET, bloco)
        dados['modelo_usado'] = SONNET
    except json.JSONDecodeError:
        dados = {'erro': 'json_invalido'}
    except Exception as exc:  # noqa: BLE001
        logger.warning('conta_pagar_ia Sonnet falhou: %s', exc)
        dados = {'erro': f'sonnet: {exc}'}

    # Fallback Opus se incompleto
    if _faltou_campo_critico(dados):
        try:
            dados_opus = _chamar(client, OPUS, bloco)
            dados_opus['modelo_usado'] = OPUS
            return dados_opus
        except Exception as exc:  # noqa: BLE001
            logger.warning('conta_pagar_ia Opus falhou: %s', exc)
            # devolve o que o Sonnet conseguiu (mesmo incompleto) se nao for erro
            if not dados.get('erro'):
                return dados
            return {'erro': f'opus: {exc}', 'raw_sonnet': dados}

    return dados
