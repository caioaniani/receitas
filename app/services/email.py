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


def enviar(destinatario, assunto, html, *, texto=None, anexos=None):
    """Envia um email. Retorna {'ok': True, 'id': ...} ou
    {'ok': False, 'erro': ...}. Best-effort — nunca propaga exceção.

    `anexos`: lista de (nome_arquivo, bytes, content_type) — vira o campo
    `Attachments` do Postmark (conteúdo em base64). Limite Postmark: 10MB
    por mensagem; boleto/DANFE ficam na casa dos KB."""
    cfg = current_app.config
    token = (cfg.get('POSTMARK_SERVER_TOKEN') or '').strip()
    if not token:
        return {'ok': False, 'erro': 'POSTMARK_SERVER_TOKEN não configurada'}
    if not destinatario or '@' not in destinatario:
        return {'ok': False, 'erro': f'destinatário inválido: {destinatario!r}'}

    remetente_nome = cfg.get('EMAIL_REMETENTE_NOME') or 'O Pão'
    remetente_email = cfg.get('EMAIL_REMETENTE') or 'noreply@opao.online'
    # Reply-To: pra onde a resposta do cliente vai. Diferente do From
    # (noreply@). Sem Reply-To, "responda este e-mail" cai no vazio
    # (incidente 24/06/2026 — dono pegou). Default: contato@opao.online,
    # que ja aparece no rodape do site como contato publico.
    reply_to = (cfg.get('EMAIL_REPLY_TO') or 'contato@opao.online').strip()
    payload = {
        'From': f'{remetente_nome} <{remetente_email}>',
        'To': destinatario,
        'Subject': assunto,
        'HtmlBody': html,
        'MessageStream': _MESSAGE_STREAM,
    }
    if reply_to and reply_to.lower() != remetente_email.lower():
        payload['ReplyTo'] = reply_to
    if texto:
        payload['TextBody'] = texto
    if anexos:
        import base64
        payload['Attachments'] = [{
            'Name': nome,
            'Content': base64.b64encode(conteudo).decode('ascii'),
            'ContentType': ctype,
        } for nome, conteudo, ctype in anexos]
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
        return {'ok': False, 'erro': str(exc), 'incerto': True}


def enviar_pedido_recebido(pedido):
    """E-mail "recebemos seu pedido" — quando o checkout grava o
    `PedidoOnline` (status `aguardando_pagamento`). Lembra o cliente do
    pedido e direciona pra continuar o pagamento.

    Best-effort — falha silente, não derruba o checkout."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Recebemos seu pedido {pedido.codigo} — O Pão Padaria Artesanal'
    html = _template_pedido_recebido(pedido, base)
    texto = _texto_pedido_recebido(pedido, base)
    return enviar(destinatario, assunto, html, texto=texto)


def enviar_confirmacao_pedido(pedido):
    """E-mail de confirmação de pagamento pro cliente do site. Best-effort —
    chamado quando o pedido vira 'pago' (webhook). Resumo + entrega."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Pedido {pedido.codigo} confirmado — O Pão Padaria Artesanal'
    html = _template_confirmacao(pedido, base)
    return enviar(destinatario, assunto, html, texto=_texto_confirmacao(pedido))


def enviar_pedido_a_caminho(pedido, rastreio_url=None):
    """E-mail "seu pedido saiu pra entrega" — mudança de status pra
    `a_caminho`. Inclui o endereço, a janela e (quando há corrida Lalamove)
    o link de rastreio em tempo real.

    Best-effort — falha silente."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Pedido {pedido.codigo} a caminho — O Pão Padaria Artesanal'
    html = _template_a_caminho(pedido, base, rastreio_url=rastreio_url)
    texto = _texto_a_caminho(pedido, base, rastreio_url=rastreio_url)
    return enviar(destinatario, assunto, html, texto=texto)


def enviar_pedido_entregue(pedido):
    """E-mail "pedido entregue" — confirmação + link da NF (se disponível)
    + pista pra avaliar/comprar de novo.

    Best-effort — falha silente."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Pedido {pedido.codigo} entregue — O Pão Padaria Artesanal'
    html = _template_entregue(pedido, base)
    texto = _texto_entregue(pedido, base)
    return enviar(destinatario, assunto, html, texto=texto)


