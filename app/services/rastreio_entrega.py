"""Acompanhamento de entrega pelo CLIENTE — por progresso, sem GPS (01/08/2026).

Pedido do dono pro Dia dos Pais (~150 pedidos, janela 06:00-10:00, motoristas
contratados no lugar do Lalamove). Decisões dele (AskUserQuestion,
31/07/2026): acompanhar por **progresso + ETA** (GPS de navegador só funciona
com a página aberta — com motorista avulso, metade dos mapas congelaria) e
aviso por **e-mail automático** quando a rota sai.

As peças que JÁ existiam e este serviço só amarra:
- rota otimizada por motorista: `rotas.gerar_rotas` + `AtribuicaoEntrega`
  (ordem, status, entregue_em — models/entregas.py:167-173);
- página do motorista (/driver/<token>) onde ele marca cada entrega;
- página pública do pedido por código (`loja.pedido_confirmado`) e o e-mail
  `email.enviar_pedido_a_caminho` (existia sem chamador).

O que nasce aqui:
- `iniciar_rota(driver, dia)`: marco RotaInicio (idempotente) + e-mails
  best-effort com o link de acompanhar (1x — `emails_em` trava re-disparo);
- `status_do_pedido(codigo)`: o dict que a página do cliente consome —
  em_preparo | a_caminho (parada N, faltam M, previsão ~HH:MM) | entregue.

ETA deliberadamente simples e honesta: média REAL de minutos por parada da
própria rota (relógio começa no iniciar_rota); antes da 1ª entrega usa
`ETA_MIN_POR_PARADA` (default 12min, env RASTREIO_MIN_POR_PARADA). Previsão
é arredondada pra cima em blocos de 5min e apresentada como "por volta de".
"""
import logging
import os
from datetime import timedelta

from app.extensions import db
from app.models import AtribuicaoEntrega, PedidoOnline, RotaInicio
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

ETA_MIN_POR_PARADA = float(os.environ.get('RASTREIO_MIN_POR_PARADA', '12'))


def _rota_do_driver(driver_id, dia):
    """Atribuições do driver no dia, na ordem da rota."""
    return (AtribuicaoEntrega.query
            .filter(AtribuicaoEntrega.driver_id == driver_id,
                    AtribuicaoEntrega.data_entrega == dia)
            .order_by(AtribuicaoEntrega.ordem, AtribuicaoEntrega.id)
            .all())


def iniciar_rota(driver, dia=None):
    """Marca a saída do motorista e dispara os e-mails (1x). Idempotente:
    segundo clique devolve o marco existente sem reenviar nada.

    Devolve (rota_inicio, emails_enviados). E-mail é best-effort POR PEDIDO:
    um endereço quebrado não pode impedir os outros avisos nem o marco."""
    dia = dia or hoje()
    ri = RotaInicio.query.filter_by(driver_id=driver.id, data=dia).first()
    if ri is None:
        ri = RotaInicio(driver_id=driver.id, data=dia)
        db.session.add(ri)
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001 — corrida do duplo clique no unique
            db.session.rollback()
            ri = RotaInicio.query.filter_by(driver_id=driver.id,
                                            data=dia).first()
            if ri is None:
                raise
    # CLAIM atômico do disparo (padrão do Confirmar do Slack): dois POSTs
    # quase simultâneos (2 aparelhos, retry de rede) passavam ambos pelo
    # `if emails_em is None` e a rota INTEIRA recebia a rajada em dobro.
    # Falha catastrófica no envio devolve o claim (retentável); e-mail
    # individual ruim já é engolido por pedido lá dentro.
    enviados = 0
    ganhou = (RotaInicio.query
              .filter(RotaInicio.id == ri.id, RotaInicio.emails_em.is_(None))
              .update({'emails_em': agora()}, synchronize_session=False))
    db.session.commit()
    if ganhou:
        try:
            enviados = _enviar_emails_saida(driver, dia)
        except Exception:
            ri.emails_em = None
            db.session.commit()
            raise
    db.session.refresh(ri)
    return ri, enviados


