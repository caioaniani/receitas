"""OCR de nota fiscal / cupom — usa Anthropic Vision (Claude Sonnet 4.6).

Recebe imagem (bytes + mimetype), pede pro modelo extrair itens em JSON
estruturado: [{nome, quantidade, unidade, preco_unitario, preco_total}].

NAO faz match com cadastro nem grava nada — so extrai. Quem chamar (rota
no blueprint) que decide o que fazer com o resultado.
"""
import base64
import json
import os
import re

SYSTEM_PROMPT = (
    "Voce e um extrator de notas fiscais e cupons. Recebe foto de uma "
    "nota/cupom e devolve APENAS um JSON valido (sem markdown, sem "
    "explicacao) no formato: "
    "{\"itens\": [{\"nome\": str, \"quantidade\": float, "
    "\"unidade\": str (ex: kg, un, l, g), \"preco_unitario\": float, "
    "\"preco_total\": float}], \"fornecedor\": str (se identificar), "
    "\"data\": str (YYYY-MM-DD se identificar), \"total\": float}. "
    "Se um campo nao for legivel, omite a chave. Quantidade e precos sao "
    "numeros (sem 'R$' nem 'kg'). Se nao for nota fiscal, retorna "
    "{\"erro\": \"nao_eh_nota_fiscal\"}."
)


def extrair_itens_nota(image_bytes, mimetype='image/jpeg'):
    """Chama Claude Sonnet 4.6 com a imagem. Retorna dict parseado ou
    {'erro': '...'} em falha."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'erro': 'ANTHROPIC_API_KEY nao configurada'}
    try:
        import anthropic
    except ImportError:
        return {'erro': 'biblioteca anthropic nao instalada'}

    b64 = base64.b64encode(image_bytes).decode('ascii')
    client = anthropic.Anthropic(api_key=api_key)
    modelo = 'claude-opus-4-8'
    try:
        response = client.messages.create(
            model=modelo,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image',
                     'source': {'type': 'base64', 'media_type': mimetype, 'data': b64}},
                    {'type': 'text', 'text': 'Extrai os itens dessa nota.'},
                ],
            }],
        )
        from app.services import uso_ia
        uso_ia.registrar('ocr_cupom', modelo, getattr(response, 'usage', None))
    except Exception as exc:  # noqa: BLE001
        return {'erro': f'Anthropic falhou: {exc}'}

    texto = ''.join(b.text for b in response.content if b.type == 'text').strip()
    # Modelo as vezes envolve em ```json — tira.
    texto = re.sub(r'^```(?:json)?\s*|\s*```$', '', texto, flags=re.MULTILINE).strip()
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        return {'erro': 'modelo retornou JSON invalido', 'raw': texto[:500]}
    return dados