def enviar_reembolso_confirmado(pedido, valor=None, metodo=None):
    """Comprovante de ESTORNO pro cliente (dono 12/08/2026, caso 131B16EA:
    "quando for estornado o cliente recebesse o e-mail com o comprovante").
    Disparado pelo reembolso ADMIN (`loja_pagamento.reembolsar_pedido`) —
    não pelo webhook, pra estorno iniciado no painel do gateway não gerar
    e-mail duplicado. Best-effort — falha silente."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    assunto = (f'Estorno confirmado — pedido {pedido.codigo} · '
               f'O Pão Padaria Artesanal')
    v = _fmt_brl(valor if valor is not None else pedido.valor_total)
    met = (metodo or '').lower()
    if met == 'pix':
        prazo = ('O valor volta pela MESMA chave Pix do pagamento — '
                 'normalmente em instantes.')
        met_label = 'Pix'
    elif met:
        prazo = ('O valor volta na fatura do MESMO cartão usado na compra — '
                 'o prazo de aparecer depende do seu banco (em geral até '
                 '2 faturas).')
        met_label = 'Cartão'
    else:
        prazo = 'O valor volta pela mesma forma de pagamento da compra.'
        met_label = 'mesma forma de pagamento'
    html = f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Estorno confirmado.
    Pedido <strong>{pedido.codigo}</strong>.</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:18px;">
    <p style="margin:0 0 6px;font-weight:600;">Valor estornado</p>
    <p style="margin:0;font-size:26px;font-weight:700;color:#8b5a2b;">{v}</p>
    <p style="margin:8px 0 0;font-size:14px;color:#6b5f54;">
      Forma: {met_label}<br>{prazo}</p>
  </div>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Qualquer dúvida, chame no WhatsApp
    <a href="https://wa.me/5511971097090" style="color:#8b5a2b;">(11) 97109-7090</a>.</p>
</div></body></html>"""
    texto = (f'Estorno confirmado — pedido {pedido.codigo}.\n\n'
             f'Valor estornado: {v}\nForma: {met_label}\n{prazo}\n\n'
             'Dúvidas? WhatsApp (11) 97109-7090.')
    return enviar(destinatario, assunto, html, texto=texto)


def enviar_nf_emitida(pedido):
    """E-mail "sua nota fiscal foi emitida" — disparado logo após a emissão
    automática (pós-pagamento). Inclui o link público pra DANFE (PDF).

    Best-effort — falha silente. Se chegar antes da NF estar persistida com
    `nf_emitida_em`, ainda assim manda (a rota pública busca por código)."""
    destinatario = (pedido.email_cliente or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'pedido sem email'}
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    assunto = f'Nota fiscal do pedido {pedido.codigo} — O Pão Padaria Artesanal'
    html = _template_nf(pedido, base)
    texto = _texto_nf(pedido, base)
    return enviar(destinatario, assunto, html, texto=texto)


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


def _comp_html(it):
    """Composicao escolhida num MENU CONFIGURAVEL (26/07/2026), pra o cliente
    conferir no e-mail o que ele montou. Vazio em item comum."""
    comps = getattr(it, 'componentes', None) or []
    if not comps:
        return ''
    from html import escape
    txt = ' · '.join(f'{int(c.quantidade or 0)}x {escape(c.nome or "")}'
                     for c in comps)
    return (f'<br><span style="font-size:12px;color:#7a6a55;">{txt}</span>')


def _comp_texto(it):
    """Versao texto puro de `_comp_html` (e-mail sem HTML)."""
    comps = getattr(it, 'componentes', None) or []
    if not comps:
        return ''
    return ('\n     ' + ', '.join(
        f'{int(c.quantidade or 0)}x {c.nome}' for c in comps))


