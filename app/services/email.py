"""Envio de email transacional via Resend (16/06/2026).

Usado pra mandar a senha/convite pra novos usuários do gestao.* (admin
cadastra → usuário recebe email com senha + como entrar). Resend foi
escolhido pela simplicidade (HTTP API, sem SMTP no servidor).

Config (Railway): RESEND_API_KEY, EMAIL_REMETENTE, EMAIL_REMETENTE_NOME,
APP_BASE_URL, CHATWOOT_PUBLIC_URL.

Best-effort: se Resend não estiver configurado ou falhar, devolve
{'ok': False, 'erro': ...} — quem chamar decide o fallback (ex: mostrar a
senha na tela pro admin copiar). NUNCA levanta exceção pro caller.
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_RESEND_URL = 'https://api.resend.com/emails'
_TIMEOUT = 12


def disponivel():
    return bool((current_app.config.get('RESEND_API_KEY') or '').strip())


def enviar(destinatario, assunto, html, *, texto=None):
    """Envia um email. Retorna {'ok': True, 'id': ...} ou
    {'ok': False, 'erro': ...}. Best-effort — nunca propaga exceção."""
    cfg = current_app.config
    api_key = (cfg.get('RESEND_API_KEY') or '').strip()
    if not api_key:
        return {'ok': False, 'erro': 'RESEND_API_KEY não configurada'}
    if not destinatario or '@' not in destinatario:
        return {'ok': False, 'erro': f'destinatário inválido: {destinatario!r}'}

    remetente_nome = cfg.get('EMAIL_REMETENTE_NOME') or 'O Pão'
    remetente_email = cfg.get('EMAIL_REMETENTE') or 'noreply@opao.online'
    payload = {
        'from': f'{remetente_nome} <{remetente_email}>',
        'to': [destinatario],
        'subject': assunto,
        'html': html,
    }
    if texto:
        payload['text'] = texto
    try:
        r = requests.post(
            _RESEND_URL, json=payload,
            headers={'Authorization': f'Bearer {api_key}',
                     'Content-Type': 'application/json'},
            timeout=_TIMEOUT)
        if r.status_code not in (200, 201):
            detalhe = ''
            try:
                detalhe = (r.json() or {}).get('message') or r.text[:300]
            except ValueError:
                detalhe = r.text[:300]
            logger.warning('resend %s: %s', r.status_code, detalhe)
            return {'ok': False, 'erro': f'Resend recusou ({r.status_code}): {detalhe}'}
        return {'ok': True, 'id': (r.json() or {}).get('id')}
    except Exception as exc:  # noqa: BLE001
        logger.exception('email.enviar falhou')
        return {'ok': False, 'erro': str(exc)}


def enviar_boas_vindas(destinatario, nome, login, senha):
    """Email de convite pro novo usuário: senha do gestao.* + como entrar
    no atendimento (Chatwoot). Cadastro do Chatwoot ainda é manual (Super
    Admin lá) — o email só ORIENTA o atendente."""
    cfg = current_app.config
    base = (cfg.get('APP_BASE_URL') or '').rstrip('/')
    chatwoot = (cfg.get('CHATWOOT_PUBLIC_URL') or '').rstrip('/')
    assunto = 'Seu acesso — O Pão Padaria Artesanal'
    html = _template_boas_vindas(nome, login, senha, base, chatwoot)
    texto = _texto_boas_vindas(nome, login, senha, base, chatwoot)
    return enviar(destinatario, assunto, html, texto=texto)


def _template_boas_vindas(nome, login, senha, base, chatwoot):
    login_url = f'{base}/auth/login' if base else '(link do sistema)'
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;font-weight:700;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 24px;">Bem-vindo(a), {nome}! 👋</p>

  <div style="background:#fff;border-radius:12px;padding:20px 22px;
  box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:20px;">
    <p style="margin:0 0 14px;font-size:15px;">Seu acesso ao sistema de gestão:</p>
    <table style="font-size:15px;line-height:2;">
      <tr><td style="color:#6b5f54;padding-right:12px;">Login:</td>
          <td><strong>{login}</strong></td></tr>
      <tr><td style="color:#6b5f54;padding-right:12px;">Senha:</td>
          <td><strong style="font-family:monospace;background:#f5efe5;
          padding:2px 8px;border-radius:4px;">{senha}</strong></td></tr>
    </table>
    <a href="{login_url}" style="display:inline-block;margin-top:18px;
    background:#8b5a2b;color:#fff;text-decoration:none;padding:12px 24px;
    border-radius:6px;font-weight:600;">Entrar no sistema</a>
    <p style="color:#6b5f54;font-size:13px;margin:14px 0 0;">
      Troque a senha no primeiro acesso (menu do seu perfil).</p>
  </div>

  <div style="background:#f5efe5;border-radius:12px;padding:18px 22px;">
    <p style="margin:0 0 8px;font-weight:600;">📞 Atendimento (Chatwoot)</p>
    <p style="margin:0 0 10px;font-size:14px;color:#6b5f54;">
      O atendimento aos clientes (WhatsApp, Instagram, site) é feito no
      Chatwoot, um sistema separado. Você vai receber um convite por email
      direto dele pra criar sua conta lá.</p>
    <p style="margin:0;font-size:14px;">
      Endereço: <a href="{chatwoot}" style="color:#1971c2;">{chatwoot}</a></p>
  </div>

  <p style="color:#9a8d80;font-size:12px;margin-top:28px;">
    Email automático — não responda. Dúvidas? Fale com o gerente.</p>
</div></body></html>"""


def _texto_boas_vindas(nome, login, senha, base, chatwoot):
    login_url = f'{base}/auth/login' if base else '(link do sistema)'
    return (
        f'Bem-vindo(a), {nome}!\n\n'
        f'Seu acesso ao sistema de gestão:\n'
        f'  Login: {login}\n'
        f'  Senha: {senha}\n\n'
        f'Entrar: {login_url}\n'
        f'(Troque a senha no primeiro acesso.)\n\n'
        f'Atendimento aos clientes (Chatwoot): {chatwoot}\n'
        f'Você receberá um convite por email direto do Chatwoot pra criar '
        f'sua conta lá.\n\n'
        f'Email automático — não responda.'
    )
