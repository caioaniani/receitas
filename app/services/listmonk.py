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


def _req(metodo, caminho, tolerar=(), **kw):
    """Chamada crua. Levanta requests.HTTPError em erro — quem chama decide
    se derruba o fluxo ou só loga (campanha nunca pode quebrar o cron).

    `tolerar` = status HTTP que NÃO são erro pra quem chama (ex.: 409 de
    "já existe" ao criar assinante). Devolve {} nesses casos.
    """
    url = f'{_base()}{caminho}'
    if not url.startswith('https://'):
        # Guard deliberado: o token vai em BasicAuth. Em HTTP puro ele
        # trafega em claro pela internet até o VPS.
        raise ValueError('LISTMONK_URL precisa ser https:// — o token vai '
                         'em BasicAuth e não pode trafegar em claro.')
    kw.setdefault('timeout', _TIMEOUT)
    r = requests.request(metodo, url, auth=_auth(), **kw)
    if r.status_code in tolerar:
        return {}
    r.raise_for_status()
    return r.json() if r.text else {}


# ── Listas ───────────────────────────────────────────────────────────

def listas_detalhe():
    """{nome: {'id', 'n'}} — o próprio /api/lists já devolve a contagem, então
    o painel sai numa requisição só (nada de um `contar` por lista)."""
    dados = _req('GET', '/api/lists', params={'per_page': 100})
    return {x['name']: {'id': x['id'], 'n': x.get('subscriber_count') or 0}
            for x in (dados.get('data') or {}).get('results', [])}


def listas():
    """{nome: id} das listas existentes."""
    return {n: v['id'] for n, v in listas_detalhe().items()}


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
    """{e-mail: id} de quem clicou em "cancelar inscrição" nesta lista.

    É o que a sincronização usa pra marcar `Cliente.marketing_descadastro_em`
    — sem refletir de volta, a próxima sincronização re-inscreveria quem
    acabou de sair. O **id** vem junto porque é com ele que se propaga o
    descadastro pras outras listas (`mudar_listas`), sem montar SQL na mão.
    """
    saida = {}
    pagina = 1
    while True:
        dados = _req('GET', '/api/subscribers', params={
            'list_id': lista_id,
            'subscription_status': 'unsubscribed',
            'page': pagina, 'per_page': 500,
        })
        res = (dados.get('data') or {}).get('results') or []
        for s in res:
            email = (s.get('email') or '').strip().lower()
            if email and s.get('id') is not None:
                saida[email] = s['id']
        if len(res) < 500:
            break
        pagina += 1
        if pagina > 40:            # ~20 mil: teto de segurança
            logger.warning('listmonk: descadastrados truncado em 40 páginas')
            break
    return saida


def contar(lista_id):
    """Quantos assinantes a lista tem (qualquer status)."""
    dados = _req('GET', '/api/subscribers',
                 params={'list_id': lista_id, 'per_page': 1})
    return int((dados.get('data') or {}).get('total') or 0)


def mudar_listas(ids, acao, listas_alvo, status='confirmed'):
    """Adiciona/remove/descadastra assinantes (por id) de listas.

    `acao` in {'add', 'remove', 'unsubscribe'}. Por ID e não por SQL de
    propósito: o e-mail vira texto dentro da query do Listmonk e montar SQL
    com dado de fora é convite a acidente.
    """
    if not ids or not listas_alvo:
        return 0
    corpo = {'ids': list(ids), 'action': acao,
             'target_list_ids': list(listas_alvo)}
    if acao == 'add':
        corpo['status'] = status
    _req('PUT', '/api/subscribers/lists', json=corpo)
    return len(ids)


def mudar_listas_por_query(query, acao, listas_alvo, listas_origem=None,
                           status='confirmed'):
    """Mesma coisa, mas selecionando por expressão SQL do Listmonk.

    Usado pra montar a lista de aniversariantes do dia: a consulta roda no
    Postgres do Listmonk (`subscribers.attribs->>'aniv_dia'`), sem trazer
    dezenas de milhares de linhas pela API.

    `listas_origem` limita o universo às nossas listas de origem — assim uma
    query torta nunca alcança assinante que não é nosso.
    """
    corpo = {'query': query, 'action': acao,
             'target_list_ids': list(listas_alvo)}
    if listas_origem:
        corpo['list_ids'] = list(listas_origem)
        corpo['subscription_status'] = 'confirmed'
    if acao == 'add':
        corpo['status'] = status
    _req('PUT', '/api/subscribers/query/lists', json=corpo)


# ── Campanhas ────────────────────────────────────────────────────────

def criar_campanha(nome, assunto, corpo, lista_ids, content_type='richtext',
                   tags=None):
    """Cria a campanha em RASCUNHO e devolve o id. Não envia nada."""
    dados = _req('POST', '/api/campaigns', json={
        'name': nome, 'subject': assunto, 'lists': list(lista_ids),
        'type': 'regular', 'content_type': content_type, 'body': corpo,
        'messenger': 'email', 'tags': tags or ['opao'],
    })
    cid = (dados.get('data') or {}).get('id')
    logger.info('listmonk: campanha %r criada (id %s)', nome, cid)
    return cid


def iniciar_campanha(campanha_id):
    """Coloca a campanha pra rodar (dispara o envio)."""
    _req('PUT', f'/api/campaigns/{campanha_id}/status',
         json={'status': 'running'})
    logger.info('listmonk: campanha %s iniciada', campanha_id)


def garantir_assinante(email, nome, lista_ids):
    """Cria o assinante se ainda não existir (409 = já existe, tudo bem).

    O Listmonk só manda mensagem de teste pra e-mail que JÁ é assinante —
    sem isso o botão "enviar teste" falharia justamente na primeira vez.
    """
    _req('POST', '/api/subscribers', tolerar=(409, 400, 422), json={
        'email': email, 'name': nome or email.split('@')[0],
        'status': 'enabled', 'lists': list(lista_ids),
        'preconfirm_subscriptions': True})


def enviar_teste(campanha_id, corpo_campanha, emails):
    """Manda a peça pros e-mails informados SEM disparar a campanha.

    O endpoint de teste do Listmonk quer os mesmos campos da criação, então
    o corpo da campanha é reenviado junto.
    """
    payload = dict(corpo_campanha)
    payload['subscribers'] = list(emails)
    _req('POST', f'/api/campaigns/{campanha_id}/test', json=payload)
    logger.info('listmonk: teste da campanha %s enviado para %s',
                campanha_id, ', '.join(emails))
