"""Extracao de dados de NF/boleto por IA (Claude vision).

Baseado em `ocr_nota.py`, mas pra contas a pagar: classifica o documento
(nota fiscal ou boleto) e extrai fornecedor, valor, vencimento, codigo de
barras / linha digitavel, itens.

Modelo: Opus 4.8 direto (decisao do dono 14/06/2026 — vale o custo extra
pra reduzir os fallbacks e os campos faltantes que o humano tinha que
preencher na mao). Modelo configuravel por env var (caso o id do Opus
mude). Atras de erro de json/transient, o codigo tenta UMA vez com o
mesmo modelo — sem cascata Sonnet->Opus.

Aceita imagem (image/*) e PDF (application/pdf — boleto as vezes eh PDF).
NAO grava nada — so extrai. Quem chamar decide o que fazer.
"""
import base64
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

MODELO = os.environ.get('OCR_MODELO_OPUS', 'claude-opus-4-8')

SYSTEM_PROMPT = (
    "Voce extrai dados de documentos de compra (nota fiscal ou boleto) a "
    "partir de uma foto/arquivo. Devolve APENAS um JSON valido (sem markdown, "
    "sem explicacao) no formato:\n"
    "{\n"
    '  "tipo_documento": "nota_fiscal" | "boleto" | "desconhecido",\n'
    '  "fornecedor": str (nome do emitente/cedente, se identificar),\n'
    '  "valor_total": float (valor a pagar, sem "R$"),\n'
    '  "vencimento": str (YYYY-MM-DD, se houver),\n'
    '  "vencimento_texto": str (a data de vencimento EXATAMENTE como aparece '
    'no documento, ex "08/05/2026"),\n'
    '  "nf_numero": str (numero da NF; no boleto, o "No documento" que casa com a NF),\n'
    '  "codigo_barras": str (so digitos do codigo de barras do boleto),\n'
    '  "linha_digitavel": str (linha digitavel do boleto, se houver),\n'
    '  "info_pagamento": str (pix/instrucoes, se houver),\n'
    '  "itens": [{"nome": str (SO o nome do produto/marca, SEM validade/lote/data), "quantidade": float, "valor_unitario": float, '
    '"valor_total": float, "unidade": str (unidade de compra como aparece: '
    'un/kg/g/ml/cx/fardo), "unidade_base_sugerida": "un"|"kg"|"g"|"ml", '
    '"fator_embalagem": float (quantas unidades-base ha em 1 unidade de '
    'compra, SE a descricao indicar)}]\n'
    "}\n"
    "Regras: numeros sem 'R$' nem unidade. Omite chaves que nao conseguir ler. "
    "Nos itens, se a descricao trouxer a contagem da embalagem (ex '300UN', "
    "'C/300', '12X1L', 'SC 25KG'), preencha 'fator_embalagem' com esse numero "
    "e 'unidade_base_sugerida' com a unidade do conteudo; senao OMITA ambos. "
    "Esses dois campos sao apenas sugestao (o humano confirma). "
    "No 'nome' do item traga APENAS o produto (marca/tipo) — NUNCA inclua "
    "validade ('VAL ...'), lote ('LOTE ...'), datas nem codigos (mudam a cada "
    "compra). Ex: 'Farinha France Bagatelle T45', nao 'FARINHA ... T45 VAL "
    "30/09/2026 LOTE GXB12603'. "
    "DATAS: os documentos sao BRASILEIROS — toda data esta em DD/MM/AAAA (dia "
    "primeiro, depois mes). Ex: '08/05/2026' = dia 8 de maio = 2026-05-08. "
    "NUNCA inverta dia e mes. Em 'vencimento' devolva ISO ja convertido "
    "corretamente; em 'vencimento_texto' copie a data crua do documento. "
    "Boleto: priorize valor, vencimento e codigo de barras/linha digitavel. "
    "No boleto, o campo 'No documento'/'Numero do documento' costuma ser o "
    "numero da nota fiscal que ele cobra — extraia em 'nf_numero' pra casar o "
    "boleto com a NF. "
    "NUMEROS: transcreva o numero da NF/documento digito a digito, EXATAMENTE "
    "como impresso (confira o ULTIMO digito); ignore ponto de milhar; NUNCA "
    "arredonde nem complete. Ex: 'No 454.898' -> '454898'. "
    "Nota fiscal: priorize fornecedor, numero, valor total e itens. "
    "Se nao for documento de compra, retorna {\"erro\": \"nao_reconhecido\"}."
)

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

    Opus 4.8 direto — sem cascata. Decisao do dono 14/06/2026.
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
    try:
        dados = _chamar(client, MODELO, bloco)
        dados['modelo_usado'] = MODELO
        return dados
    except json.JSONDecodeError:
        return {'erro': 'json_invalido'}
    except Exception as exc:  # noqa: BLE001
        logger.warning('conta_pagar_ia falhou: %s', exc)
        return {'erro': f'opus: {exc}'}