def _enviar_emails_saida(driver, dia):
    """"Seu pedido saiu para entrega" pra cada pedido do SITE na rota do
    driver. Só pedidos ainda não entregues; sem e-mail = pula em silêncio.

    O link vem da base LOJA_BASE_URL que o `enviar_pedido_a_caminho` já
    resolve sozinho (site, não gestão) — nada de `url_for(_external=True)`,
    que quebra fora de request e apontaria pro host errado."""
    from flask import current_app

    from app.services import email as email_svc
    atribs = [a for a in _rota_do_driver(driver.id, dia)
              if (a.status or 'pendente') == 'pendente']
    codes = [a.pedido_code for a in atribs if a.pedido_code]
    if not codes:
        return 0
    pedidos = PedidoOnline.query.filter(
        PedidoOnline.codigo.in_(codes)).all()
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    enviados = 0
    for p in pedidos:
        if not p.email_cliente:
            continue
        try:
            # Rota real: loja_bp.route('/pedido/<codigo>') — prefixo /loja.
            url = f'{base}/loja/pedido/{p.codigo}' if base else None
            r = email_svc.enviar_pedido_a_caminho(p, rastreio_url=url)
            if isinstance(r, dict) and r.get('ok') is False:
                continue
            enviados += 1
        except Exception:  # noqa: BLE001 — um e-mail ruim não trava a rota
            logger.exception('rastreio: email de saída falhou (%s)', p.codigo)
    return enviados


def _eta_minutos(ri, entregues, restantes_antes):
    """Minutos até a entrega DESTE cliente: média real da rota quando já há
    entrega feita; senão o chute configurável. `restantes_antes` = paradas
    pendentes na frente + a dele própria."""
    if entregues > 0:
        decorrido = (agora() - ri.iniciado_em).total_seconds() / 60.0
        media = max(4.0, decorrido / entregues)   # piso: ninguém entrega em 2min
    else:
        media = ETA_MIN_POR_PARADA
    return media * restantes_antes


def status_do_pedido(codigo):
    """Dict pro JSON público da página do pedido. Nunca levanta — erro vira
    o estado neutro 'em_preparo' (a página já mostra o pedido em si)."""
    try:
        atrib = AtribuicaoEntrega.query.filter_by(pedido_code=codigo).first()
        if atrib is None or atrib.driver_id is None:
            return {'fase': 'em_preparo'}
        if (atrib.status or '') == 'entregue':
            return {'fase': 'entregue',
                    'entregue_em': (atrib.entregue_em.strftime('%H:%M')
                                    if atrib.entregue_em else None)}
        if (atrib.status or '') == 'nao_entregue':
            # Problema na entrega: a página não detalha (quem fala com o
            # cliente é a loja) — mostra o telefone em vez de prometer hora.
            return {'fase': 'problema'}
        ri = RotaInicio.query.filter_by(driver_id=atrib.driver_id,
                                        data=atrib.data_entrega).first()
        if ri is None:
            return {'fase': 'em_preparo'}
        rota = _rota_do_driver(atrib.driver_id, atrib.data_entrega)
        entregues = sum(1 for a in rota if (a.status or '') == 'entregue')
        pendentes_antes = sum(
            1 for a in rota
            if (a.status or 'pendente') == 'pendente'
            and (a.ordem or 0, a.id) < (atrib.ordem or 0, atrib.id))
        minutos = _eta_minutos(ri, entregues, pendentes_antes + 1)
        # Blocos de 5min, sempre pra cima: prometer 07:32 e chegar 07:40
        # irrita; "por volta de 07:40" e chegar 07:35 agrada.
        eta = agora() + timedelta(minutes=minutos)
        eta += timedelta(minutes=(5 - eta.minute % 5) % 5)
        # AtribuicaoEntrega NAO tem relationship com Driver — lookup por id.
        from app.models import Driver
        drv = db.session.get(Driver, atrib.driver_id)
        nome = drv.nome if drv else 'nosso motorista'
        return {'fase': 'a_caminho',
                'driver': nome,
                'parada': pendentes_antes + 1,
                'faltam': pendentes_antes,
                'eta': eta.strftime('%H:%M')}
    except Exception:  # noqa: BLE001
        logger.exception('rastreio: status falhou (%s)', codigo)
        return {'fase': 'em_preparo'}
