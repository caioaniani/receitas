"""Portal Wi-Fi das lojas — cadastro que vira CONTA do site (11/07/2026).

Fluxo (decisões do dono, 11/07/2026):
1. Cliente conecta no Wi-Fi de clientes → cai no portal (/loja/wifi) →
   preenche nome + e-mail + WhatsApp + senha + aniversário + aceite LGPD.
2. O portal mostra o botão do WhatsApp (wa.me da padaria) com o código
   `WIFI-XXXXXX` pré-preenchido. O cliente envia; o webhook do Chatwoot
   (crm/routes) reconhece o código e chama `processar_codigo_whatsapp` —
   a POSSE do número fica PROVADA (a mensagem saiu do aparelho dele).
3. A conta do site é resolvida pelas 4 REGRAS (abaixo) e o bot responde no
   WhatsApp com o link de login one-time — o cliente toca e abre o site
   LOGADO no navegador de verdade (fora do mini-navegador do portal).
4. O aparelho é autorizado no controlador Omada (best-effort; enquanto a
   Open API não estiver configurada, fica pendente e nada quebra).

REGRAS DE CONTA (a posse provada é do TELEFONE; e-mail digitado sem prova
NUNCA loga em conta alheia):
a) e-mail novo + telefone novo            → cria conta (senha do form) e loga;
b) e-mail existe + telefone da conta BATE → loga direto (sem pedir a senha
   antiga — aprovado pelo dono; a senha do form é IGNORADA);
c) telefone validado pertence a OUTRA conta (e-mail diferente) → loga NA
   CONTA DO TELEFONE (posse provada) e avisa qual e-mail ela usa;
d) e-mail existe + telefone NÃO bate      → NÃO loga: manda link de acesso
   pro E-MAIL cadastrado (Postmark). Wi-Fi libera mesmo assim.
Guest (e-mail existe sem senha): telefone batendo (cadastro ou pedidos) ou
guest sem nenhum telefone/pedido → vira conta (upgrade); divergindo → (d).

Senha NUNCA em claro (hash scrypt na entrada). Sessões >30 dias são podadas
na criação de novas (PII — LGPD).
"""
import logging
import re
import secrets
from datetime import timedelta

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.utils import agora, telefone_chave

logger = logging.getLogger(__name__)

# Código que o cliente manda no WhatsApp. Sem 0/O/1/I (ambíguos).
_ALFABETO_CODIGO = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
RE_CODIGO_WIFI = re.compile(r'WIFI[-\s]?([A-Z2-9]{6})', re.IGNORECASE)

SESSAO_VALIDADE_MIN = 30       # cadastro pendente de validação
LOGIN_TOKEN_VALIDADE_MIN = 30  # link de login one-time
_PODA_DIAS = 30                # sessões antigas (PII) somem


def _gerar_codigo():
    return 'WIFI-' + ''.join(secrets.choice(_ALFABETO_CODIGO)
                             for _ in range(6))


# ── Validação forte do formulário (pedido do dono, 12/07/2026: "precisa
# colocar nome e sobrenome, e-mail valido e whatsapp valido") ────────────

# Formato estrito de e-mail (o email_valido do loja_auth aceita 'a@b.c').
_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$')

# Palavra de nome: 2+ letras (aceita acento).
_NOME_PALAVRA_RE = re.compile(r'[A-Za-zÀ-ÖØ-öø-ÿ]{2,}')

# Provedores populares no Brasil — base do detector de typo (gmial.com,
# hotmial.com etc.: domínios a 1 edição de um destes são quase sempre erro
# de digitação, e muitos são squatted — o DNS resolve e não pega).
_PROVEDORES_COMUNS = frozenset({
    'gmail.com', 'hotmail.com', 'outlook.com', 'outlook.com.br',
    'yahoo.com', 'yahoo.com.br', 'icloud.com', 'live.com', 'msn.com',
    'uol.com.br', 'bol.com.br', 'terra.com.br', 'globo.com', 'ig.com.br',
})

# DDDs reais (ANATEL). '20', '23'… não existem — número com DDD inválido
# é erro de digitação na certa.
_DDDS_VALIDOS = frozenset({
    '11', '12', '13', '14', '15', '16', '17', '18', '19',
    '21', '22', '24', '27', '28',
    '31', '32', '33', '34', '35', '37', '38',
    '41', '42', '43', '44', '45', '46', '47', '48', '49',
    '51', '53', '54', '55',
    '61', '62', '63', '64', '65', '66', '67', '68', '69',
    '71', '73', '74', '75', '77', '79',
    '81', '82', '83', '84', '85', '86', '87', '88', '89',
    '91', '92', '93', '94', '95', '96', '97', '98', '99',
})