def _template_confirmacao(pedido, base):
    itens = ''.join(
        f'<tr><td style="padding:4px 0;">{it.quantidade}× {it.nome}'
        f'{" (fatiado)" if it.fatiado else ""}{_comp_html(it)}</td>'
        f'<td style="padding:4px 0;text-align:right;">{_fmt_brl(it.subtotal)}</td></tr>'
        for it in pedido.itens)
    onde, quando = _entrega_linha(pedido)
    link = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    link_html = (f'<a href="{link}" style="color:#8b5a2b;">Acompanhar pedido</a>'
                 if link else '')
    # Conferência do endereço (dono 09/08/2026, pós-Dia dos Pais: número/
    # complemento errados em massa): o e-mail é a segunda chance de o
    # cliente pegar o erro, ANTES de a rota ser montada. Só entrega.
    # Conferência do endereço (dono 09/08/2026, pós-Dia dos Pais). Canal =
    # SÓ WhatsApp ("ninguém lê esse e-mail, é o noreply") e tom LEVE — a
    # 1ª versão com ⚠️/"algo errado?" soava como problema no pagamento
    # (feedback do dono no mesmo dia). É um lembrete simpático, não alarme.
    aviso_endereco = ''
    if pedido.modo_entrega != 'retirada':
        aviso_endereco = (
            '<br><span style="font-size:13px;color:#8a6d3b;">Vamos entregar '
            'exatamente no endereço acima — dá uma conferida no '
            '<strong>número</strong> e no <strong>complemento</strong>? '
            'Se precisar ajustar algo, é só chamar no WhatsApp '
            '<a href="https://wa.me/5511971097090" style="color:#8b5a2b;">'
            '(11) 97109-7090</a> antes do dia da entrega. 😊</span>')
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
    <p style="margin:0;font-size:14px;color:#6b5f54;">{onde}<br>{quando}{aviso_endereco}</p>
  </div>
  <p style="margin-top:20px;">{link_html}</p>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Dúvidas? Responda este e-mail ou fale com a gente.</p>
</div></body></html>"""


def _texto_confirmacao(pedido):
    onde, quando = _entrega_linha(pedido)
    linhas = '\n'.join(
        f'  {it.quantidade}x {it.nome}'
        f'{" (fatiado)" if it.fatiado else ""} — {_fmt_brl(it.subtotal)}'
        f'{_comp_texto(it)}'
        for it in pedido.itens)
    return (
        f'Pagamento confirmado! Pedido {pedido.codigo}.\n\n'
        f'{linhas}\n'
        f'Subtotal: {_fmt_brl(pedido.subtotal)}\n'
        f'Frete: {_fmt_brl(pedido.frete_valor)}\n'
        f'Total: {_fmt_brl(pedido.valor_total)}\n\n'
        f'Entrega: {onde} {quando}\n')


def _template_pedido_recebido(pedido, base):
    itens = ''.join(
        f'<tr><td style="padding:4px 0;">{it.quantidade}× {it.nome}'
        f'{" (fatiado)" if it.fatiado else ""}{_comp_html(it)}</td>'
        f'<td style="padding:4px 0;text-align:right;">{_fmt_brl(it.subtotal)}</td></tr>'
        for it in pedido.itens)
    onde, quando = _entrega_linha(pedido)
    link = f'{base}/loja/pedido/{pedido.codigo}/pagamento' if base else ''
    link_html = (f'<a href="{link}" style="display:inline-block;background:#8b5a2b;'
                 f'color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;'
                 f'font-weight:600;">Continuar pagamento</a>'
                 if link else '')
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Recebemos seu pedido
    <strong>{pedido.codigo}</strong>! Ele está aguardando pagamento.</p>
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
    Você ainda não foi cobrado. Clique no botão acima para concluir o pagamento.</p>
</div></body></html>"""


def _texto_pedido_recebido(pedido, base):
    onde, quando = _entrega_linha(pedido)
    linhas = '\n'.join(
        f'  {it.quantidade}x {it.nome}'
        f'{" (fatiado)" if it.fatiado else ""} — {_fmt_brl(it.subtotal)}'
        f'{_comp_texto(it)}'
        for it in pedido.itens)
    link = f'{base}/loja/pedido/{pedido.codigo}/pagamento' if base else ''
    return (
        f'Recebemos seu pedido {pedido.codigo}! Aguardando pagamento.\n\n'
        f'{linhas}\n'
        f'Total: {_fmt_brl(pedido.valor_total)}\n\n'
        f'Entrega: {onde} {quando}\n\n'
        f'Continuar o pagamento: {link}\n')


