"""Envio de email transacional via Postmark (17/06/2026).

Trocado do Resend pro Postmark porque o Resend exige MX em subdomínio pra
verificar o domínio, e o Wix (onde está hospedado o DNS do `opao.online`)
não permite MX em subdomínio. Postmark valida com CNAME (que o Wix
permite) — sem precisar mexer no DNS do email atual.

Usado pra mandar a senha/convite pra novos usuários do gestao.* (admin
cadastra → usuário recebe email com senha + como entrar).

Config (Railway): POSTMARK_SERVER_TOKEN, EMAIL_REMETENTE,
EMAIL_REMETENTE_NOME, APP_BASE_URL, CHATWOOT_PUBLIC_URL.

Best-effort: se Postmark não estiver configurado ou falhar, devolve
{'ok': False, 'erro': ...} — quem chamar decide o fallback (ex: mostrar a
senha na tela pro admin copiar). NUNCA levanta exceção pro caller.
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_POSTMARK_URL = 'https://api.postmarkapp.com/email'
_TIMEOUT = 12
# Stream "outbound" eh o default transacional do Postmark. Mantemos
# explicito pra deixar claro que NAO eh broadcast (newsletter, marketing
# em massa) — tem regra diferente.
_MESSAGE_STREAM = 'outbound'


def disponivel():
    return bool((current_app.config.get('POSTMARK_SERVER_TOKEN') or '').strip())


def enviar(destinatario, assunto, html, *, texto=None):
    """Envia um email. Retorna {'ok': True, 'id': ...} ou
    {'ok': False, 'erro': ...}. Best-effort — nunca propaga exceção."""
    cfg = current_app.config
    token = (cfg.get('POSTMARK_SERVER_TOKEN') or '').strip()
    if not token:
        return {'ok': False, 'erro': 'POSTMARK_SERVER_TOKEN não configurada'}
    if not destinatario or '@' not in destinatario:
        return {'ok': False, 'erro': f'destinatário inválido: {destinatario!r}'}

    remetente_nome = cfg.get('EMAIL_REMETENTE_NOME') or 'O Pão'
    remetente_email = cfg.get('EMAIL_REMETENTE') or 'noreply@opao.online'
    payload = {
        'From': f'{remetente_nome} <{remetente_email}>',
        'To': destinatario,
        'Subject': assunto,
        'HtmlBody': html,
        'MessageStream': _MESSAGE_STREAM,
    }
    if texto:
        payload['TextBody'] = texto
    try:
        r = requests.post(
            _POSTMARK_URL, json=payload,
            headers={'Accept': 'application/json',
                     'Content-Type': 'application/json',
                     'X-Postmark-Server-Token': token},
            timeout=_TIMEOUT)
        # Postmark devolve 200 com ErrorCode=0 em sucesso; outras combos sao
        # falha (ex: 422 com ErrorCode=405 quando sender signature nao foi
        # verificada). Tratamos as duas dimensoes.
        try:
            corpo = r.json() or {}
        except ValueError:
            corpo = {}
        if r.status_code == 200 and corpo.get('ErrorCode') == 0:
            return {'ok': True, 'id': corpo.get('MessageID')}
        detalhe = corpo.get('Message') or r.text[:300] or f'HTTP {r.status_code}'
        codigo = corpo.get('ErrorCode')
        logger.warning('postmark %s (ErrorCode=%s): %s', r.status_code, codigo, detalhe)
        return {'ok': False,
                'erro': f'Postmark recusou ({r.status_code}/{codigo}): {detalhe}'}
    except Exception as exc:  # noqa: BLE001
        logger.exception('email.enviar falhou')
        return {'ok': False, 'erro': str(exc)}


def enviar_confirmacao_pedido(pedido):
    """E-mail de confirmação de pagamento pro cliente do site. Best-effort —
    chamado quando o pedido vira 'pago' (webhook). Resumo + entrega."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Pedido {pedido.codigo} confirmado — O Pão Padaria Artesanal'
    html = _template_confirmacao(pedido, base)
    return enviar(destinatario, assunto, html, texto=_texto_confirmacao(pedido))


def _fmt_brl(v):
    from decimal import Decimal
    return f'R$ {Decimal(str(v or 0)):.2f}'.replace('.', ',')


def _entrega_linha(pedido):
    if pedido.modo_entrega == 'retirada':
        loja = getattr(pedido, 'loja_retirada', None)
        onde = f'Retirada: {loja.nome}' if loja else 'Retirada na loja'
    else:
        onde = pedido.endereco_entrega or 'Entrega'
    quando = ''
    if pedido.data_entrega:
        quando = pedido.data_entrega.strftime('%d/%m/%Y')
        if pedido.janela_entrega:
            quando += f' · {pedido.janela_entrega}'
    return onde, quando