def _distancia1(a, b):
    """True se `a` e `b` diferem por UMA edição: troca, inserção, remoção
    ou transposição adjacente (Damerau — pega gmial.com ↔ gmail.com)."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        difs = [i for i in range(la) if a[i] != b[i]]
        if len(difs) == 1:
            return True
        return (len(difs) == 2 and difs[1] == difs[0] + 1
                and a[difs[0]] == b[difs[1]] and a[difs[1]] == b[difs[0]])
    if la > lb:
        a, b, la = b, a, lb
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _typo_de_provedor(dominio):
    """Domínio a 1 edição de um provedor popular → devolve a sugestão
    ('gmial.com' → 'gmail.com'). Domínio exato na lista → None (ok)."""
    if dominio in _PROVEDORES_COMUNS:
        return None
    for prov in _PROVEDORES_COMUNS:
        if _distancia1(dominio, prov):
            return prov
    return None


def _dominio_email_resolve(dominio):
    """True se o domínio existe pra receber e-mail: MX, com fallback A/AAAA
    (RFC 5321 — sem MX a entrega cai no A). Fail-open DELIBERADO em erro de
    INFRA de DNS (timeout, resolver fora): instabilidade de rede nunca pode
    barrar cadastro no balcão — só NXDOMAIN/sem registro reprova."""
    try:
        import dns.resolver
    except ImportError:
        logger.warning('wifi_portal: dnspython ausente — e-mail sem '
                       'checagem de domínio')
        return True
    try:
        res = dns.resolver.Resolver()
        res.lifetime = 3
        try:
            if res.resolve(dominio, 'MX'):
                return True
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            pass
        for tipo in ('A', 'AAAA'):
            try:
                if res.resolve(dominio, tipo):
                    return True
            except dns.resolver.NoAnswer:
                continue
        return False
    except dns.resolver.NXDOMAIN:
        return False
    except Exception as e:  # noqa: BLE001 — fail-open de infra (docstring)
        logger.warning('wifi_portal: DNS indisponível pra %s (%s) — '
                       'aceitando sem checar', dominio, e)
        return True


def _whatsapp_valido(telefone):
    """Celular brasileiro: DDD real + nono dígito 9 + 8 dígitos (11 no
    total, com ou sem o 55 do país). Fixo não recebe WhatsApp."""
    from app.utils import normalizar_telefone
    d = normalizar_telefone(telefone)
    if len(d) in (12, 13) and d.startswith('55'):
        d = d[2:]
    if len(d) != 11:
        return False
    if d[:2] not in _DDDS_VALIDOS or d[2] != '9':
        return False
    return len(set(d[2:])) > 1     # 9 9999-9999 etc. = fake


def _podar_antigas():
    from app.models import WifiPortalSessao
    limite = agora() - timedelta(days=_PODA_DIAS)
    try:
        n = (WifiPortalSessao.query
             .filter(WifiPortalSessao.criado_em < limite)
             .delete(synchronize_session=False))
        if n:
            logger.info('wifi_portal: %s sessões antigas podadas (LGPD)', n)
    except Exception:  # noqa: BLE001
        db.session.rollback()


def validar_form(form):
    """Valida os campos do formulário. Retorna (dados, erros)."""
    from app.utils import normalizar_telefone
    erros = []
    nome = (form.get('nome') or '').strip()[:150]
    email = (form.get('email') or '').strip().lower()[:200]
    telefone = (form.get('telefone') or '').strip()[:30]
    senha = form.get('senha') or ''
    aceite = bool(form.get('aceite_lgpd'))
    if len(_NOME_PALAVRA_RE.findall(nome)) < 2:
        erros.append('Informe nome e sobrenome.')
    if not _EMAIL_RE.match(email):
        erros.append('E-mail inválido.')
    else:
        dominio = email.split('@')[-1]
        sugestao = _typo_de_provedor(dominio)
        if sugestao:
            erros.append(f'Confira o e-mail — você quis dizer @{sugestao}?')
        elif not _dominio_email_resolve(dominio):
            erros.append('E-mail inválido — o domínio depois do @ não '
                         'existe. Confira se digitou certo.')
    if not _whatsapp_valido(telefone):
        erros.append('WhatsApp inválido — informe o celular com DDD '
                     '(ex.: 11 91234-5678).')
    if len(senha) < 6:
        erros.append('A senha precisa ter pelo menos 6 caracteres.')
    if not aceite:
        erros.append('É preciso aceitar o termo de uso dos dados (LGPD).')

    def _int(campo, lo, hi):
        try:
            v = int(form.get(campo) or 0)
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    dia = _int('aniversario_dia', 1, 31)
    mes = _int('aniversario_mes', 1, 12)
    ano = _int('nascimento_ano', 1900, agora().year)   # opcional
    if not dia or not mes:
        erros.append('Informe dia e mês do aniversário.')
    dados = {'nome': nome, 'email': email, 'telefone': telefone,
             'senha': senha, 'aniversario_dia': dia, 'aniversario_mes': mes,
             'nascimento_ano': ano,
             'telefone_digitos': normalizar_telefone(telefone)}
    return dados, erros


def criar_sessao(dados, params_omada=None):
    """Cria a sessão pendente do portal. `dados` = saída de validar_form.
    Retorna a sessão (commitada)."""
    from app.models import WifiPortalSessao
    _podar_antigas()
    p = params_omada or {}
    s = WifiPortalSessao(
        token=secrets.token_urlsafe(32),
        codigo=_gerar_codigo(),
        client_mac=(p.get('clientMac') or '')[:20] or None,
        ap_mac=(p.get('apMac') or '')[:20] or None,
        ssid=(p.get('ssidName') or '')[:50] or None,
        site_omada=(p.get('site') or '')[:50] or None,
        redirect_url=(p.get('redirectUrl') or '')[:300] or None,
        nome=dados['nome'], email=dados['email'], telefone=dados['telefone'],
        senha_hash=generate_password_hash(dados['senha'], method='scrypt'),
        aniversario_dia=dados['aniversario_dia'],
        aniversario_mes=dados['aniversario_mes'],
        nascimento_ano=dados['nascimento_ano'],
        aceite_lgpd_em=agora(),
        expira_em=agora() + timedelta(minutes=SESSAO_VALIDADE_MIN),
    )
    db.session.add(s)
    db.session.commit()
    return s


def mensagem_whatsapp(sessao):
    """Texto pré-preenchido do wa.me."""
    return f'Ativar Wi-Fi O Pão — código {sessao.codigo}'


def link_whatsapp(sessao):
    from urllib.parse import quote

    from flask import current_app
    numero = (current_app.config.get('WIFI_PORTAL_WHATSAPP') or '').strip()
    if not numero:
        return None
    return f'https://wa.me/{numero}?text={quote(mensagem_whatsapp(sessao))}'


# ── Resolução de conta (o coração das 4 regras) ──────────────────────────

def _cliente_por_email(email):
    from app.models import Cliente
    return Cliente.query.filter(
        db.func.lower(Cliente.email) == (email or '').lower()).first()


def _cliente_por_telefone(chave):
    """Cliente cujo telefone CADASTRADO bate com a chave canônica. Filtro em
    memória (telefone_chave é Python-side) — mesmo padrão do CRM card."""
    from app.models import Cliente
    if not chave:
        return None
    candidatos = (Cliente.query
                  .filter(Cliente.telefone.isnot(None),
                          Cliente.telefone != '', Cliente.ativo.is_(True))
                  .all())
    for c in candidatos:
        if telefone_chave(c.telefone) == chave:
            return c
    return None


def _guest_tem_telefone_divergente(cliente, chave):
    """Guest: True se há telefone conhecido (cadastro OU pedidos) e NENHUM
    bate com a chave provada — proteger o histórico de pedidos dele."""
    conhecidos = set()
    if cliente.telefone:
        conhecidos.add(telefone_chave(cliente.telefone))
    try:
        for p in cliente.pedidos.limit(20).all():
            if p.telefone_cliente:
                conhecidos.add(telefone_chave(p.telefone_cliente))
    except Exception:  # noqa: BLE001
        pass
    conhecidos.discard('')
    return bool(conhecidos) and chave not in conhecidos


def _aplicar_perfil(cliente, sessao, *, telefone=True):
    """Copia dados do form pro cliente (aniversário, aceite, telefone)."""
    if telefone and sessao.telefone_validado:
        cliente.telefone = sessao.telefone_validado
    cliente.aniversario_dia = sessao.aniversario_dia
    cliente.aniversario_mes = sessao.aniversario_mes
    if sessao.nascimento_ano:
        cliente.nascimento_ano = sessao.nascimento_ano
    if not cliente.aceite_lgpd_em:
        cliente.aceite_lgpd_em = agora()


def _resolver_conta(sessao):
    """Aplica as regras a/b/c/d. Preenche sessao.cliente_id/resultado.
    Retorna dict {'resultado', 'cliente', 'email_mascarado'|None}."""
    from app.models import Cliente
    chave = telefone_chave(sessao.telefone_validado)
    por_email = _cliente_por_email(sessao.email)
    por_tel = _cliente_por_telefone(chave)

    # (b)/(d)/guest — e-mail já existe.
    if por_email is not None:
        if por_email.tem_conta:
            tel_bate = (telefone_chave(por_email.telefone) == chave
                        and bool(chave))
            if tel_bate:
                _aplicar_perfil(por_email, sessao)
                sessao.resultado = 'login_direto'
                sessao.cliente_id = por_email.id
                return {'resultado': 'login_direto', 'cliente': por_email,
                        'email_mascarado': None}
            # (d) conta existe, telefone diverge → magic link pro e-mail.
            sessao.resultado = 'magic_link_email'
            sessao.cliente_id = por_email.id
            return {'resultado': 'magic_link_email', 'cliente': por_email,
                    'email_mascarado': _mascarar(por_email.email)}
        # Guest: upgrade se o telefone provado não conflita com o histórico.
        if _guest_tem_telefone_divergente(por_email, chave):
            sessao.resultado = 'magic_link_email'
            sessao.cliente_id = por_email.id
            return {'resultado': 'magic_link_email', 'cliente': por_email,
                    'email_mascarado': _mascarar(por_email.email)}
        por_email.senha_hash = sessao.senha_hash
        _aplicar_perfil(por_email, sessao)
        if not por_email.nome:
            por_email.nome = sessao.nome
        sessao.resultado = 'conta_criada'      # upgrade de guest = conta nova
        sessao.cliente_id = por_email.id
        return {'resultado': 'conta_criada', 'cliente': por_email,
                'email_mascarado': None}

    # (c) telefone provado já pertence a outra conta.
    if por_tel is not None:
        _aplicar_perfil(por_tel, sessao, telefone=False)
        sessao.resultado = 'login_conta_telefone'
        sessao.cliente_id = por_tel.id
        return {'resultado': 'login_conta_telefone', 'cliente': por_tel,
                'email_mascarado': _mascarar(por_tel.email)}

    # (a) tudo novo → cria a conta.
    cliente = Cliente(nome=sessao.nome, email=sessao.email,
                      telefone=sessao.telefone_validado or sessao.telefone,
                      senha_hash=sessao.senha_hash,
                      aceite_lgpd_em=agora(),
                      aniversario_dia=sessao.aniversario_dia,
                      aniversario_mes=sessao.aniversario_mes,
                      nascimento_ano=sessao.nascimento_ano)
    db.session.add(cliente)
    db.session.flush()
    sessao.resultado = 'conta_criada'
    sessao.cliente_id = cliente.id
    return {'resultado': 'conta_criada', 'cliente': cliente,
            'email_mascarado': None}


def _mascarar(email):
    """j***@gmail.com — o suficiente pro dono da conta se reconhecer."""
    try:
        user, dominio = (email or '').split('@', 1)
        return (user[0] + '•••@' + dominio) if user else '•••@' + dominio
    except ValueError:
        return '•••'


# ── Entrada do webhook (código via WhatsApp) ─────────────────────────────

def extrair_codigo(texto):
    m = RE_CODIGO_WIFI.search(texto or '')
    return ('WIFI-' + m.group(1).upper()) if m else None


# ── Vouchers (trava dura sem API — decisão do dono 12/07/2026) ──────────
# O OC200 não fala com a Open API da nuvem (ver CLAUDE.md), então o portal
# do controlador roda no modo VOUCHER: o dono gera o lote no Hotspot
# Manager, exporta e sobe em /admin/wifi-vouchers; cada cadastro validado
# consome UM voucher e o código vai na resposta do WhatsApp.

_RE_VOUCHER_LINHA = re.compile(r'^[\d\s\-]{6,20}$')


def importar_vouchers(texto, lote=None):
    """Importa vouchers de um export do Hotspot Manager (CSV/TXT — uma
    linha por voucher, aceita colunas extras). Idempotente: código já
    existente é pulado. Retorna (importados, duplicados, ignorados)."""
    from app.models import WifiVoucher
    existentes = {v.codigo for v in WifiVoucher.query.with_entities(
        WifiVoucher.codigo)}
    importados = duplicados = ignorados = 0
    vistos = set()
    for linha in (texto or '').splitlines():
        linha = linha.strip().lstrip('﻿')
        if not linha:
            continue
        codigo = None
        for campo in re.split(r'[,;\t]', linha):
            campo = campo.strip().strip('"')
            if _RE_VOUCHER_LINHA.match(campo):
                digitos = re.sub(r'\D', '', campo)
                if 6 <= len(digitos) <= 12:
                    codigo = digitos
                    break
        if not codigo:
            ignorados += 1          # cabeçalho/linha sem código
            continue
        if codigo in existentes or codigo in vistos:
            duplicados += 1
            continue
        vistos.add(codigo)
        db.session.add(WifiVoucher(codigo=codigo, lote=(lote or '')[:60]
                                   or None))
        importados += 1
    db.session.commit()
    return importados, duplicados, ignorados


def vouchers_restantes():
    from app.models import WifiVoucher
    return WifiVoucher.query.filter(WifiVoucher.usado_em.is_(None)).count()


def alocar_voucher(sessao):
    """Consome UM voucher livre pra sessão (claim atômico — UPDATE
    condicional, mesmo padrão do Confirmar do Slack). Retorna o código ou
    None (estoque vazio/regime sem voucher — o fluxo segue sem ele)."""
    from app.models import WifiVoucher
    for _ in range(5):
        candidato = (WifiVoucher.query
                     .filter(WifiVoucher.usado_em.is_(None))
                     .order_by(WifiVoucher.id)
                     .first())
        if candidato is None:
            return None
        n = (WifiVoucher.query
             .filter(WifiVoucher.id == candidato.id,
                     WifiVoucher.usado_em.is_(None))
             .update({'usado_em': agora(), 'sessao_id': sessao.id},
                     synchronize_session=False))
        if n == 1:
            return candidato.codigo
    return None


def _avisar_estoque_baixo():
    """WhatsApp pro dono quando o estoque de vouchers fica baixo (dedup
    24h em AppConfig). Best-effort: nunca quebra o fluxo do cliente."""
    from flask import current_app
    try:
        restantes = vouchers_restantes()
        minimo = int(current_app.config.get('WIFI_VOUCHER_AVISO_MIN') or 50)
        if restantes > minimo:
            return
        from datetime import datetime

        from app.models import AppConfig
        marca = AppConfig.get('wifi_voucher_alerta_em')
        if marca:
            try:
                if (agora() - datetime.fromisoformat(marca)) < \
                        timedelta(hours=24):
                    return
            except ValueError:
                pass
        numero = (current_app.config.get('ZAPI_BOT_DONO_NUMERO')
                  or '').strip()
        if not numero:
            return
        from app.services import zapi
        zapi.enviar_texto(numero, (
            f'⚠️ Wi-Fi da loja: restam só {restantes} vouchers no '
            'estoque. Gere um lote novo no Hotspot Manager do Omada e '
            'suba em /admin/wifi-vouchers.'))
        AppConfig.set('wifi_voucher_alerta_em', agora().isoformat())
        db.session.commit()
    except Exception:  # noqa: BLE001 — aviso é best-effort (docstring)
        logger.exception('wifi_portal: falha no aviso de estoque baixo')
        db.session.rollback()


def processar_codigo_whatsapp(texto, telefone_remetente):
    """Chamado pelo webhook do Chatwoot quando a mensagem carrega um código
    WIFI-XXXXXX. `telefone_remetente` = número que ENVIOU (prova de posse).

    Retorna None se o código não existe/expirou (o chamador responde um
    aviso), ou dict {'texto': resposta pro cliente, 'sessao': sessao}."""
    from flask import current_app

    from app.models import WifiPortalSessao
    codigo = extrair_codigo(texto)
    if not codigo:
        return None
    agora_dt = agora()
    sessao = (WifiPortalSessao.query
              .filter_by(codigo=codigo)
              .order_by(WifiPortalSessao.criado_em.desc())
              .first())
    if sessao is None or not sessao.pendente(agora_dt):
        return {'texto': ('Não encontrei um cadastro de Wi-Fi aguardando '
                          'esse código. Volte à tela do Wi-Fi da loja e '
                          'refaça o cadastro, por favor. 🙏'),
                'sessao': None}

    sessao.telefone_validado = telefone_remetente or sessao.telefone
    sessao.validado_em = agora_dt
    res = _resolver_conta(sessao)

    # Login one-time (30 min). No caso (d) o token vai pro E-MAIL cadastrado,
    # não pro WhatsApp — quem não tem a caixa de entrada não entra.
    sessao.login_token = secrets.token_urlsafe(32)

    # Autoriza o aparelho no controlador (best-effort).
    try:
        from app.services import omada
        r = omada.autorizar_cliente(sessao.client_mac, sessao.ap_mac,
                                    sessao.ssid)
        if r['ok']:
            sessao.wifi_autorizado_em = agora_dt
        else:
            sessao.wifi_erro = (r['erro'] or '')[:200]
    except Exception as exc:  # noqa: BLE001
        sessao.wifi_erro = str(exc)[:200]

    db.session.commit()

    base = _base_url_loja()
    link = f'{base}/loja/wifi/entrar/{sessao.login_token}'
    nome_curto = (sessao.nome or '').split(' ')[0] or 'cliente'

    if res['resultado'] == 'magic_link_email':
        _enviar_magic_link(res['cliente'], sessao)
        texto_resp = (
            f'Wi-Fi liberado, {nome_curto}! ✅\n\n'
            f'O e-mail {res["email_mascarado"]} já tem uma conta no nosso '
            'site com outro telefone. Por segurança, enviamos um link de '
            'acesso para esse e-mail — é só abrir a caixa de entrada.')
    elif res['resultado'] == 'login_conta_telefone':
        texto_resp = (
            f'Wi-Fi liberado, {nome_curto}! ✅\n\n'
            f'Encontramos uma conta sua no site (e-mail '
            f'{res["email_mascarado"]}). Toque para entrar nela:\n{link}')
    elif res['resultado'] == 'login_direto':
        texto_resp = (
            f'Wi-Fi liberado, {nome_curto}! ✅\n\n'
            'Você já tem conta no nosso site — toque para entrar sem '
            f'senha:\n{link}')
    else:   # conta_criada
        texto_resp = (
            f'Wi-Fi liberado, {nome_curto}! ✅\n\n'
            'Sua conta no site da O Pão foi criada. Toque para entrar já '
            f'logado:\n{link}\n\nBom apetite! 🥐')
    # Aviso honesto se a config do WhatsApp/portal não autorizou o wifi
    # (rede aberta em pré-enforcement: o acesso já funciona de qualquer
    # jeito, então não confundimos o cliente com detalhe técnico).
    _ = current_app  # (config já usada em _base_url_loja)
    return {'texto': texto_resp, 'sessao': sessao}


def _base_url_loja():
    from flask import current_app
    hosts = (current_app.config.get('LOJA_HOSTS')
             or 'opao.online,www.opao.online')
    primeiro = hosts.split(',')[0].strip() or 'opao.online'
    return f'https://{primeiro}'


def _enviar_magic_link(cliente, sessao):
    """Caso (d): link de login vai pro e-mail CADASTRADO via Postmark."""
    try:
        from app.services import email as email_svc
        base = _base_url_loja()
        link = f'{base}/loja/wifi/entrar/{sessao.login_token}'
        email_svc.enviar(
            cliente.email,
            'Seu acesso ao site — O Pão Padaria Artesanal',
            (f'<p>Olá, {cliente.nome or "cliente"}!</p>'
             '<p>Alguém (provavelmente você) usou o Wi-Fi da nossa loja e '
             'informou este e-mail. Para entrar na sua conta do site, toque '
             f'no link abaixo (vale por {LOGIN_TOKEN_VALIDADE_MIN} '
             'minutos):</p>'
             f'<p><a href="{link}">{link}</a></p>'
             '<p>Se não foi você, ignore este e-mail — sua conta continua '
             'protegida.</p>'),
            texto=f'Seu link de acesso: {link}')
    except Exception:  # noqa: BLE001
        logger.exception('wifi_portal: magic link por e-mail falhou')


def usar_login_token(token):
    """Valida o token one-time do link de login. Retorna (cliente, sessao)
    ou (None, None). Marca usado — segunda visita não loga."""
    from app.models import WifiPortalSessao
    if not token or len(token) < 20:
        return None, None
    agora_dt = agora()
    s = WifiPortalSessao.query.filter_by(login_token=token).first()
    if (s is None or s.login_usado_em is not None
            or s.validado_em is None or s.cliente_id is None
            or s.validado_em + timedelta(
                minutes=LOGIN_TOKEN_VALIDADE_MIN) < agora_dt):
        return None, None
    s.login_usado_em = agora_dt
    db.session.commit()
    return s.cliente, s