def _template_a_caminho(pedido, base, rastreio_url=None):
    onde, quando = _entrega_linha(pedido)
    link = f'{base}/loja/conta/pedidos/{pedido.codigo}' if base else ''
    link_html = (f'<a href="{link}" style="color:#8b5a2b;">Ver detalhes</a>'
                 if link else '')
    rastreio_html = (
        f'<p style="margin:18px 0 0;"><a href="{rastreio_url}" '
        f'style="display:inline-block;background:#8b5a2b;color:#fff;'
        f'text-decoration:none;padding:11px 20px;border-radius:8px;'
        f'font-weight:600;">Acompanhar a entrega</a></p>'
        if rastreio_url else '')
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Seu pedido
    <strong>{pedido.codigo}</strong> saiu pra entrega! 🚚</p>
  <div style="background:#f5efe5;border-radius:12px;padding:16px 20px;">
    <p style="margin:0 0 6px;font-weight:600;">Entrega</p>
    <p style="margin:0;font-size:14px;color:#6b5f54;">{onde}<br>{quando}</p>
  </div>
  {rastreio_html}
  <p style="margin-top:20px;">{link_html}</p>
</div></body></html>"""


def _texto_a_caminho(pedido, base, rastreio_url=None):
    onde, quando = _entrega_linha(pedido)
    link = f'{base}/loja/conta/pedidos/{pedido.codigo}' if base else ''
    rastreio = f'Acompanhe a entrega: {rastreio_url}\n\n' if rastreio_url else ''
    return (
        f'Seu pedido {pedido.codigo} saiu pra entrega!\n\n'
        f'Entrega: {onde} {quando}\n\n'
        f'{rastreio}'
        f'Detalhes: {link}\n')


def _template_nf(pedido, base):
    """E-mail dedicado da NF — disparado logo após a emissão automática."""
    link_nf = f'{base}/loja/pedido/{pedido.codigo}/nf' if base else ''
    link_pedido = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    nota_id = getattr(pedido, 'tiny_nota_fiscal_id', None) or ''
    nota_html = (f'<p style="margin:0 0 6px;font-size:13px;color:#6b5f54;">'
                 f'Identificador: <code>{nota_id}</code></p>' if nota_id else '')
    botao_html = (f'<p style="margin-top:18px;"><a href="{link_nf}" '
                  f'style="display:inline-block;background:#8b5a2b;color:#fff;'
                  f'text-decoration:none;padding:12px 24px;border-radius:6px;'
                  f'font-weight:600;">Baixar nota fiscal (PDF)</a></p>'
                  if link_nf else '')
    rodape_html = (f'<p style="color:#9a8d80;font-size:12px;margin-top:24px;">'
                   f'<a href="{link_pedido}" style="color:#9a8d80;">Detalhes do '
                   f'pedido</a></p>' if link_pedido else '')
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Sua nota fiscal do pedido
    <strong>{pedido.codigo}</strong> foi emitida. 🧾</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;">
    <p style="margin:0 0 6px;font-weight:600;">Nota fiscal eletrônica</p>
    {nota_html}
    {botao_html}
  </div>
  {rodape_html}
</div></body></html>"""


def _texto_nf(pedido, base):
    link_nf = f'{base}/loja/pedido/{pedido.codigo}/nf' if base else ''
    link_pedido = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    linhas = [f'Sua nota fiscal do pedido {pedido.codigo} foi emitida.']
    if link_nf:
        linhas += ['', f'Baixar a NF (PDF): {link_nf}']
    if link_pedido:
        linhas += ['', f'Detalhes do pedido: {link_pedido}']
    return '\n'.join(linhas)


def _template_entregue(pedido, base):
    link_pedido = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    link_loja = f'{base}/loja/' if base else ''
    tem_nf = bool(getattr(pedido, 'nf_emitida_em', None))
    link_nf = f'{base}/loja/pedido/{pedido.codigo}/nf' if base and tem_nf else ''
    nf_html = (f'<p style="margin:14px 0 0"><a href="{link_nf}" '
               f'style="color:#8b5a2b;">Ver nota fiscal (PDF)</a></p>'
               if link_nf else '')
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Pedido <strong>{pedido.codigo}</strong>
    entregue! ✓ Esperamos que tenha gostado.</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:18px;">
    <p style="margin:0 0 6px;font-weight:600;">Obrigado pela preferência</p>
    <p style="margin:0;font-size:14px;color:#6b5f54;">
      Qualquer feedback é bem-vindo — responda este e-mail ou escreva pra
      <a href="mailto:contato@opao.online"
         style="color:#8b5a2b;">contato@opao.online</a>.</p>
    {nf_html}
  </div>
  <p style="margin-top:20px;">
    <a href="{link_loja}" style="display:inline-block;background:#8b5a2b;
    color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;
    font-weight:600;">Comprar de novo</a>
  </p>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    <a href="{link_pedido}" style="color:#9a8d80;">Detalhes do pedido</a>
  </p>
