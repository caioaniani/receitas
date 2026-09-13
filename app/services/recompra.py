"""Recompra — e-mail pós-compra automático do site (13/09/2026).

Pedido do dono ("Gostei da ideia 3"): 87% dos clientes do site compraram
UMA vez e sumiram (793 clientes em 3 meses, 690 com 1 compra). O gesto mais
barato de trazer gente de volta é um e-mail automático alguns dias depois
da compra, com o que a pessoa comprou e o link para repetir.

Decisões (opções recomendadas — o dono dispensou o questionário):
- SEM cupom: não mexe em preço, NF nem Pagar.me; o resultado se mede por
  UTM (`utm_medium=recompra`) e pela métrica "voltou?" da tela.
- Dispara `recompra_dias` (default 12) dias depois do PAGAMENTO, só para
  quem NÃO voltou a comprar. Dois textos: "pão" (itens do dia a dia) e
  "presente" (cesta/menu, destinatário ou cartinha — "seu pão acabou?" não
  cabe para quem mandou presente).
- NASCE DESLIGADO (`recompra_ativo`), mesmo padrão do aniversário: o 1º
  e-mail de marketing para a base real é gesto do dono na tela.
- Sai pelo stream BROADCAST do Postmark (nunca pelo transacional — uma
  reclamação de spam não pode derrubar o e-mail de pedido) com link de
  descadastro PRÓPRIO (`/loja/marketing/sair/<token>`), que marca
  `Cliente.marketing_descadastro_em` e tira a pessoa das listas do
  Listmonk (best-effort).

Salvaguardas: 1 e-mail por pedido (`RecompraEnvio.pedido_id` unique), 1
e-mail por pessoa a cada `ANTI_SPAM_DIAS`, janela de `JANELA_DIAS` (ligar
a chave hoje NÃO dispara para o histórico inteiro), teto por rodada,
opt-out respeitado (e cliente sem cadastro fica de fora — sem `Cliente`
não há onde honrar o descadastro), divulgação/cancelado/não pago fora.
`rodar()` nunca levanta exceção para o cron.
"""
import logging
from datetime import datetime, time, timedelta
from urllib.parse import quote

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer
from markupsafe import escape
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

CFG_ATIVO = 'recompra_ativo'
CFG_DIAS = 'recompra_dias'
CFG_ASSUNTO_PAO = 'recompra_assunto_pao'
CFG_ASSUNTO_PRESENTE = 'recompra_assunto_presente'
CFG_ULTIMO_RUN = 'recompra_ultimo_run'

DIAS_PADRAO = 12
DIAS_MIN, DIAS_MAX = 1, 90
JANELA_DIAS = 3          # pedidos pagos entre D-dias-2 e D-dias
ANTI_SPAM_DIAS = 30      # 1 e-mail de recompra por pessoa a cada 30 dias
TETO_POR_RODADA = 150    # acima disso é sinal de consulta errada, não de venda
MAX_ITENS_EMAIL = 6

ASSUNTO_PAO_PADRAO = 'Seu pão acabou? A fornada de hoje já está no forno'
ASSUNTO_PRESENTE_PADRAO = 'Quem recebeu gostou? Sempre tem uma próxima ocasião'
_SALT_SAIR = 'recompra-sair-v1'
_UTM = 'utm_source=email&utm_medium=recompra&utm_campaign=recompra-{tipo}'


# ── Configuração (AppConfig) ─────────────────────────────────────────

def ligado():
    from app.models import AppConfig
    return AppConfig.get(CFG_ATIVO) == '1'


def dias():
    """Dias entre o pagamento e o e-mail. Valor ilegível/fora da faixa cai
    no padrão com WARNING — nunca zera nem estoura por config torta."""
    from app.models import AppConfig
    bruto = AppConfig.get(CFG_DIAS)
    if bruto in (None, ''):
        return DIAS_PADRAO
    try:
        v = int(str(bruto).strip())
    except (TypeError, ValueError):
        logger.warning('recompra: %s=%r ilegível, usando %d',
                       CFG_DIAS, bruto, DIAS_PADRAO)
        return DIAS_PADRAO
    if not DIAS_MIN <= v <= DIAS_MAX:
        logger.warning('recompra: %s=%r fora de %d-%d, usando %d',
                       CFG_DIAS, v, DIAS_MIN, DIAS_MAX, DIAS_PADRAO)
        return DIAS_PADRAO
    return v


