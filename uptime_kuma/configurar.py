#!/usr/bin/env python3
"""Cria os monitores e o alerta de WhatsApp do Uptime Kuma por comando.

O Uptime Kuma 1.x não tem API REST oficial — a `uptime-kuma-api` fala o
socket.io dele. Este script é IDEMPOTENTE: só cria o que ainda não existe
(compara pelo nome), então rodar de novo é seguro.

Uso (no VPS, dentro de um container Python descartável — não instala nada
no sistema):

    docker run --rm --network host \\
      -e KUMA_USER=admin -e KUMA_PASS='...' \\
      -e ZAPI_INSTANCE_ID='...' -e ZAPI_TOKEN='...' \\
      -e ZAPI_CLIENT_TOKEN='...' -e ZAPI_PHONE='5511999999999' \\
      -v /opt/uptime-kuma/configurar.py:/c.py:ro \\
      python:3.12-slim sh -c "pip install -q uptime-kuma-api && python /c.py"

Sem as variáveis do Z-API ele cria só os monitores (sem notificação).
"""
import os
import sys

from uptime_kuma_api import MonitorType, NotificationType, UptimeKumaApi

KUMA_URL = os.environ.get('KUMA_URL', 'http://127.0.0.1:3001')

# ── O corpo do webhook ────────────────────────────────────────────────
# `{{ msg | json }}` SEM aspas em volta, de propósito: o corpo é renderizado
# por LiquidJS e precisa sair JSON válido. O exemplo que a tela do Uptime
# Kuma sugere usa "{{ msg }}" ENTRE ASPAS e quebra quando a mensagem tem
# aspas ou quebra de linha — que é o formato dos erros de queda
# (`getaddrinfo ENOTFOUND "gestao"`). Resultado: o alerta não sairia
# justamente na hora em que o sistema caiu. O filtro `json` já produz a
# string escapada. Testado com aspas, multilinha, barra invertida e
# mensagem de recuperação.
CORPO_WEBHOOK = """{
  "phone": "%s",
  "message": {{ msg | prepend: "🚨 O Pão — " | json }}
}"""

MONITORES = [
    dict(nome='Sistema — gestão',
         tipo=MonitorType.KEYWORD,
         url='https://gestao.opaopadariaartesanal.com.br/health',
         # Keyword e não HTTP puro: o proxy do Railway pode devolver uma
         # página de erro com status 200. Exigir 'ok' no corpo garante que
         # quem respondeu foi o app.
         keyword='ok', interval=60),
    dict(nome='Loja online (opao.online)',
         tipo=MonitorType.HTTP,
         url='https://opao.online', interval=60),
    dict(nome='Atendimento (Chatwoot)',
         tipo=MonitorType.HTTP,
         url='https://atendimento.opaopadariaartesanal.com.br', interval=120),
    dict(nome='Ponte RADIUS (Wi-Fi das lojas)',
         tipo=MonitorType.PORT,
         hostname='127.0.0.1', port=1812, interval=300),
]


def main():
    usuario = os.environ.get('KUMA_USER', '').strip()
    senha = os.environ.get('KUMA_PASS', '').strip()
    if not usuario or not senha:
        sys.exit('Defina KUMA_USER e KUMA_PASS (o admin que você criou).')

    api = UptimeKumaApi(KUMA_URL)
    try:
        api.login(usuario, senha)
        print(f'Conectado em {KUMA_URL} como {usuario}')

        notif_id = _garantir_notificacao(api)
        _garantir_monitores(api, notif_id)

        print('\nPronto. Confira em', KUMA_URL)
        if notif_id is None:
            print('AVISO: nenhuma notificação configurada — os monitores '
                  'vigiam, mas ninguém é avisado. Rode de novo com as '
                  'variáveis ZAPI_* para ligar o WhatsApp.')
    finally:
        api.disconnect()


def _garantir_notificacao(api):
    """Cria (ou reaproveita) a notificação de WhatsApp. Devolve o id."""
    instancia = os.environ.get('ZAPI_INSTANCE_ID', '').strip()
    token = os.environ.get('ZAPI_TOKEN', '').strip()
    client_token = os.environ.get('ZAPI_CLIENT_TOKEN', '').strip()
    telefone = os.environ.get('ZAPI_PHONE', '').strip()

    nome = 'WhatsApp do dono (Z-API)'
    for n in api.get_notifications():
        if n.get('name') == nome:
            print(f'· notificação "{nome}" já existe (id {n["id"]})')
            return n['id']

    if not all([instancia, token, telefone]):
        print('· ZAPI_INSTANCE_ID/ZAPI_TOKEN/ZAPI_PHONE ausentes — pulando '
              'a notificação')
        return None

    cabecalhos = ('{"Client-Token": "%s"}' % client_token) if client_token else ''
    r = api.add_notification(
        name=nome,
        type=NotificationType.WEBHOOK,
        isDefault=True,        # já vem marcada em monitor novo
        applyExisting=True,    # aplica nos monitores que já existirem
        webhookURL=(f'https://api.z-api.io/instances/{instancia}'
                    f'/token/{token}/send-text'),
        webhookContentType='custom',
        webhookCustomBody=CORPO_WEBHOOK % telefone,
        webhookAdditionalHeaders=cabecalhos,
    )
    print(f'· notificação "{nome}" criada (id {r["id"]})')
    return r['id']


def _garantir_monitores(api, notif_id):
    existentes = {m.get('name') for m in api.get_monitors()}
    ids = [notif_id] if notif_id else []
    for cfg in MONITORES:
        nome = cfg['nome']
        if nome in existentes:
            print(f'· monitor "{nome}" já existe — pulando')
            continue
        campos = dict(
            type=cfg['tipo'], name=nome,
            interval=cfg['interval'],
            # 2 tentativas antes de alertar: um blip de rede não vira
            # alarme falso às 3h da manhã.
            retries=2,
            notificationIDList=ids,
        )
        for k in ('url', 'keyword', 'hostname', 'port'):
            if k in cfg:
                campos[k] = cfg[k]
        api.add_monitor(**campos)
        print(f'· monitor "{nome}" criado')


if __name__ == '__main__':
    main()
