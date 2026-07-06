"""Vigia do SITE (loja online) — smoke test recorrente de saúde da venda.

Criado em 05/07/2026, no dia do incidente do frete: a BrasilAPI parou de
devolver coordenadas de alguns CEPs e o geocode caía em rua homônima de
outra cidade — R$ 95 pra vizinho da padaria e o Centro bloqueado como
"fora da área". Ninguém reclamou; o cliente só abandonava o checkout, e o
funil do dia despencou (4 pedidos pagos na véspera → 1 no dia). Pedido do
dono: "um scan diário no site pra ter certeza que tudo está funcionando".

O vigia roda CANÁRIOS nas MESMAS funções que o checkout usa (nada de
navegador/screenshot — é a lógica de verdade):

1. Frete: endereços de referência com faixa esperada de distância. Pega
   tanto o frete absurdo (vizinho a 19 km) quanto o bloqueio indevido
   (dentro da área virando "fora") e o inverso (fora da área passando).
2. Catálogo: o site tem produto ativo com preço; a home responde 200.
3. Agenda: existe data de entrega disponível e a primeira data tem janela.

Alerta o dono no WhatsApp (Z-API) na TRANSIÇÃO saudável→doente, re-alerta o
mesmo problema a cada 6h e avisa uma vez quando normalizar — mesmo padrão
do vigia de infra do Chatwoot (estado em AppConfig, sobrevive a deploy).
Cron a cada 2h em `seru_cron` (kill-switch: SITE_VIGIA=0). Inspeção manual:
GET /admin/vigia-site (owner).
"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)

_REALERTA_MIN = 360      # re-alerta do MESMO problema a cada 6h
_KEY_QUEBRADO = 'site_vigia_quebrado_desde'
_KEY_ULTIMO = 'site_vigia_ultimo_alerta_em'
_KEY_ASSIN = 'site_vigia_ultima_assinatura'

# Canários de frete: (consulta, fora_area esperado, km_min, km_max).
# Faixas LARGAS de propósito — o vigia acusa geocode em outra cidade/bairro,
# não variação de metros. Casos 2 e 3 são os do incidente de 05/07/2026.
_CANARIOS_FRETE = [
    # A própria padaria (Brooklin): tem que ser pertíssimo.
    ('Rua Ribeiro do Vale, 455, Brooklin, São Paulo, SP, 04568-010',
     False, 0.0, 1.5),
    # Vizinho da padaria — caiu na homônima do Grajaú (19,3 km) no incidente.
    ('Rua Nova York, Brooklin, São Paulo, SP, 04560-000',
     False, 0.0, 5.0),
    # Centro (caso D Lucas) — caiu em Arujá (44 km) no incidente.
    ('01050-000', False, 3.0, 13.0),
    # Campinas: SEMPRE fora da área — pega o defeito inverso (tudo passando).
    ('Rua Barão de Jaguara, 1000, Centro, Campinas, SP, 13015-001',
     True, None, None),
]


def checar_frete():
    """Roda os canários no `consultar_frete` real. Lista de problemas ([]=OK)."""
    from app.services import frete as frete_svc

    problemas = []
    for consulta, fora_esperado, km_min, km_max in _CANARIOS_FRETE:
        r = frete_svc.consultar_frete(consulta)
        rotulo = consulta if len(consulta) <= 40 else consulta[:37] + '…'
        if not r.get('ok'):
            problemas.append(f'frete não resolveu "{rotulo}" '
                             f'({r.get("erro")})')
            continue
        if bool(r.get('fora_area')) != fora_esperado:
            problemas.append(
                f'frete de "{rotulo}": esperado '
                f'{"FORA" if fora_esperado else "DENTRO"} da área, veio o '
                f'contrário ({r.get("distancia_km")} km — '
                f'{(r.get("endereco") or "")[:60]})')
            continue
        if not fora_esperado:
            km = float(r.get('distancia_km') or 0)
            if not (km_min <= km <= km_max):
                problemas.append(
                    f'frete de "{rotulo}": {km} km fora da faixa esperada '
                    f'({km_min}-{km_max} km) — geocode suspeito: '
                    f'{(r.get("endereco") or "")[:60]}')
    return problemas


def checar_catalogo():
    """A vitrine tem item vendável — usa a MESMA fonte da home da loja
    (`produtos_publicados`, que também serializa: se a serialização quebrar,
    o problema aparece aqui). Não bate na rota HTTP de propósito: o gate de
    host da loja devolve 404 fora do domínio público (test_client do cron
    não é opao.online)."""
    from app.services import loja_catalogo

    try:
        itens = loja_catalogo.produtos_publicados()
    except Exception as e:  # noqa: BLE001 — vigia nunca derruba o cron
        return [f'vitrine quebrou ao montar o catálogo: {e}']
    if not itens:
        return ['catálogo do site vazio (nenhum item ativo com preço de '
                'site) — vitrine sem nada pra vender']
    return []


def checar_agenda():
    """Entrega agendada tem data disponível e a 1ª data tem janela."""
    from app.services import loja_checkout as lc

    problemas = []
    datas = lc.datas_disponiveis('agendada')
    if not datas:
        problemas.append('nenhuma data de entrega agendada disponível')
        return problemas
    janelas = lc.janelas_disponiveis('agendada', datas[0])
    if not janelas:
        problemas.append(
            f'primeira data de entrega ({datas[0].isoformat()}) sem '
            'nenhuma janela de horário')
    return problemas


def rodar_checks():
    """Roda todos os checks. {'saudavel': bool, 'problemas': [...]}) —
    read-only, nunca levanta exceção (problema vira item da lista)."""
    problemas = []
    for nome, fn in (('frete', checar_frete),
                     ('catalogo', checar_catalogo),
                     ('agenda', checar_agenda)):
        try:
            problemas.extend(fn())
        except Exception as e:  # noqa: BLE001 — check quebrado é achado, não crash
            logger.exception('vigia site: check %s explodiu', nome)
            problemas.append(f'check {nome} explodiu: {e}')
    return {'saudavel': not problemas, 'problemas': problemas}


def _carregar():
    from datetime import datetime as _dt

    from app.models import AppConfig

    def _parse(s):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None
    return {'quebrado_desde': _parse(AppConfig.get(_KEY_QUEBRADO)),
            'ultimo_alerta_em': _parse(AppConfig.get(_KEY_ULTIMO)),
            'ultima_assinatura': AppConfig.get(_KEY_ASSIN)}


def _gravar(est):
    from app.extensions import db
    from app.models import AppConfig

    def _fmt(v):
        return v.isoformat() if v else None
    AppConfig.set(_KEY_QUEBRADO, _fmt(est.get('quebrado_desde')))
    AppConfig.set(_KEY_ULTIMO, _fmt(est.get('ultimo_alerta_em')))
    AppConfig.set(_KEY_ASSIN, est.get('ultima_assinatura'))
    db.session.commit()


def vigiar():
    """Roda os checks e alerta o dono no WhatsApp quando o site adoece.

    Anti-spam idêntico ao vigia de infra: alerta na transição, re-alerta o
    mesmo problema a cada 6h, avisa quando normalizar."""
    from app.services import zapi
    from app.utils import agora as _agora

    cfg = current_app.config
    dono = ((cfg.get('CHATWOOT_VIGIA_INFRA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())

    out = rodar_checks()
    est = _carregar()
    agora_dt = _agora()

    if out['saudavel']:
        if est['quebrado_desde'] is not None:
            if dono:
                zapi.enviar_texto(dono, '✅ Site normalizou — frete, '
                                        'catálogo e agenda OK de novo.')
            _gravar({'quebrado_desde': None, 'ultimo_alerta_em': None,
                     'ultima_assinatura': None})
            return {'rodou': True, 'enviado': bool(dono),
                    'tipo': 'recuperacao', **out}
        return {'rodou': True, 'enviado': False, 'tipo': 'saudavel', **out}

    assinatura = ' | '.join(sorted(out['problemas']))[:900]
    mudou = assinatura != est['ultima_assinatura']
    venceu = (est['ultimo_alerta_em'] is None
              or (agora_dt - est['ultimo_alerta_em']).total_seconds()
              >= _REALERTA_MIN * 60)
    if est['quebrado_desde'] is None:
        est['quebrado_desde'] = agora_dt
    if dono and (mudou or venceu):
        linhas = '\n'.join('• ' + p for p in out['problemas'][:8])
        zapi.enviar_texto(dono, ('🚨 Vigia do SITE — problema detectado:\n\n'
                                 f'{linhas}\n\n'
                                 'Clientes podem estar vendo frete errado ou '
                                 'não conseguindo comprar. Detalhe: '
                                 '/admin/vigia-site'))
        est['ultimo_alerta_em'] = agora_dt
        est['ultima_assinatura'] = assinatura
        _gravar(est)
        return {'rodou': True, 'enviado': True, 'tipo': 'alerta', **out}
    _gravar(est)
    return {'rodou': True, 'enviado': False, 'tipo': 'alerta_suprimido', **out}