def assunto(tipo):
    from app.models import AppConfig
    chave = CFG_ASSUNTO_PRESENTE if tipo == 'presente' else CFG_ASSUNTO_PAO
    padrao = (ASSUNTO_PRESENTE_PADRAO if tipo == 'presente'
              else ASSUNTO_PAO_PADRAO)
    v = (AppConfig.get(chave) or '').strip()
    return v or padrao


# ── Elegibilidade ────────────────────────────────────────────────────

def _e_presente(pedido):
    """Presente = destinatário/cartinha no pedido OU algum item é cesta
    (produto com composição) / menu configurável (componentes escolhidos)."""
    if (pedido.nome_destinatario or '').strip() or (pedido.cartinha or '').strip():
        return True
    for it in pedido.itens:
        if it.componentes:
            return True
        if it.kind == 'produto' and it.produto is not None and it.produto.itens:
            return True
    return False


def tipo_do_pedido(pedido):
    return 'presente' if _e_presente(pedido) else 'pao'


def _janela(hoje_, d):
    """[início, fim) em datetime dos pagamentos que vencem HOJE:
    pago_em.date() em [hoje - d - (JANELA_DIAS-1), hoje - d]."""
    fim = hoje_ - timedelta(days=d)
    ini = fim - timedelta(days=JANELA_DIAS - 1)
    return (datetime.combine(ini, time.min),
            datetime.combine(fim + timedelta(days=1), time.min))


def _email_key(e):
    return (e or '').strip().lower()


def candidatos(hoje_=None, d=None):
    """Pedidos que recebem o e-mail HOJE, um por pessoa (o mais recente da
    janela), já filtrados por: pago e não cancelado, sem divulgação, e-mail
    válido, cliente cadastrado e não descadastrado, não voltou a comprar
    depois, sem e-mail de recompra recente e nunca enviado para o pedido.

    Devolve [{'pedido', 'tipo', 'email', 'cliente'}] em ordem de pago_em."""
    from app.models import Cliente, PedidoOnline, RecompraEnvio

    hoje_ = hoje_ or hoje()
    d = d or dias()
    ini_dt, fim_dt = _janela(hoje_, d)
    pedidos = (PedidoOnline.query
               .options(selectinload(PedidoOnline.itens))
               .filter(PedidoOnline.pago_em >= ini_dt,
                       PedidoOnline.pago_em < fim_dt,
                       PedidoOnline.status != 'cancelado',
                       PedidoOnline.divulgacao.is_(False))
               .order_by(PedidoOnline.pago_em).all())
    if not pedidos:
        return []

    # Um por pessoa: o pedido MAIS RECENTE da janela.
    por_email = {}
    for p in pedidos:
        chave = _email_key(p.email_cliente)
        if '@' not in chave:
            continue
        por_email[chave] = p
    if not por_email:
        return []
    emails = set(por_email)

    # Já enviado para o pedido / pessoa com envio recente (anti-spam).
    ja_pedido = {r[0] for r in db.session.query(RecompraEnvio.pedido_id)
                 .filter(RecompraEnvio.pedido_id.in_(
                     [p.id for p in por_email.values()])).all()}
    corte_spam = agora() - timedelta(days=ANTI_SPAM_DIAS)
    spam = {r[0] for r in db.session.query(RecompraEnvio.email)
            .filter(RecompraEnvio.email.in_(emails),
                    RecompraEnvio.enviado_em >= corte_spam).all()}

    # Cadastro + opt-out. Sem `Cliente` não há onde honrar o descadastro —
    # fica de fora (conservador; guest do checkout vira Cliente há meses).
    clientes = {}
    for c in (Cliente.query
              .filter(func.lower(Cliente.email).in_(emails)).all()):
        clientes.setdefault(_email_key(c.email), c)

    # Voltou a comprar depois do pedido da janela?
    voltou = {}
    for e, pago_em in (db.session.query(
            func.lower(PedidoOnline.email_cliente), PedidoOnline.pago_em)
            .filter(func.lower(PedidoOnline.email_cliente).in_(emails),
                    PedidoOnline.pago_em >= fim_dt,
                    PedidoOnline.status != 'cancelado').all()):
        voltou[e] = max(voltou.get(e) or pago_em, pago_em)

    out = []
    for chave, p in sorted(por_email.items(), key=lambda kv: kv[1].pago_em):
        if p.id in ja_pedido or chave in spam:
            continue
        cli = clientes.get(chave)
        if cli is None or not cli.ativo or cli.marketing_descadastro_em:
            continue
        if chave in voltou and voltou[chave] > p.pago_em:
            continue
        out.append({'pedido': p, 'tipo': tipo_do_pedido(p),
                    'email': chave, 'cliente': cli})
    return out