def _template_confirmacao(pedido, base):
    itens = ''.join(
        f'<tr><td style="padding:4px 0;">{it.quantidade}× {it.nome}</td>'
        f'<td style="padding:4px 0;text-align:right;">{_fmt_brl(it.subtotal)}</td></tr>'
        for it in pedido.itens)
    onde, quando = _entrega_linha(pedido)
    link = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    link_html = (f'<a href="{link}" style="color:#8b5a2b;">Acompanhar pedido</a>'
                 if link else '')
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Pagamento confirmado! 🎉
    Pedido <strong>{pedido.codigo}</strong>.</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:18px;">
    <table style="width:100%;font-size:15px;border-collapse:collapse;">{itens}
      <tr><td style="padding-top:10px;border-top:1px solid #eee;">Subtotal</td>
          <td style="padding-top:10px;border-top:1px solid #eee;text-align:right;">{_fmt_brl(pedido.subtotal)}</td></tr>
      <tr><td>Frete</td><td style="text-align:right;">{_fmt_brl(pedido.frete_valor)}</td></tr>
      <tr><td style="font-weight:700;padding-top:6px;">Total</td>
          <td style="font-weight:700;padding-top:6px;text-align:right;color:#8b5a2b;">{_fmt_brl(pedido.valor_total)}</td></tr>
    </table>
  </div>
  <div style="background:#f5efe5;border-radius:12px;padding:16px 20px;">
    <p style="margin:0 0 6px;font-weight:600;">Entrega</p>
    <p style="margin:0;font-size:14px;color:#6b5f54;">{onde}<br>{quando}</p>
  </div>
  <p style="margin-top:20px;">{link_html}</p>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Dúvidas? Responda este e-mail ou fale com a gente.</p>
</div></body></html>"""


def _texto_confirmacao(pedido):
    onde, quando = _entrega_linha(pedido)
    linhas = '\n'.join(f'  {it.quantidade}x {it.nome} — {_fmt_brl(it.subtotal)}'
                       for it in pedido.itens)
    return (
        f'Pagamento confirmado! Pedido {pedido.codigo}.\n\n'
        f'{linhas}\n'
        f'Subtotal: {_fmt_brl(pedido.subtotal)}\n'
        f'Frete: {_fmt_brl(pedido.frete_valor)}\n'
        f'Total: {_fmt_brl(pedido.valor_total)}\n\n'
        f'Entrega: {onde} {quando}\n')


def enviar_reset_senha(cliente, token):
    """Manda o link de redefinição pro cliente. Best-effort — falha silente.

    Link aponta pra `/loja/redefinir-senha/<token>`. Expira em 1h."""
    cfg = current_app.config
    base = (cfg.get('APP_BASE_URL') or '').rstrip('/')
    if not base:
        return {'ok': False, 'erro': 'APP_BASE_URL não configurada'}
    destinatario = (cliente.email or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'cliente sem email'}
    link = f'{base}/loja/redefinir-senha/{token}'
    assunto = 'Redefinir sua senha — O Pão Padaria Artesanal'
    html = _template_reset(cliente.nome, link)
    texto = (f'Olá, {cliente.nome.split()[0] if cliente.nome else ""}!\n\n'
             f'Recebemos um pedido pra redefinir sua senha. Use o link a '
             f'seguir (vale por 1h):\n\n  {link}\n\n'
             f'Se não foi você, ignore — sua senha continua a mesma.')
    return enviar(destinatario, assunto, html, texto=texto)


def _template_reset(nome, link):
    primeiro = nome.split()[0] if nome else ''
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Olá, {primeiro}! 👋</p>
  <div style="background:#fff;border-radius:12px;padding:20px 22px;
  box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:20px;">
    <p style="margin:0 0 14px;font-size:15px;">Recebemos um pedido pra
    redefinir a senha da sua conta. Clique abaixo (o link vale por 1h):</p>
    <a href="{link}" style="display:inline-block;background:#8b5a2b;
    color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;
    font-weight:600;">Redefinir senha</a>
    <p style="color:#6b5f54;font-size:13px;margin:14px 0 0;">
      Se o botão não funcionar, copie e cole no navegador:<br>
      <span style="word-break:break-all;color:#1971c2;">{link}</span></p>
  </div>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Se você não pediu isso, ignore — sua senha continua a mesma. Email
    automático, não responda.</p>
</div></body></html>"""


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
