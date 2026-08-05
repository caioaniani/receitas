"""Cliente da API do Listmonk — e-mail marketing (05/08/2026).

O Listmonk roda no VPS da Vultr (o mesmo da ponte RADIUS e do Uptime Kuma),
atrás de HTTPS: https://mkt.opaopadariaartesanal.com.br. O envio sai pelo
stream BROADCAST do Postmark, separado do transacional de propósito —
reclamação de spam numa campanha não pode derrubar a entrega dos e-mails de
pedido/magic link.

Config (Railway): LISTMONK_URL, LISTMONK_API_USER, LISTMONK_API_TOKEN.
Sem elas o módulo fica DORMENTE (`disponivel()` False) e nada quebra — é o
mesmo padrão do Dropbox/Postmark.

NUNCA usar HTTP puro aqui: o token vai em BasicAuth e viajaria em claro.
"""
import csv
import io
import json
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_TIMEOUT = 25
# Import em lotes: o Listmonk aguenta o CSV inteiro, mas fatiar dá
# progresso no log e evita um POST gigante travando o worker.
LOTE = 500


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('LISTMONK_URL') or '').strip()
                and (cfg.get('LISTMONK_API_TOKEN') or '').strip())


def _base():
    return (current_app.config.get('LISTMONK_URL') or '').rstrip('/')


def _auth():
    cfg = current_app.config
    return ((cfg.get('LISTMONK_API_USER') or 'api_padaria').strip(),
            (cfg.get('LISTMONK_API_TOKEN') or '').strip())


def _req(metodo, caminho, **kw):
    """Chamada crua. Levanta requests.HTTPError em erro — quem chama decide
    se derruba o fluxo ou só loga (campanha nunca pode quebrar o cron)."""
    url = f'{_base()}{caminho}'
    if not url.startswith('https://'):
        # Guard deliberado: o token vai em BasicAuth. Em HTTP puro ele
        # trafega em claro pela internet até o VPS.
        raise ValueError('LISTMONK_URL precisa ser https:// — o token vai '
                         'em BasicAuth e não pode trafegar em claro.')
    kw.setdefault('timeout', _TIMEOUT)
    r = requests.request(metodo, url, auth=_auth(), **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


# ── Listas ───────────────────────────────────────────────────────────

def listas():
    """{nome: id} das listas existentes."""
    dados = _req('GET', '/api/lists', params={'per_page': 100})
    return {x['name']: x['id'] for x in (dados.get('data') or {}).get('results', [])}


def garantir_lista(nome, descricao=''):
    """Cria a lista se não existir. Devolve o id. Idempotente pelo NOME.

    `type=private` e `optin=single`: a lista não é pública (ninguém se
    inscreve sozinho pelo Listmonk) e não mandamos e-mail de confirmação —
    quem entra vem da nossa base, que já tem o consentimento registrado.
    """
    atuais = listas()
    if nome in atuais:
        return atuais[nome]
    dados = _req('POST', '/api/lists', json={
        'name': nome, 'type': 'private', 'optin': 'single',
        'description': descricao, 'tags': ['opao'],
    })
    novo = (dados.get('data') or {}).get('id')
    logger.info('listmonk: lista %r criada (id %s)', nome, novo)
    return novo


# ── Assinantes ───────────────────────────────────────────────────────

def _csv_de(contatos):
    """CSV no formato que o import do Listmonk espera.

    `attributes` é JSON por linha — é onde vão dia/mês de aniversário, o que
    permite a campanha segmentar por consulta SQL depois.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['email', 'name', 'attributes'])
    for c in contatos:
        w.writerow([c['email'], c.get('nome') or '',
                    c.get('attribs_json') or '{}'])
    return buf.getvalue()


def importar(lista_ids, contatos, sobrescrever=False):
    """Importa/atualiza assinantes em lote (upsert por e-mail).

    `sobrescrever=False` de propósito: NÃO reescreve nome/atributos de quem
    já existe e, principalmente, **não re-inscreve quem descadastrou** — o
    Listmonk respeita o status de quem saiu. Re-inscrever quem clicou em
    "cancelar" é o caminho mais curto pra virar spam.
    """
    if not contatos:
        return {'importados': 0, 'lotes': 0}
    total = 0
    lotes = 0
    for i in range(0, len(contatos), LOTE):
        fatia = contatos[i:i + LOTE]
        params = {
            'mode': 'subscribe',
            'subscription_status': 'confirmed',
            'delim': ',',
            'lists': list(lista_ids),
            'overwrite': bool(sobrescrever),
        }
        _req('POST', '/api/import/subscribers',
             data={'params': json.dumps(params)},
             files={'file': ('contatos.csv', _csv_de(fatia), 'text/csv')})
        total += len(fatia)
        lotes += 1
        logger.info('listmonk: importados %d/%d', total, len(contatos))
    return {'importados': total, 'lotes': lotes}


def descadastrados(lista_id):
    """E-mails que clicaram em "cancelar inscrição" nesta lista.

    É o que a sincronização usa pra marcar `Cliente.marketing_descadastro_em`
    — sem refletir de volta, a próxima sincronização re-inscreveria quem
    acabou de sair.
    """
    saida = set()
    pagina = 1
    while True:
        dados = _req('GET', '/api/subscribers', params={
            'list_id': lista_id,
            'subscription_status': 'unsubscribed',
            'page': pagina, 'per_page': 500,
        })
        res = (dados.get('data') or {}).get('results') or []
        saida.update((s.get('email') or '').strip().lower() for s in res)
        if len(res) < 500:
            break
        pagina += 1
        if pagina > 40:            # ~20 mil: teto de segurança
            logger.warning('listmonk: descadastrados truncado em 40 páginas')
            break
    return {e for e in saida if e}