</div></body></html>"""


def _texto_entregue(pedido, base):
    link_pedido = f'{base}/loja/pedido/{pedido.codigo}' if base else ''
    link_loja = f'{base}/loja/' if base else ''
    tem_nf = bool(getattr(pedido, 'nf_emitida_em', None))
    link_nf = f'{base}/loja/pedido/{pedido.codigo}/nf' if base and tem_nf else ''
    linhas = [f'Pedido {pedido.codigo} entregue!',
              '',
              'Obrigado pela preferência. Qualquer feedback é bem-vindo — '
              'responda este e-mail ou escreva pra contato@opao.online.']
    if link_nf:
        linhas += ['', f'Nota fiscal: {link_nf}']
    linhas += ['', f'Comprar de novo: {link_loja}',
               f'Detalhes do pedido: {link_pedido}']
    return '\n'.join(linhas)


def enviar_reset_senha(cliente, token):
    """Manda o link de redefinição pro cliente. Best-effort — falha silente.

    Link aponta pra `/loja/redefinir-senha/<token>`. Expira em 1h."""
    cfg = current_app.config
    base = (cfg.get('LOJA_BASE_URL') or cfg.get('APP_BASE_URL') or '').rstrip('/')
    if not base:
        return {'ok': False, 'erro': 'LOJA_BASE_URL não configurada'}
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


def enviar_verificacao_cadastro(cliente, token):
    """Manda o link de verificação pra finalizar o cadastro. Best-effort.

    Dispara SÓ quando o cadastro reivindicaria um pedido feito como guest
    (mesmo e-mail já presente no Cliente sem senha). Cadastro de e-mail
    "novo" não passa por aqui (segue instantâneo). Token vale 1h."""
    cfg = current_app.config
    base = (cfg.get('LOJA_BASE_URL') or cfg.get('APP_BASE_URL')
            or '').rstrip('/')
    if not base:
        return {'ok': False, 'erro': 'LOJA_BASE_URL não configurada'}
    destinatario = (cliente.email or '').strip()
    if not destinatario:
        return {'ok': False, 'erro': 'cliente sem email'}
    link = f'{base}/loja/verificar-cadastro/{token}'
    assunto = 'Confirme seu e-mail — O Pão Padaria Artesanal'
    html = _template_verificacao(cliente.nome, link)
    primeiro = cliente.nome.split()[0] if cliente.nome else ''
    texto = (f'Olá, {primeiro}!\n\n'
             f'Vimos que esse e-mail já fez um pedido antes. Pra finalizar a '
             f'criação da sua conta, clique no link abaixo (vale por 1h):\n\n'
             f'  {link}\n\n'
             f'Se não foi você que tentou criar conta, ignore — nada vai '
             f'mudar.')
    return enviar(destinatario, assunto, html, texto=texto)


def _template_verificacao(nome, link):
    primeiro = nome.split()[0] if nome else ''
    return f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Olá, {primeiro}!</p>
  <div style="background:#fff;border-radius:12px;padding:20px 22px;
  box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:20px;">
    <p style="margin:0 0 14px;font-size:15px;">Vimos que esse e-mail já
    fez um pedido antes com a gente. Pra proteger seus dados, confirme
    que é você antes de criar a conta. O link vale por 1h:</p>
    <a href="{link}" style="display:inline-block;background:#8b5a2b;
    color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;
    font-weight:600;">Confirmar e-mail</a>
    <p style="color:#6b5f54;font-size:13px;margin:14px 0 0;">
      Se o botão não funcionar, copie e cole no navegador:<br>
      <span style="word-break:break-all;color:#1971c2;">{link}</span></p>
  </div>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Se não foi você, ignore — nada vai mudar. Email automático, não
    responda.</p>
</div></body></html>"""