# ── Token de descadastro ─────────────────────────────────────────────

def _serializer():
    return URLSafeSerializer(current_app.config['SECRET_KEY'], salt=_SALT_SAIR)


def token_sair(email):
    """Token NÃO expira de propósito: link de descadastro tem que funcionar
    meses depois, senão a pessoa reclama de spam em vez de sair."""
    return _serializer().dumps(_email_key(email))


def email_do_token(token):
    try:
        e = _serializer().loads(token or '')
    except BadData:
        return None
    e = _email_key(e if isinstance(e, str) else '')
    return e if '@' in e else None


def descadastrar(email):
    """Marca `marketing_descadastro_em` em TODO `Cliente` com o e-mail e,
    best-effort, descadastra a pessoa das listas permanentes do Listmonk
    (senão continuaria recebendo as campanhas). Idempotente. Devolve o
    número de clientes marcados agora."""
    from app.models import Cliente

    chave = _email_key(email)
    if '@' not in chave:
        return 0
    n = (Cliente.query
         .filter(func.lower(Cliente.email) == chave,
                 Cliente.marketing_descadastro_em.is_(None))
         .update({'marketing_descadastro_em': agora()},
                 synchronize_session=False))
    db.session.commit()
    _descadastrar_listmonk(chave)
    return n


def _descadastrar_listmonk(email):
    from app.services import listmonk, marketing
    try:
        if not listmonk.disponivel():
            return
        sid = listmonk.id_por_email(email)
        if sid:
            listmonk.mudar_listas([sid], 'unsubscribe',
                                  marketing._ids_permanentes())
    except Exception:  # noqa: BLE001 — a página de descadastro nunca cai
        logger.exception('recompra: descadastro no Listmonk falhou (%s)', email)


# ── Montagem do e-mail ───────────────────────────────────────────────

def _base_url():
    cfg = current_app.config
    return (cfg.get('LOJA_BASE_URL') or cfg.get('APP_BASE_URL') or '').rstrip('/')


def _link_item(base, it, tipo):
    from app.services.loja_catalogo import href_publico
    item_id = it.receita_id if it.kind == 'receita' else it.produto_id
    if not item_id:
        return None
    return f'{base}{href_publico(it.kind, item_id, it.nome)}?{_UTM.format(tipo=tipo)}'


