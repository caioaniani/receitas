"""Leitura das falhas de envio e resolução após um envio humano confirmado."""

import json
from datetime import date

from app.models import AppConfig, Receita
from app.services.auto_pedidos import STATUS_ENVIO_PLANO_KEY
from app.utils import hoje


def ler_status():
    try:
        valor = json.loads(AppConfig.get(STATUS_ENVIO_PLANO_KEY) or '{}')
    except (TypeError, ValueError):
        return {}
    return valor if isinstance(valor, dict) else {}


def falhas_atuais():
    """GET só lê: falhas antigas ficam no registro, fora do aviso acionável."""
    status = ler_status()
    falhas = status.get('falhas')
    if not isinstance(falhas, list):
        return []
    receitas = Receita.query.with_entities(Receita.id, Receita.nome).all()
    resultado = []
    for falha in falhas:
        if not isinstance(falha, dict) or not isinstance(falha.get('erro'), str):
            continue
        try:
            dia = date.fromisoformat(falha.get('data', ''))
        except (TypeError, ValueError):
            continue
        if dia < hoje():
            continue
        erro = falha['erro']
        candidatas = [rid for rid, nome in receitas
                      if nome and erro.startswith(nome + ':')]
        resultado.append({
            'data': dia.isoformat(), 'label': dia.strftime('%d/%m'),
            'erro': erro,
            'receita_id': candidatas[0] if len(candidatas) == 1 else None,
        })
    return sorted(resultado, key=lambda f: f['data'])


def limpar_falha(data_alvo):
    """Remove somente a data que acabou de ser enviada; chamador commita."""
    status = ler_status()
    falhas = status.get('falhas')
    if not isinstance(falhas, list):
        return False
    iso = data_alvo.isoformat()
    restantes = [f for f in falhas
                 if not isinstance(f, dict) or f.get('data') != iso]
    if len(restantes) == len(falhas):
        return False
    status['falhas'] = restantes
    AppConfig.set(STATUS_ENVIO_PLANO_KEY, json.dumps(status, ensure_ascii=False))
    return True