def enviar_boas_vindas(destinatario, nome, login, senha, *, com_chatwoot=True):
    """Email de convite pro novo usuário: senha do gestao.* + (opcional) como
    entrar no atendimento (Chatwoot). `com_chatwoot=False` (contas SÓ de
    treinamento) omite o bloco do Chatwoot — nem todo funcionário atende
    cliente, então não deve receber esse convite (decisão do dono 23/07/2026)."""
    cfg = current_app.config
    base = (cfg.get('APP_BASE_URL') or '').rstrip('/')
    chatwoot = (cfg.get('CHATWOOT_PUBLIC_URL') or '').rstrip('/') if com_chatwoot else ''
    assunto = 'Seu acesso — O Pão Padaria Artesanal'
    html = _template_boas_vindas(nome, login, senha, base, chatwoot)
    texto = _texto_boas_vindas(nome, login, senha, base, chatwoot)
    return enviar(destinatario, assunto, html, texto=texto)


def _template_boas_vindas(nome, login, senha, base, chatwoot):
    login_url = f'{base}/auth/login' if base else '(link do sistema)'
    chatwoot_bloco = f"""\
<div style="background:#f5efe5;border-radius:12px;padding:18px 22px;">
    <p style="margin:0 0 8px;font-weight:600;">📞 Atendimento (Chatwoot)</p>
    <p style="margin:0 0 10px;font-size:14px;color:#6b5f54;">
      O atendimento aos clientes (WhatsApp, Instagram, site) é feito no
      Chatwoot, um sistema separado. Você vai receber um convite por email
      direto dele pra criar sua conta lá.</p>
    <p style="margin:0;font-size:14px;">
      Endereço: <a href="{chatwoot}" style="color:#1971c2;">{chatwoot}</a></p>
  </div>""" if chatwoot else ''
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
      No primeiro acesso o sistema vai pedir pra você criar uma nova senha.</p>
  </div>

  {chatwoot_bloco}

  <p style="color:#9a8d80;font-size:12px;margin-top:28px;">
    Email automático — não responda. Dúvidas? Fale com o gerente.</p>
</div></body></html>"""


def enviar_boleto_b2b(cobranca, destinatario, pdf_bytes, *,
                      linha_digitavel=None):
    """E-mail do boleto B2B pro cliente, com o PDF anexado.

    `linha_digitavel` vem pronta do caller (sicredi_boleto) — evita o
    email.py conhecer CNAB. Inclui o Pix copia-e-cola do boleto híbrido
    quando o retorno do banco já trouxe (registro tipo 8)."""
    if cobranca.fatura:
        ref = (f'fatura {cobranca.fatura.codigo} '
               f'({cobranca.fatura.periodo_display})')
    elif cobranca.parcela:
        ref = f'venda B2B #{cobranca.parcela.venda.id}'
    else:
        ref = cobranca.seu_numero
    assunto = (f'Boleto O Pão — vencimento '
               f'{cobranca.vencimento.strftime("%d/%m/%Y")}')
    nome_pdf = f'boleto_{cobranca.nosso_numero}.pdf'
    ld_html = (f'<p style="margin:10px 0 0;font-size:13px;color:#6b5f54;">'
               f'Linha digitável:<br><code style="font-size:13px;">'
               f'{linha_digitavel}</code></p>' if linha_digitavel else '')
    pix_html = ''
    if cobranca.pix_copia_cola:
        pix_html = (
            '<div style="background:#f5efe5;border-radius:12px;'
            'padding:16px 20px;margin-top:14px;">'
            '<p style="margin:0 0 6px;font-weight:600;">Pagar com Pix</p>'
            '<p style="margin:0;font-size:12px;color:#6b5f54;'
            'word-break:break-all;">Copia e cola:<br>'
            f'<code>{cobranca.pix_copia_cola}</code></p></div>')
    html = f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Olá! Segue o boleto da
    {ref} em anexo. 📎</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;">
    <table style="width:100%;font-size:15px;border-collapse:collapse;">
      <tr><td style="padding:4px 0;color:#6b5f54;">Valor</td>
          <td style="padding:4px 0;text-align:right;font-weight:700;">
            {_fmt_brl(cobranca.valor)}</td></tr>
      <tr><td style="padding:4px 0;color:#6b5f54;">Vencimento</td>
          <td style="padding:4px 0;text-align:right;">
            {cobranca.vencimento.strftime('%d/%m/%Y')}</td></tr>
      <tr><td style="padding:4px 0;color:#6b5f54;">Nosso número</td>
          <td style="padding:4px 0;text-align:right;">
            {cobranca.nosso_numero_fmt}</td></tr>
    </table>
    {ld_html}
  </div>
  {pix_html}
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Dúvidas? Responda este e-mail ou fale com a gente.</p>
</div></body></html>"""
    linhas = [f'Segue o boleto da {ref} em anexo.',
              '',
              f'Valor: {_fmt_brl(cobranca.valor)}',
              f'Vencimento: {cobranca.vencimento.strftime("%d/%m/%Y")}',
              f'Nosso número: {cobranca.nosso_numero_fmt}']
    if linha_digitavel:
        linhas += ['', f'Linha digitável: {linha_digitavel}']
    if cobranca.pix_copia_cola:
        linhas += ['', f'Pix copia e cola: {cobranca.pix_copia_cola}']
    return enviar(destinatario, assunto, html, texto='\n'.join(linhas),
                  anexos=[(nome_pdf, pdf_bytes, 'application/pdf')])