def montar_email(pedido, tipo, base=None):
    """(assunto, html, texto) — tudo que vem do banco passa por `escape`
    (nome do cliente e dos itens são texto digitado)."""
    base = base if base is not None else _base_url()
    primeiro = escape((pedido.nome_cliente or '').strip().split(' ')[0] or 'olá')
    utm = _UTM.format(tipo=tipo)
    link_loja = f'{base}/loja/?{utm}'
    link_sair = f'{base}/loja/marketing/sair/{quote(token_sair(pedido.email_cliente))}'

    linhas_html, linhas_txt = [], []
    for it in list(pedido.itens)[:MAX_ITENS_EMAIL]:
        nome = escape(it.nome or '')
        link = _link_item(base, it, tipo)
        rotulo = f'{it.quantidade}× {nome}'
        if link:
            linhas_html.append(
                f'<li style="padding:3px 0;"><a href="{link}" '
                f'style="color:#8b5a2b;">{rotulo}</a></li>')
        else:
            linhas_html.append(f'<li style="padding:3px 0;">{rotulo}</li>')
        linhas_txt.append(f'- {it.quantidade}x {it.nome}')
    itens_html = ''.join(linhas_html)
    itens_txt = '\n'.join(linhas_txt)

    if tipo == 'presente':
        abertura = (f'{primeiro}, esperamos que quem recebeu tenha adorado. '
                    'Sempre tem uma próxima ocasião — e a gente entrega com '
                    'cartinha, no dia e na hora que você escolher.')
        cta = 'Ver cestas e presentes'
        abertura_txt = abertura
    else:
        abertura = (f'{primeiro}, faz uns dias que o seu pão chegou. A fornada '
                    'de hoje já está no forno — quer repetir?')
        cta = 'Pedir de novo'
        abertura_txt = abertura

    html = f'''<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#333;">
  <h2 style="color:#8b5a2b;margin:0 0 12px;">O Pão</h2>
  <p style="font-size:16px;line-height:1.5;">{abertura}</p>
  <p style="margin:16px 0 6px;font-weight:bold;">Da última vez você pediu:</p>
  <ul style="padding-left:18px;margin:0 0 18px;">{itens_html}</ul>
  <p style="margin:0 0 24px;">
    <a href="{link_loja}" style="background:#8b5a2b;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;display:inline-block;font-weight:bold;">{cta}</a>
  </p>
  <p style="font-size:12px;color:#888;line-height:1.5;border-top:1px solid #eee;padding-top:12px;">
    Você recebe este e-mail porque comprou em opao.online.
    <a href="{link_sair}" style="color:#888;">Não quero receber novidades</a>.
  </p>
</div>'''
    texto = (f'{abertura_txt}\n\nDa última vez você pediu:\n{itens_txt}\n\n'
             f'{cta}: {link_loja}\n\n'
             f'Você recebe este e-mail porque comprou em opao.online. '
             f'Para não receber novidades: {link_sair}\n')
    return assunto(tipo), html, texto


# ── Envio ────────────────────────────────────────────────────────────

def _stream():
    return (current_app.config.get('POSTMARK_BROADCAST_STREAM') or '').strip()


def enviar_para(cand, base=None):
    """Envia o e-mail de UM candidato e registra o `RecompraEnvio`.
    Devolve {'ok', 'codigo', 'email', 'tipo', 'erro'?}."""
    from app.models import RecompraEnvio
    from app.services import email as email_svc

    p, tipo, dest = cand['pedido'], cand['tipo'], cand['email']
    ass, html, texto = montar_email(p, tipo, base)
    r = email_svc.enviar(dest, ass, html, texto=texto, stream=_stream())
    if not r.get('ok'):
        return {'ok': False, 'codigo': p.codigo, 'email': dest, 'tipo': tipo,
                'erro': r.get('erro')}
    db.session.add(RecompraEnvio(pedido_id=p.id, email=dest, tipo=tipo,
                                 message_id=(r.get('id') or '')[:80] or None))
    db.session.commit()
    return {'ok': True, 'codigo': p.codigo, 'email': dest, 'tipo': tipo}


