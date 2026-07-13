"""Vigia do PDV (Seru) — avisa o dono quando a baixa de venda PARA de
funcionar (pedido do dono 07/07/2026, apos o incidente da Ribeiro do Vale:
renomearam as lojas no Seru em ~20/06 e a loja ficou 2 SEMANAS sem baixar
estoque, em silencio — as vendas caiam na loja errada).

Tres canarios, todos read-only e baratos (rodam apos cada sync de 15min):
1. Sync do Seru atrasado/desligado (>40min sem rodar).
2. Loja MUDA: loja com vinculo confirmado que TINHA baixas de venda no
   historico recente e ficou >=36h sem NENHUMA baixa (vendia e parou =
   vinculo/caixa mudou no Seru, ou a loja parou de vender — ambos merecem
   olhar humano).
3. Company vendendo SEM vinculo confirmado: aparece em VendaSeruDiaria nos
   ultimos 2 dias mas o mapa esta pendente/auto-fuzzy — essas vendas NAO
   baixam estoque ate alguem confirmar em /pdv/mapeamentos.

Alerta no WhatsApp do dono com o MESMO anti-spam dos vigias de infra/site:
transicao saudavel→doente alerta na hora, re-alerta a cada 6h enquanto durar,
avisa "normalizou" na recuperacao. Estado em AppConfig (sobrevive a deploy).
Kill-switch: env PDV_VIGIA=0.
"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)

_KEY_QUEBRADO = 'pdv_vigia_quebrado_desde'
_KEY_ULTIMO = 'pdv_vigia_ultimo_alerta_em'
_KEY_ASSIN = 'pdv_vigia_ultima_assinatura'
_REALERTA_MIN = 360           # re-alerta o mesmo problema a cada 6h
_JANELA_MUDA_H = 36           # loja sem baixa ha 36h = muda

# Check de vazao da API (13/07/2026): piso de pedidos ACUMULADOS do dia
# por hora BRT (hora >= chave -> piso), avaliado so em horario de loja.
_VAZAO_PISOS = ((17, 120), (14, 60), (11, 20), (9, 3))
_VAZAO_HORA_INI, _VAZAO_HORA_FIM = 9, 21
_HIST_DIAS = 14               # "vendia" = teve baixa nos 14 dias anteriores


def _lojas_com_baixa(ini_dt, fim_dt):
    """Set de loja_id com baixa de venda Seru no intervalo [ini, fim)."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    rows = (MovEstoqueLoja.query
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(MovEstoqueLoja.tipo.in_(('venda_seru',
                                             'venda_seru_sem_estoque')),
                    MovEstoqueLoja.data >= ini_dt,
                    MovEstoqueLoja.data < fim_dt)
            .with_entities(EstoqueLoja.loja_id).distinct().all())
    return {r[0] for r in rows if r[0] is not None}