def enviar_nf_b2b(doc, destinatario, pdf_bytes, *, rotulo=None):
    """E-mail da NF-e (DANFE) do B2B pro cliente, com o PDF anexado.

    `doc` é VendaB2B ou FaturaB2B (os dois têm nf_numero /
    tiny_nota_fiscal_id / valor_total). `rotulo` descreve a origem no
    corpo (default: 'venda #N'); a fatura mensal passa 'fatura FATxxxxx
    (período)'.

    O link do Tiny expira — por isso o PDF vai ANEXADO (baixado na hora
    pelo caller via `tiny_nf.baixar_danfe_pdf`)."""
    rotulo = rotulo or f'venda #{doc.id}'
    numero = doc.nf_numero or doc.tiny_nota_fiscal_id or ''
    assunto = (f'Nota fiscal {numero} — O Pão Padaria Artesanal' if numero
               else 'Nota fiscal — O Pão Padaria Artesanal')
    nome_pdf = f'nfe_{numero or doc.id}.pdf'
    num_html = (f'<p style="margin:0 0 6px;font-size:13px;color:#6b5f54;">'
                f'Número: <code>{numero}</code></p>' if numero else '')
    html = f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Segue a nota fiscal da sua
    compra ({rotulo}) em anexo. 🧾</p>
  <div style="background:#fff;border-radius:12px;padding:18px 20px;">
    <p style="margin:0 0 6px;font-weight:600;">Nota fiscal eletrônica</p>
    {num_html}
    <p style="margin:0;font-size:13px;color:#6b5f54;">Valor total:
      <strong>{_fmt_brl(doc.valor_total)}</strong></p>
  </div>
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Dúvidas? Responda este e-mail ou fale com a gente.</p>
</div></body></html>"""
    texto = (f'Segue a nota fiscal da {rotulo} em anexo (PDF).\n\n'
             + (f'Número: {numero}\n' if numero else '')
             + f'Valor total: {_fmt_brl(doc.valor_total)}\n')
    return enviar(destinatario, assunto, html, texto=texto,
                  anexos=[(nome_pdf, pdf_bytes, 'application/pdf')])


def enviar_nf_e_boleto_b2b(venda, destinatario, nf_pdf, boletos, *, rotulo=None):
    """Manda a NF (DANFE) + o(s) boleto(s) da venda B2B num e-mail SÓ, com
    todos os PDFs anexados (pedido do dono 10/07/2026 — evita 2 e-mails).

    `boletos` = lista de dicts {cob, pdf, linha_digitavel}. O corpo mostra a
    NF (número/valor) e cada boleto (valor, vencimento, nosso número, linha
    digitável e Pix quando houver)."""
    rotulo = rotulo or f'venda #{venda.id}'
    numero = venda.nf_numero or venda.tiny_nota_fiscal_id or ''
    assunto = (f'Nota fiscal {numero} + boleto — O Pão Padaria Artesanal'
               if numero else 'Nota fiscal + boleto — O Pão Padaria Artesanal')
    anexos = [(f'nfe_{numero or venda.id}.pdf', nf_pdf, 'application/pdf')]

    blocos_html, linhas = [], [
        f'Segue, em anexo, a nota fiscal da {rotulo} '
        + (f'(nº {numero}) ' if numero else '')
        + f'e o(s) boleto(s). Valor total: {_fmt_brl(venda.valor_total)}.', '']
    num_html = (f'<p style="margin:0 0 6px;font-size:13px;color:#6b5f54;">'
                f'NF nº <code>{numero}</code></p>' if numero else '')
    blocos_html.append(
        '<div style="background:#fff;border-radius:12px;padding:18px 20px;'
        'margin-bottom:12px;">'
        '<p style="margin:0 0 6px;font-weight:600;">Nota fiscal eletrônica</p>'
        f'{num_html}'
        f'<p style="margin:0;font-size:13px;color:#6b5f54;">Valor total: '
        f'<strong>{_fmt_brl(venda.valor_total)}</strong></p></div>')

    for b in boletos:
        cob, ld = b['cob'], b.get('linha_digitavel')
        anexos.append((f'boleto_{cob.nosso_numero}.pdf', b['pdf'],
                       'application/pdf'))
        ld_html = (f'<p style="margin:8px 0 0;font-size:13px;color:#6b5f54;">'
                   f'Linha digitável:<br><code>{ld}</code></p>' if ld else '')
        pix_html = ''
        if cob.pix_copia_cola:
            pix_html = ('<p style="margin:8px 0 0;font-size:12px;'
                        'color:#6b5f54;word-break:break-all;">Pix copia e '
                        f'cola:<br><code>{cob.pix_copia_cola}</code></p>')
        blocos_html.append(
            '<div style="background:#fff;border-radius:12px;padding:18px 20px;'
            'margin-bottom:12px;">'
            '<p style="margin:0 0 6px;font-weight:600;">Boleto</p>'
            '<table style="width:100%;font-size:14px;border-collapse:collapse;">'
            f'<tr><td style="color:#6b5f54;">Valor</td><td style="text-align:'
            f'right;font-weight:700;">{_fmt_brl(cob.valor)}</td></tr>'
            f'<tr><td style="color:#6b5f54;">Vencimento</td><td style="'
            f'text-align:right;">{cob.vencimento.strftime("%d/%m/%Y")}</td></tr>'
            f'<tr><td style="color:#6b5f54;">Nosso número</td><td style="'
            f'text-align:right;">{cob.nosso_numero_fmt}</td></tr></table>'
            f'{ld_html}{pix_html}</div>')
        linhas += ['', f'Boleto — valor {_fmt_brl(cob.valor)}, '
                   f'vencimento {cob.vencimento.strftime("%d/%m/%Y")}, '
                   f'nosso número {cob.nosso_numero_fmt}']
        if ld:
            linhas.append(f'Linha digitável: {ld}')
        if cob.pix_copia_cola:
            linhas.append(f'Pix copia e cola: {cob.pix_copia_cola}')

    html = f"""\