def rodar(enviar=None, forcar=False, dry_run=False, hoje_=None):
    """Rodada do dia. `enviar=None` = obedece a chave da tela; `forcar=True`
    manda mesmo com a chave desligada (gesto explícito do dono);
    `dry_run=True` só lista quem receberia. Nunca levanta exceção."""
    from app.models import AppConfig
    from app.services import email as email_svc

    try:
        lista = candidatos(hoje_=hoje_)
    except Exception as exc:  # noqa: BLE001 — cron best-effort
        logger.exception('recompra: falha ao listar candidatos')
        db.session.rollback()
        return {'erro': str(exc), 'candidatos': 0, 'enviados': 0}

    resumo_lista = [{'codigo': c['pedido'].codigo, 'email': c['email'],
                     'tipo': c['tipo'], 'nome': c['pedido'].nome_cliente}
                    for c in lista]
    st = {'candidatos': len(lista), 'enviados': 0, 'erros': 0,
          'teto_atingido': len(lista) > TETO_POR_RODADA, 'lista': resumo_lista}
    if dry_run:
        st['pulou'] = 'dry-run'
        return st
    if enviar is None:
        enviar = ligado()
    if not enviar and not forcar:
        st['pulou'] = 'desligado'
        return st
    if not email_svc.disponivel():
        st['erro'] = 'POSTMARK_SERVER_TOKEN não configurada'
        return st
    if st['teto_atingido']:
        # Sinal de consulta/config errada (ou de a chave ter sido ligada com
        # um histórico enorme na janela) — não dispara em massa no escuro.
        st['erro'] = (f'{len(lista)} candidatos passam do teto de '
                      f'{TETO_POR_RODADA} — nada enviado')
        logger.error('recompra: %s', st['erro'])
        return st

    for cand in lista:
        try:
            r = enviar_para(cand)
        except Exception:  # noqa: BLE001 — um e-mail ruim não derruba a rodada
            logger.exception('recompra: envio falhou (%s)', cand['email'])
            db.session.rollback()
            r = {'ok': False}
        if r.get('ok'):
            st['enviados'] += 1
        else:
            st['erros'] += 1
    try:
        AppConfig.set(CFG_ULTIMO_RUN, agora().strftime('%d/%m/%Y %H:%M'))
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('recompra: não gravou o marcador da rodada')
        db.session.rollback()
    logger.info('recompra: %d candidato(s), %d enviado(s), %d erro(s)',
                st['candidatos'], st['enviados'], st['erros'])
    return st


# ── Tela ─────────────────────────────────────────────────────────────

def resumo(hoje_=None):
    """Estado para o card de /admin/marketing. 'voltaram' = envios dos
    últimos 30 dias cujo e-mail tem pedido pago DEPOIS do envio."""
    from app.models import AppConfig, PedidoOnline, RecompraEnvio
    from app.services import email as email_svc

    corte = agora() - timedelta(days=30)
    envios = (RecompraEnvio.query
              .filter(RecompraEnvio.enviado_em >= corte)
              .order_by(RecompraEnvio.enviado_em).all())
    voltaram = 0
    if envios:
        emails = {e.email for e in envios}
        primeiro = min(e.enviado_em for e in envios)
        pagos = {}
        for e, pago_em in (db.session.query(
                func.lower(PedidoOnline.email_cliente), PedidoOnline.pago_em)
                .filter(func.lower(PedidoOnline.email_cliente).in_(emails),
                        PedidoOnline.pago_em > primeiro,
                        PedidoOnline.status != 'cancelado').all()):
            pagos.setdefault(e, []).append(pago_em)
        for env in envios:
            if any(pe > env.enviado_em for pe in pagos.get(env.email, [])):
                voltaram += 1
    try:
        n_cand = len(candidatos(hoje_=hoje_))
    except Exception:  # noqa: BLE001 — a tela nunca cai por causa da prévia
        logger.exception('recompra: prévia de candidatos falhou')
        n_cand = None
    return {
        'auto': ligado(),
        'dias': dias(),
        'assunto_pao': assunto('pao'),
        'assunto_presente': assunto('presente'),
        'disponivel': email_svc.disponivel(),
        'candidatos_hoje': n_cand,
        'enviados_30d': len(envios),
        'voltaram_30d': voltaram,
        'taxa_pct': (round(100.0 * voltaram / len(envios), 1) if envios else None),
        'ultimo_run': AppConfig.get(CFG_ULTIMO_RUN),
        'teto': TETO_POR_RODADA,
        'janela': JANELA_DIAS,
        'anti_spam': ANTI_SPAM_DIAS,
    }
