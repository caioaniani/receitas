"""Magic link diario do motorista.

Cron 05:00 BRT (`driver_magic_cron.py`) chama `rotacionar_e_enviar()`:
1. Pra cada Driver.ativo=True, revoga tokens antigos.
2. Gera novo DriverMagicToken com expira_em = 00:00 BRT do dia seguinte.
3. Envia WhatsApp via Z-API com a URL `/driver/<token>`.

Aceitacao em `_driver_por_token` (driver/routes.py):
- Aceita Driver.token legado (compat).
- Aceita DriverMagicToken.valido pra dia atual.
"""
import logging
import secrets
from datetime import datetime, time, timedelta

from flask import current_app, url_for

from app.extensions import db
from app.models import Driver, DriverMagicToken
from app.utils import agora, hoje
from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)


def _expira_fim_do_dia():
    """Retorna datetime do proximo 00:00 BRT (fim do dia atual)."""
    amanha = hoje_brt() + timedelta(days=1)
    return datetime.combine(amanha, time(0, 0, 0))


def driver_por_magic_token(token):
    """Resolve token magic -> Driver ativo. Retorna Driver ou None."""
    if not token or len(token) < 16:
        return None
    mt = DriverMagicToken.query.filter_by(token=token).first()
    if not mt or not mt.valido:
        return None
    drv = mt.driver
    if not drv or not drv.ativo:
        return None
    return drv


def gerar_token(driver):
    """Cria novo DriverMagicToken pra driver. Revoga os anteriores ativos.
    Retorna o novo DriverMagicToken (nao envia ainda)."""
    # Revoga tudo que ainda esta valido pra esse driver
    DriverMagicToken.query.filter_by(driver_id=driver.id, revogado=False) \
        .update({'revogado': True})

    mt = DriverMagicToken(
        driver_id=driver.id,
        token=secrets.token_urlsafe(32),
        expira_em=_expira_fim_do_dia(),
        revogado=False,
    )
    db.session.add(mt)
    db.session.commit()
    return mt


def enviar_whatsapp(mt):
    """Envia o magic link via Z-API pro telefone do driver.
    Marca enviado_em/enviado_ok no token. Retorna (ok, msg)."""
    from app.services import zapi

    drv = mt.driver
    if not drv.telefone:
        msg = f'Driver {drv.nome} sem telefone — pula envio.'
        mt.enviado_em = agora()
        mt.enviado_ok = False
        db.session.commit()
        return False, msg

    if not zapi.disponivel():
        msg = 'Z-API nao configurada (faltam env vars).'
        mt.enviado_em = agora()
        mt.enviado_ok = False
        db.session.commit()
        return False, msg

    url = url_for('driver.index', token=mt.token, _external=True)
    texto = (
        f'Bom dia, {drv.nome}! ☀️\n\n'
        f'Painel de entregas de hoje ({hoje().strftime("%d/%m")}):\n'
        f'{url}\n\n'
        f'Link valido ate meia-noite. Amanha voce recebe um novo aqui.'
    )
    res = zapi.enviar_texto(drv.telefone, texto)
    ok = bool(res and res.get('ok'))
    mt.enviado_em = agora()
    mt.enviado_ok = ok
    db.session.commit()
    return ok, res.get('erro') if not ok else 'ok'


def magic_ativo(driver):
    """Retorna DriverMagicToken valido do driver (ou None)."""
    return (DriverMagicToken.query
            .filter_by(driver_id=driver.id, revogado=False)
            .filter(DriverMagicToken.expira_em > agora())
            .order_by(DriverMagicToken.criado_em.desc())
            .first())


def criar_se_necessario(driver):
    """Garante que o motorista tem magic link valido do dia.
    Retorna (DriverMagicToken, criado_novo: bool)."""
    mt = magic_ativo(driver)
    if mt:
        return mt, False
    return gerar_token(driver), True


def notificar_pedido(driver, pedido):
    """On-demand: avisa motorista que tem pedido pra retirar.

    - Se nao tem magic link valido: cria + envia mensagem completa com link.
    - Se ja tem: envia mensagem curta lembrando do painel.

    Retorna (ok, msg, mt). `mt` eh o token usado (novo ou existente).
    """
    from app.services import zapi

    if not driver.telefone:
        return False, f'{driver.nome} sem telefone cadastrado', None
    if not zapi.disponivel():
        return False, 'Z-API nao configurada', None

    mt, criado_novo = criar_se_necessario(driver)
    url = url_for('driver.index', token=mt.token, _external=True)
    loja_nome = pedido.loja.nome if pedido.loja else '?'
    data_entrega = (pedido.data_entrega.strftime('%d/%m')
                     if pedido.data_entrega else '?')
    n_itens = len(pedido.itens or [])

    if criado_novo:
        texto = (
            f'Bom dia, {driver.nome}! ☀️\n\n'
            f'Pedido #{pedido.id} de {loja_nome} pronto pra retirar '
            f'({n_itens} item{"ns" if n_itens != 1 else ""}, '
            f'entrega {data_entrega}).\n\n'
            f'Seu painel de entregas de hoje:\n{url}\n\n'
            f'Link valido ate meia-noite.'
        )
    else:
        texto = (
            f'{driver.nome}, pedido #{pedido.id} de {loja_nome} '
            f'pronto pra retirar ({n_itens} item{"ns" if n_itens != 1 else ""}).\n'
            f'Veja no seu painel: {url}'
        )
    res = zapi.enviar_texto(driver.telefone, texto)
    ok = bool(res and res.get('ok'))
    mt.enviado_em = agora()
    mt.enviado_ok = ok
    db.session.commit()
    return ok, res.get('erro') if not ok else 'ok', mt


def rotacionar_e_enviar():
    """Cron diario: pra cada Driver ativo, gera novo token e envia.
    Retorna lista de dicts {driver, ok, erro?}."""
    resultados = []
    drivers = Driver.query.filter_by(ativo=True).all()
    for drv in drivers:
        try:
            mt = gerar_token(drv)
            ok, msg = enviar_whatsapp(mt)
            resultados.append({'driver': drv.nome, 'ok': ok,
                                'erro': None if ok else msg})
            if ok:
                logger.info('Magic link enviado: %s', drv.nome)
            else:
                logger.warning('Magic link falha %s: %s', drv.nome, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Magic link erro %s', drv.nome)
            resultados.append({'driver': drv.nome, 'ok': False,
                                'erro': str(exc)[:200]})
    return resultados


def telefones_drivers_ativos():
    """Numeros normalizados de motoristas ativos (pra whitelist Z-API)."""
    from app.services.zapi import _normalizar_numero
    nums = (db.session.query(Driver.telefone)
            .filter(Driver.ativo.is_(True))
            .filter(Driver.telefone.isnot(None))
            .all())
    return {_normalizar_numero(t[0]) for t in nums if t[0]}


# pra evitar warning de unused imports
_ = current_app