<!doctype html><html lang="pt-BR"><body style="margin:0;background:#fbf8f3;
font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#2a2520;">
<div style="max-width:540px;margin:0 auto;padding:32px 24px;">
  <h1 style="font-size:22px;margin:0 0 4px;">O Pão · Padaria Artesanal</h1>
  <p style="color:#6b5f54;margin:0 0 20px;">Olá! Segue a nota fiscal e o
    boleto da sua compra em anexo. 🧾📎</p>
  {''.join(blocos_html)}
  <p style="color:#9a8d80;font-size:12px;margin-top:24px;">
    Dúvidas? Responda este e-mail ou fale com a gente.</p>
</div></body></html>"""
    return enviar(destinatario, assunto, html, texto='\n'.join(linhas),
                  anexos=anexos)


def _texto_boas_vindas(nome, login, senha, base, chatwoot):
    login_url = f'{base}/auth/login' if base else '(link do sistema)'
    chatwoot_txt = (
        f'Atendimento aos clientes (Chatwoot): {chatwoot}\n'
        f'Você receberá um convite por email direto do Chatwoot pra criar '
        f'sua conta lá.\n\n'
    ) if chatwoot else ''
    return (
        f'Bem-vindo(a), {nome}!\n\n'
        f'Seu acesso ao sistema de gestão:\n'
        f'  Login: {login}\n'
        f'  Senha: {senha}\n\n'
        f'Entrar: {login_url}\n'
        f'(No primeiro acesso você vai criar uma nova senha.)\n\n'
        f'{chatwoot_txt}'
        f'Email automático — não responda.'
    )