def rodar_checks():
    """{'saudavel': bool, 'problemas': [...]}) — nunca levanta exceção."""
    from datetime import timedelta

    from app.models import SeruLojaMap, VendaSeruDiaria
    from app.services import pdv_saude
    from app.utils import agora, hoje

    problemas = []
    agora_dt = agora()

    # 1. Sync atrasado/desligado.
    try:
        res = pdv_saude.resumo()
        if res.get('seru_atrasado'):
            problemas.append('sync do Seru atrasado/parado (>40min sem rodar) '
                             '— NENHUMA venda esta baixando estoque')
    except Exception as e:  # noqa: BLE001 — check quebrado é achado
        logger.exception('pdv_vigia: resumo explodiu')
        problemas.append(f'check de sync explodiu: {e}')

    # 2. Loja confirmada que vendia e ficou MUDA.
    try:
        corte = agora_dt - timedelta(hours=_JANELA_MUDA_H)
        ativas = _lojas_com_baixa(corte, agora_dt)
        vendiam = _lojas_com_baixa(agora_dt - timedelta(days=_HIST_DIAS),
                                   corte)
        confirmadas = {m.loja_id: m.loja for m in SeruLojaMap.query
                       .filter(SeruLojaMap.confirmado_em.isnot(None),
                               SeruLojaMap.ignorar.is_(False),
                               SeruLojaMap.loja_id.isnot(None)).all()}
        for lid in sorted((vendiam - ativas) & set(confirmadas)):
            loja = confirmadas[lid]
            nome = loja.nome if loja else f'loja {lid}'
            problemas.append(
                f'{nome}: vendia pelo PDV e esta ha {_JANELA_MUDA_H}h sem '
                'NENHUMA baixa de venda — vinculo/caixa pode ter mudado no '
                'Seru (foi assim que a Ribeiro ficou 2 semanas sem baixar)')
    except Exception as e:  # noqa: BLE001
        logger.exception('pdv_vigia: check loja muda explodiu')
        problemas.append(f'check de loja muda explodiu: {e}')

    # 3. Company vendendo sem vinculo confirmado (vendas sem baixa).
    try:
        ini = hoje() - timedelta(days=1)
        vendendo = {r[0] for r in (VendaSeruDiaria.query
                    .filter(VendaSeruDiaria.data >= ini,
                            VendaSeruDiaria.qtd > 0)
                    .with_entities(VendaSeruDiaria.loja_seru)
                    .distinct().all())}
        if vendendo:
            confirmados = {m.seru_company_name for m in SeruLojaMap.query
                           .filter(SeruLojaMap.confirmado_em.isnot(None))
                           .all()}
            ignorados = {m.seru_company_name for m in SeruLojaMap.query
                         .filter(SeruLojaMap.ignorar.is_(True)).all()}
            for nome_c in sorted(vendendo - confirmados - ignorados):
                problemas.append(
                    f'company "{nome_c}" esta VENDENDO sem vinculo '
                    'confirmado — as vendas NAO baixam estoque; confirme '
                    'em /pdv/mapeamentos')
    except Exception as e:  # noqa: BLE001
        logger.exception('pdv_vigia: check company pendente explodiu')
        problemas.append(f'check de company pendente explodiu: {e}')

    # 4. VAZAO na FONTE (13/07/2026, incidente das companies): a API do
    #    Seru respondia mas enxergava 1 pedido no dia — as empresas tinham
    #    saido da "Integracao SERU" no painel do Colibri e NADA subia. Os
    #    checks 1-3 olham o NOSSO lado (baixas/sync) e so pegariam isso
    #    36h depois (loja muda); este pergunta A API quantos pedidos o dia
    #    tem (limit=1 -> totalPages == total) e compara com piso por hora.
    #    Pisos conservadores (feriado fraco nao alarma; normal ~600/dia).
    #    So avalia em horario de loja; API fora tambem e achado.
    try:
        hora = agora_dt.hour
        if _VAZAO_HORA_INI <= hora < _VAZAO_HORA_FIM:
            from app.services import seru
            resp = seru.listar_pedidos(hoje(), hoje(), page=1, limit=1)
            pedidos_hoje = int((resp or {}).get('totalPages') or 0)
            piso = next((p for h, p in _VAZAO_PISOS if hora >= h), 0)
            if pedidos_hoje < piso:
                problemas.append(
                    f'pedidos de HOJE nao estao chegando na API do Seru: '
                    f'{pedidos_hoje} visivel(is) as '
                    f'{agora_dt.strftime("%H:%M")} (esperado >= {piso}) — '
                    'vendas das lojas nao estao subindo; conferir '
                    'sincronizacao dos PDVs e as empresas do painel '
                    '"Integracao SERU" do Colibri')
    except Exception as e:  # noqa: BLE001 — API fora e achado, nao silencio
        logger.exception('pdv_vigia: check de vazao da API explodiu')
        problemas.append(f'API do Seru fora (check de vazao): {str(e)[:200]}')

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
    """Roda os checks e alerta o dono no WhatsApp quando a baixa adoece.
    Anti-spam identico aos vigias de infra/site."""
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
                zapi.enviar_texto(dono, '✅ PDV normalizou — todas as lojas '
                                        'voltaram a baixar venda.')
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
        zapi.enviar_texto(dono, ('🚨 Vigia do PDV — venda sem baixar '
                                 'estoque:\n\n'
                                 f'{linhas}\n\n'
                                 'Confira /pdv/mapeamentos e /pdv/itens-vendidos.'))
        est['ultimo_alerta_em'] = agora_dt
        est['ultima_assinatura'] = assinatura
        _gravar(est)
        return {'rodou': True, 'enviado': True, 'tipo': 'alerta', **out}
    _gravar(est)
    return {'rodou': True, 'enviado': False, 'tipo': 'alerta_suprimido', **out}
