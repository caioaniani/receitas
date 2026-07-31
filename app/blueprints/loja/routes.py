"""Rotas públicas da loja online (Fase 2, 16/06/2026).

Gate: enquanto `LOJA_VISIVEL=0` (padrão), visitante anônimo vê 404 — só
staff logado no admin vê. Pra cutover futuro (Fase 8): `LOJA_VISIVEL=1`
libera pra todo mundo.

Read-only nesta fase: home + página de produto. Botão "Comprar" fica
desabilitado com "Em breve" (carrinho/checkout entra na Fase 3).
"""
import json
import os

from flask import (
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user

from app.blueprints.loja import loja_bp
from app.extensions import csrf, limiter
from app.services import frete as frete_svc
from app.services import (
    loja_auth,
    loja_catalogo,
    loja_checkout,
    loja_data_especial,
    loja_pagamento,
)


def _ctx_checkout(erros=None, form=None):
    """Contexto compartilhado entre GET e POST(falha) do checkout.

    Datas viram min/max pro <input type="date"> (calendário). O range é
    contíguo (entregamos todo dia) e respeita o corte das 17h, então
    [min, max] bate exatamente com o conjunto que o servidor valida.

    Quando o cliente está logado, os campos do PAGADOR vêm pré-preenchidos
    com os dados da conta (form da requisição prevalece se tiver — não
    sobrescreve o que o cliente acabou de digitar)."""
    from app.utils import agora
    # Sob encomenda (dono 21/07/2026): se o carrinho tem item sob encomenda,
    # a 1ª data válida do calendário vira D+2 (o item precisa de antecedência)
    # e o express some. O lead é do CARRINHO (maior dos itens) — uma data de
    # entrega por pedido. O servidor re-valida em `criar_pedido`.
    lead_encomenda = loja_checkout.lead_do_carrinho(_carrinho_sessao())
    datas = loja_checkout.datas_disponiveis(
        'agendada', lead_dias=lead_encomenda)
    base = agora()
    form = dict(form or {})
    cli = loja_auth.cliente_atual()
    if cli:
        # Nome salvo (campo único) → divide em nome + sobrenome pros 2 campos
        # do checkout (1ª palavra = nome; resto = sobrenome).
        _partes = (cli.nome or '').strip().split(None, 1)
        form.setdefault('nome', _partes[0] if _partes else '')
        form.setdefault('sobrenome', _partes[1] if len(_partes) > 1 else '')
        form.setdefault('email', cli.email or '')
        form.setdefault('telefone', cli.telefone or '')
        form.setdefault('cpf', cli.cpf or '')
        # Endereço principal salvo (último que ele recebeu entrega) →
        # pré-preenche pra ele não redigitar.
        end = loja_checkout.endereco_principal(cli)
        if end:
            form.setdefault('logradouro', end.logradouro or '')
            form.setdefault('numero', end.numero or '')
            form.setdefault('complemento', end.complemento or '')
            form.setdefault('bairro', end.bairro or '')
            form.setdefault('cidade', end.cidade or '')
            form.setdefault('uf', end.uf or '')
            form.setdefault('cep', end.cep or '')
    permitida = loja_checkout.loja_retirada_permitida()
    return dict(
        em_teste=_em_teste(),
        lojas=loja_checkout.lojas_retirada(),
        # Só ESSA loja pode receber retirada (decisão do dono 19/06/2026 —
        # hoje é a Anésio Pinto Rosa). As outras aparecem desabilitadas.
        loja_retirada_permitida_id=(permitida.id if permitida else None),
        data_min=(datas[0].isoformat() if datas else ''),
        data_max=(datas[-1].isoformat() if datas else ''),
        janelas=list(loja_checkout.JANELAS_HORARIAS),
        # HORÁRIO ESPECIAL POR DATA (27/07/2026): {iso: [janelas]} pras datas
        # do calendário que têm horário diferente do normal (Dia dos Pais
        # 06:00–10:00). Lista VAZIA = dia fechado.
        #
        # Vai no payload em vez de virar endpoint AJAX porque o seletor já é
        # montado 100% no cliente a partir de `janelas` (checkout.js::
        # popularJanelas) — sem isto o site mostraria 08:00–18:00 no 09/08, o
        # cliente escolheria 12:00–13:00 e só o POST recusaria, com a mensagem
        # errada ("o horário escolhido já passou"). São no máximo 15 datas por
        # render e quase sempre nenhuma.
        janelas_por_data=loja_checkout.janelas_especiais_do_periodo(
            datas, base=base),
        # Pro JS filtrar janelas passadas quando a data escolhida é hoje
        # (usa a hora do SERVIDOR — evita divergência de relógio do cliente).
        hoje_iso=base.date().isoformat(),
        min_hora_hoje=base.hour + loja_checkout.LEAD_HORAS,
        # Express indisponível quando o carrinho tem item sob encomenda
        # (same-day conflita com D+2). O front esconde a opção; o servidor
        # também recusa.
        express_ok=(loja_checkout.express_disponivel()
                    and lead_encomenda == 0),
        # Motivo do express estar fora HOJE: sem distinguir "data especial"
        # de "fora do horário", o rótulo diria "fora do horário 8h–18h" às
        # 11h do Dia dos Pais.
        express_bloqueado_hoje=loja_data_especial.express_bloqueado_em(
            base.date()),
        encomenda_no_carrinho=(lead_encomenda > 0),
        encomenda_lead_dias=lead_encomenda,
        # Corte por distância: pro JS cortar a 1ª janela quando a cotação
        # chegar e for >= corte_km (motoboy não chega a tempo). Servidor é
        # autoridade — esses dados sao DICA do front, validação real em
        # criar_pedido.
        corte_km=loja_checkout.DISTANCIA_CORTE_PRIMEIRA_JANELA_KM,
        janelas_cortadas_longe=list(loja_checkout.JANELAS_CORTADAS_LONGE),
        express_longe_km=loja_checkout.DISTANCIA_EXPRESS_2H_KM,
        erros=erros, form=form,
    )


# ── Pagamento (Fase 4) ─────────────────────────────────────────────────
# Esquema: checkout cria o PedidoOnline -> redireciona pra /pagamento ->
# cliente escolhe Pix ou cartão -> POST chama loja_pagamento.iniciar_*.
# Webhook Pagar.me marca como pago e baixa estoque (única fonte da verdade
# de "pago", evita race com retorno do checkout).

def _pedido_aguardando(codigo):
    """Carrega pedido por código e exige status 'aguardando_pagamento'.
    Bloqueia tentar pagar de novo um pedido já pago/cancelado."""
    from app.models import PedidoOnline
    pedido = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not pedido:
        abort(404)
    return pedido


def _ctx_pagamento(pedido, erros=None):
    """Contexto da tela de pagamento. Usado no GET e nos POSTs (Pix/cartão)
    quando falham — assim a re-renderização de erro NÃO perde o pubkey nem
    o display do Pix (bug: aparecia 'PUBLIC_KEY não configurada' no erro)."""
    from flask import current_app
    pubkey = (current_app.config.get('PAGARME_PUBLIC_KEY') or '')
    pix_pendente = next((p for p in pedido.pagamentos
                         if p.metodo == 'pix' and p.status == 'pendente'),
                        None)
    pix = None
    if pix_pendente:
        qc = pix_pendente.pix_qr_code or ''
        qu = pix_pendente.pix_qr_code_url or ''
        emv = next((v for v in (qc, qu) if v and not v.startswith('http')), '')
        link = next((v for v in (qu, qc) if v and v.startswith('http')), '')
        pix = {'emv': emv, 'link': link,
               'img': loja_pagamento.pagarme.qr_data_uri(emv) if emv else None,
               'expira_em': pix_pendente.pix_expira_em}
    # Sem erro explícito, mostra o motivo da última tentativa falhada.
    if erros is None:
        ult = next((p for p in sorted(pedido.pagamentos,
                                      key=lambda x: x.criado_em or 0,
                                      reverse=True) if p.status == 'falhou'),
                   None)
        if ult and ult.erro:
            erros = [f'Última tentativa falhou: {ult.erro}']
    return dict(pedido=pedido, pubkey=pubkey, pix_pendente=pix_pendente,
                pix=pix, erros=erros or None, em_teste=_em_teste())


@loja_bp.route('/pedido/<codigo>/pagamento', methods=['GET'])
def pedido_pagamento(codigo):
    """Tela de pagamento — escolhe método e mostra o resultado."""
    pedido = _pedido_aguardando(codigo)
    return render_template('loja/pagamento.html', **_ctx_pagamento(pedido))


@loja_bp.route('/pedido/<codigo>/pix', methods=['POST'])
@limiter.limit('10 per minute')
def pedido_pix(codigo):
    """Gera Pix (QR + copia-e-cola) pro pedido. Rate limit pra evitar spam
    de tentativa de pagamento no Pagar.me (achado da auditoria 23/06/2026)."""
    pedido = _pedido_aguardando(codigo)
    if pedido.status != 'aguardando_pagamento':
        return redirect(url_for('loja.pedido_confirmado', codigo=codigo))
    pag, erros = loja_pagamento.iniciar_pix(pedido)
    if erros:
        return render_template(
            'loja/pagamento.html',
            **_ctx_pagamento(pedido, erros=erros)), 400
    return redirect(url_for('loja.pedido_pagamento', codigo=codigo))


@loja_bp.route('/pedido/<codigo>/cartao', methods=['POST'])
@limiter.limit('10 per minute')
def pedido_cartao(codigo):
    """Processa pagamento em cartão. Recebe `card_token` (já tokenizado no
    front via pk_ do Pagar.me) — servidor NUNCA vê o número do cartão.
    Rate limit pra evitar spam (achado da auditoria 23/06/2026)."""
    pedido = _pedido_aguardando(codigo)
    if pedido.status != 'aguardando_pagamento':
        return redirect(url_for('loja.pedido_confirmado', codigo=codigo))
    token = (request.form.get('card_token') or '').strip()
    # Cartão só à vista (1x) — sem parcelamento (decisão do dono 17/06/2026).
    parcelas = 1
    # Endereço de cobrança (antifraude do Pagar.me exige no charge).
    billing = {
        'line_1': (request.form.get('bill_line') or '').strip(),
        'zip_code': ''.join(c for c in (request.form.get('bill_cep') or '')
                            if c.isdigit()),
        'city': (request.form.get('bill_city') or '').strip() or 'São Paulo',
        'state': (request.form.get('bill_state') or '').strip() or 'SP',
        'country': 'BR',
    }
    pag, erros = loja_pagamento.iniciar_cartao(pedido, token, parcelas,
                                               billing=billing)
    if erros:
        return render_template(
            'loja/pagamento.html',
            **_ctx_pagamento(pedido, erros=erros)), 400
    # Cartão aprovado pelo Pagar.me: redireciona pra confirmação. A baixa
    # de estoque acontece quando chegar o webhook 'paid' (única fonte de
    # verdade — evita race).
    return redirect(url_for('loja.pedido_confirmado', codigo=codigo))


@loja_bp.route('/pedido/<codigo>/status')
def pedido_status(codigo):
    """JSON com o status do pedido — usado pelo polling da tela de Pix
    pra detectar pagamento e redirecionar pra confirmação."""
    from app.models import PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not p:
        return jsonify(status='nao_encontrado'), 404
    return jsonify(status=p.status, codigo=p.codigo)


_PAGARME_HIT_PATH = '/tmp/pagarme_webhook_ultimo.json'


def _gravar_pagarme_hit(info):
    """Best-effort: grava metadados do último hit do webhook pro debug ler.
    NUNCA vaza o segredo — só comprimentos, primeiros e últimos 4 chars
    (suficiente pra COMPARAR sem expor)."""
    import time
    try:
        info['quando_epoch'] = time.time()
        with open(_PAGARME_HIT_PATH, 'w') as f:
            json.dump(info, f)
    except OSError:
        pass


def _mascarar(s):
    """4 primeiros + len + 4 últimos. NÃO mostra o meio."""
    s = s or ''
    if len(s) <= 8:
        return {'len': len(s), 'amostra': '(curto demais p/ mascarar)'}
    return {'len': len(s), 'inicio': s[:4], 'fim': s[-4:]}


@loja_bp.route('/webhook/pagarme', methods=['POST'])
@csrf.exempt
@limiter.exempt
def webhook_pagarme():
    """Webhook do Pagar.me. Protegido por segredo na URL (?k=) — mesmo
    padrão de Chatwoot/Slack/Zapi nesse projeto.

    Idempotente (PagarmeEvento). 'order.paid'/'charge.paid' marca pago e
    baixa estoque (venda_site). Refund/cancel é IGNORADO (decisão do dono
    18/06/2026 — estorno é manual no admin). Reentrega do mesmo evento
    devolve 200 sem dupli­car efeito."""
    import hmac

    from flask import current_app
    segredo_esperado = (current_app.config.get('PAGARME_WEBHOOK_SECRET')
                        or '').strip()
    fornecido = request.args.get('k') or ''
    # Tentativa de leitura do tipo do evento (best-effort, só pro log — não
    # serve pra autenticar).
    try:
        peek = request.get_json(silent=True, cache=True) or {}
        tipo_peek = peek.get('type') or ''
    except Exception:
        tipo_peek = ''
    hit = {'tipo': tipo_peek,
           'esperado': _mascarar(segredo_esperado),
           'fornecido': _mascarar(fornecido),
           'bate': bool(segredo_esperado
                        and hmac.compare_digest(segredo_esperado, fornecido))}
    if not segredo_esperado:
        hit['status'] = 503
        _gravar_pagarme_hit(hit)
        return jsonify(ok=False, erro='webhook desabilitado'), 503
    # Comparação constante — defesa contra timing attack.
    if not hit['bate']:
        hit['status'] = 401
        _gravar_pagarme_hit(hit)
        return jsonify(ok=False, erro='unauthorized'), 401
    hit['status'] = 200
    _gravar_pagarme_hit(hit)
    evento = request.get_json(silent=True) or {}
    res = loja_pagamento.processar_webhook(evento)
    # Devolve 200 mesmo em "sem_pedido"/"ignorado" pra Pagar.me NÃO ficar
    # reentregando indefinidamente um evento que não vai processar.
    return jsonify(res), 200


def ler_ultimo_hit_pagarme():
    """Pro /admin/debug-pagarme/ultimo-webhook: último hit registrado neste
    container (best-effort; pode estar vazio se ainda não recebeu nada
    desde o último redeploy)."""
    try:
        with open(_PAGARME_HIT_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _loja_visivel_publico():
    """Lê env `LOJA_VISIVEL`. '1' = pública pra todo mundo (Fase 8).
    Qualquer outro valor = gated (só staff logado vê)."""
    return os.environ.get('LOJA_VISIVEL', '0').strip() == '1'


def _em_teste():
    """Vitrine ainda escondida do público — exibe banner 'em teste' pra
    quem vê (que necessariamente é staff logado)."""
    return not _loja_visivel_publico()


@loja_bp.app_template_filter('catslug')
def _filtro_catslug(s):
    """Slug de categoria pro id da âncora `#cat-<slug>`. Idêntico ao slug
    do dropdown (ambos via loja_catalogo._slugify) — o link pula certo."""
    return loja_catalogo._slugify(s or '')


@loja_bp.context_processor
def _injetar_contexto_loja():
    """Expõe no header de TODA página da loja:
    - `cliente_atual`: cliente logado (ou None)
    - `categorias_loja`: [{nome, slug}] pro dropdown "Produtos"
    - `loja_logo_url`: logo enviado no admin (ou None → wordmark de texto)
    - `chatwoot_widget`: {url, token} pro widget de chat (None se desligado).
    - `cookies_aceitos` / `cookies_recusados`: estado do consentimento
      LGPD (cookie `cookies_consent` = aceitar|recusar). Sem cookie, banner
      aparece; com aceite, GA4/Pixel sao carregados.
    """
    from flask import current_app

    from app.models import AppConfig
    cw_token = (current_app.config.get('CHATWOOT_WEBSITE_TOKEN') or '').strip()
    cw_url = (current_app.config.get('CHATWOOT_PUBLIC_URL') or '').strip()
    chatwoot_widget = ({'url': cw_url.rstrip('/'), 'token': cw_token}
                       if cw_token and cw_url else None)
    consent = request.cookies.get('cookies_consent') or ''
    return {
        'cliente_atual': loja_auth.cliente_atual(),
        'categorias_loja': loja_catalogo.categorias_publicadas(),
        'loja_logo_url': AppConfig.get('loja_logo_url'),
        'chatwoot_widget': chatwoot_widget,
        'cookies_aceitos': consent == 'aceitar',
        'cookies_recusados': consent == 'recusar',
    }


@loja_bp.before_request
def _gate_acesso():
    """404 silencioso pra visitante anônimo enquanto LOJA_VISIVEL=0.
    Retorna 404 (não 403) pra não confessar que a rota existe — robots/
    Google não indexa rota inexistente.

    `/loja/robots.txt` é exceção: precisa estar sempre acessível pra dizer
    pro Google "não indexa" enquanto a loja está em teste."""
    # Sempre liberadas: rotas de auth (cliente precisa entrar/cadastrar) +
    # robots + webhook do gateway + paginas legais (CDC/LGPD exigem
    # acessibilidade publica independente do estado de cutover).
    if request.endpoint in (
            'loja.robots', 'loja.webhook_pagarme',
            'loja.entrar', 'loja.cadastrar', 'loja.sair',
            'loja.esqueci_senha', 'loja.redefinir_senha',
            'loja.verificar_cadastro',
            # Paginas legais: nao podem ser 404 nem mesmo em modo teste —
            # cliente precisa conseguir ler antes de comprar (Decreto
            # 7.962/2013 Art. 2 IV). Sitemap fica fora porque so vale com
            # loja publica (a propria rota retorna 404 se !visivel).
            'loja.privacidade', 'loja.termos', 'loja.trocas', 'loja.contato',
            # Acompanhar pedido pelo CÓDIGO (link do email pra guests):
            # qualquer um com o código vê o pedido. O código é random hex 8
            # (16^8 = 4 bi) — não é adivinhável por enumeração realista.
            'loja.pedido_confirmado', 'loja.pedido_pagamento',
            'loja.pedido_status', 'loja.pedido_pix', 'loja.pedido_cartao',
            'loja.pedido_danfe',
            # API publica de disponibilidade por dia (chamada pelo JS da
            # pagina de produto). Read-only e nao expoe nada sensivel.
            'loja.api_disponibilidade_dia',
            'loja.api_disponibilidade_checkout',
            # PWA: manifest + service worker da loja. Tem que ser publico
            # pra o navegador buscar mesmo com loja em modo teste; nao
            # expoem dado sensivel (manifest e estatico; SW e script).
            'loja.pwa_manifest_loja',
            'loja.pwa_service_worker_loja',
    ):
        return None
    from app.utils import host_atual_eh_loja
    # Público (sem login) SÓ no domínio público da loja (opao.online). No
    # gestao.*/loja a mesma loja responde só pra admin logado — assim o
    # cliente usa a porta da frente (opao.online) e o gestao.* não vira uma
    # segunda URL pública/indexável da mesma vitrine (conteúdo duplicado).
    if _loja_visivel_publico() and host_atual_eh_loja():
        return None  # loja pública no domínio público
    if current_user.is_authenticated:
        return None  # staff logado vê pra testar (em qualquer host)
    if loja_auth.cliente_atual():
        return None  # cliente logado pode entrar mesmo em modo teste
    abort(404)


# ── Auth do cliente (Fase 6) ───────────────────────────────────────────
# Sessão SEPARADA do admin: cliente usa `cliente_id` na sessão; staff usa
# `_user_id` do Flask-Login. Nunca cruzam — privilégio NÃO escala.

@loja_bp.route('/entrar', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def entrar():
    """Login do cliente final. Rate limit (5/min) trava brute-force de senha
    sem atrapalhar cliente que erra a digitação — espelha o admin."""
    if loja_auth.cliente_atual():
        return redirect(loja_auth.safe_next() or url_for('loja.minha_conta'))
    erro = None
    email = ''
    if request.method == 'POST':
        from app.extensions import db
        from app.models import Cliente
        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''
        c = Cliente.query.filter(
            db.func.lower(Cliente.email) == email).first()
        if c and c.check_senha(senha) and c.ativo:
            loja_auth.login_cliente(c)
            return redirect(loja_auth.safe_next()
                            or url_for('loja.minha_conta'))
        erro = 'Email ou senha incorretos.'
    return render_template('loja/entrar.html',
                           em_teste=_em_teste(), erro=erro, email=email), \
        (400 if erro else 200)


@loja_bp.route('/cadastrar', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def cadastrar():
    """Cadastro de cliente.

    Dois caminhos pra evitar sequestro de pedido feito como guest:

    - E-mail NOVO (sem Cliente): cria conta + login direto. Zero atrito.
    - E-mail JÁ EXISTENTE como guest (Cliente sem senha): NÃO vincula na
      hora — manda link de verificação pro próprio e-mail. Só quem lê o
      e-mail consegue ativar a conta e ver o histórico/PII do pedido
      anterior. Atrito focado nos ~5% dos casos onde havia risco real.
    """
    if loja_auth.cliente_atual():
        return redirect(url_for('loja.minha_conta'))
    erros, form, verificacao_enviada = [], {}, False
    if request.method == 'POST':
        from app.extensions import db
        from app.models import Cliente
        from app.utils import agora
        form = request.form
        nome = (form.get('nome') or '').strip()
        email = (form.get('email') or '').strip().lower()
        telefone = (form.get('telefone') or '').strip()
        senha = form.get('senha') or ''
        if not nome:
            erros.append('Informe seu nome.')
        if not loja_auth.email_valido(email):
            erros.append('Informe um email válido.')
        if len(senha) < 8:
            erros.append('A senha precisa ter ao menos 8 caracteres.')
        if form.get('aceite_lgpd') not in ('1', 'on', 'true'):
            erros.append('É preciso aceitar os termos para criar a conta.')
        if not erros:
            c = Cliente.query.filter(
                db.func.lower(Cliente.email) == email).first()
            if c and c.senha_hash:
                erros.append('Já existe uma conta com esse email. '
                             'Tente entrar.')
            elif c:
                # Guest reivindicando a conta: NÃO loga direto — manda link
                # de verificação. Defende contra sequestro por e-mail
                # adivinhado (atacante não recebe o e-mail da vítima).
                loja_auth.iniciar_verificacao_cadastro(
                    c, nome, telefone, senha)
                verificacao_enviada = True
            else:
                # E-mail novo: cadastro instantâneo (mesmo de antes).
                c = Cliente(nome=nome, email=email, telefone=telefone)
                db.session.add(c)
                c.set_senha(senha)
                c.aceite_lgpd_em = c.aceite_lgpd_em or agora()
                db.session.commit()
                loja_auth.login_cliente(c)
                return redirect(url_for('loja.minha_conta'))
    return render_template('loja/cadastrar.html',
                           em_teste=_em_teste(),
                           erros=erros, form=form,
                           verificacao_enviada=verificacao_enviada), \
        (400 if erros else 200)


@loja_bp.route('/verificar-cadastro/<token>', methods=['GET'])
def verificar_cadastro(token):
    """Link do e-mail de verificação. Valida o token e ativa a conta:
    promove os dados pendentes (nome/telefone/senha_hash) pro Cliente,
    loga e redireciona pra minha-conta. Inválido/expirado: pede pra
    cadastrar de novo."""
    res = loja_auth.aplicar_verificacao(token)
    if not res['ok']:
        return render_template('loja/verificar_cadastro.html',
                               em_teste=_em_teste(), erro=res['erro']), 400
    loja_auth.login_cliente(res['cliente'])
    return redirect(url_for('loja.minha_conta'))


@loja_bp.route('/sair', methods=['POST'])
def sair():
    """Logout do cliente. POST pra não disparar por link/prefetch."""
    loja_auth.logout_cliente()
    return redirect(url_for('loja.home'))


@loja_bp.route('/esqueci-senha', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def esqueci_senha():
    """Form pra pedir o link de redefinição. Anti-enumeração: SEMPRE
    devolve a mesma mensagem (existindo ou não a conta)."""
    enviado = False
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        loja_auth.iniciar_reset(email)
        enviado = True
    return render_template('loja/esqueci_senha.html',
                           em_teste=_em_teste(), enviado=enviado)


@loja_bp.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def redefinir_senha(token):
    """Form pra definir nova senha. Valida token; aplica."""
    reg = loja_auth.token_reset_valido(token)
    if not reg:
        return render_template('loja/redefinir_senha.html',
                               em_teste=_em_teste(),
                               token=token, invalido=True), 400
    erro = None
    if request.method == 'POST':
        nova = request.form.get('senha') or ''
        confirmar = request.form.get('confirmar_senha') or ''
        if nova != confirmar:
            erro = 'As senhas não batem. Confira e tente de novo.'
        else:
            res = loja_auth.aplicar_reset(token, nova)
            if res['ok']:
                loja_auth.login_cliente(res['cliente'])
                return redirect(url_for('loja.minha_conta'))
            erro = res['erro']
    return render_template('loja/redefinir_senha.html',
                           em_teste=_em_teste(), token=token, erro=erro), \
        (400 if erro else 200)


@loja_bp.route('/conta')
@loja_auth.cliente_required
def minha_conta():
    """Painel da conta do cliente."""
    return render_template('loja/minha_conta.html',
                           cliente=loja_auth.cliente_atual(),
                           em_teste=_em_teste())


@loja_bp.route('/conta/pedidos')
@loja_auth.cliente_required
def meus_pedidos():
    """Lista os pedidos do cliente (só os dele — não vaza dos outros)."""
    from app.models import PedidoOnline
    cli = loja_auth.cliente_atual()
    pedidos = (PedidoOnline.query
               .filter_by(cliente_id=cli.id)
               .order_by(PedidoOnline.criado_em.desc())
               .all())
    return render_template('loja/meus_pedidos.html',
                           pedidos=pedidos, em_teste=_em_teste())


@loja_bp.route('/conta/pedidos/<codigo>')
@loja_auth.cliente_required
def meu_pedido(codigo):
    """Detalhe de UM pedido. 404 se não for do cliente — assim o código não
    confessa a existência de pedidos de OUTROS (enumeração)."""
    from app.models import PedidoOnline
    cli = loja_auth.cliente_atual()
    p = PedidoOnline.query.filter_by(codigo=codigo,
                                     cliente_id=cli.id).first()
    if not p:
        abort(404)
    return render_template('loja/meu_pedido.html', pedido=p,
                           em_teste=_em_teste())


@loja_bp.route('/conta/dados.json')
@loja_auth.cliente_required
def exportar_dados():
    """LGPD: exporta os dados do cliente (perfil + endereços + pedidos) em
    JSON. Direito de portabilidade (Art. 18, V)."""
    from flask import jsonify

    from app.models import EnderecoCliente, PedidoOnline
    cli = loja_auth.cliente_atual()
    ends = EnderecoCliente.query.filter_by(cliente_id=cli.id).all()
    pedidos = PedidoOnline.query.filter_by(cliente_id=cli.id).all()
    out = {
        'perfil': {
            'nome': cli.nome, 'email': cli.email, 'telefone': cli.telefone,
            'cpf': cli.cpf, 'aceite_lgpd_em': cli.aceite_lgpd_em.isoformat()
            if cli.aceite_lgpd_em else None,
            'criado_em': cli.criado_em.isoformat() if cli.criado_em else None,
        },
        'enderecos': [
            {'apelido': e.apelido, 'cep': e.cep, 'logradouro': e.logradouro,
             'numero': e.numero, 'complemento': e.complemento,
             'bairro': e.bairro, 'cidade': e.cidade, 'uf': e.uf,
             'principal': e.principal}
            for e in ends
        ],
        'pedidos': [
            {'codigo': p.codigo, 'criado_em':
             p.criado_em.isoformat() if p.criado_em else None,
             'status': p.status,
             'valor_total': float(p.valor_total or 0),
             'modo_entrega': p.modo_entrega,
             'endereco_entrega': p.endereco_entrega,
             'cartinha': p.cartinha,
             'itens': [{'nome': i.nome, 'quantidade': i.quantidade,
                        'preco_unitario': float(i.preco_unitario)}
                       for i in p.itens]}
            for p in pedidos
        ],
    }
    resp = jsonify(out)
    resp.headers['Content-Disposition'] = (
        f'attachment; filename=opao-meus-dados-{cli.id}.json')
    return resp


@loja_bp.route('/conta/excluir', methods=['POST'])
@loja_auth.cliente_required
def excluir_conta():
    """LGPD: exclui a conta do cliente (Art. 18, VI). Anonimiza os PEDIDOS
    em vez de apagar — o histórico fiscal precisa existir (NF emitida não
    pode sumir), mas tiramos as PII (nome/email/telefone/CPF). Endereços
    salvos vão embora junto."""
    from app.extensions import db
    from app.models import Cliente, EnderecoCliente, PedidoOnline
    confirma = (request.form.get('confirmar') or '').strip().upper()
    if confirma != 'EXCLUIR':
        from flask import flash
        flash('Para confirmar, digite EXCLUIR exatamente.', 'warning')
        return redirect(url_for('loja.minha_conta'))
    cli = loja_auth.cliente_atual()
    # Anonimiza os pedidos (mantém histórico fiscal)
    rotulo = f'[Conta excluída #{cli.id}]'
    PedidoOnline.query.filter_by(cliente_id=cli.id).update({
        'nome_cliente': rotulo, 'email_cliente': '',
        'telefone_cliente': '',
        'nome_destinatario': None, 'telefone_destinatario': None,
        'cliente_id': None,
    })
    EnderecoCliente.query.filter_by(cliente_id=cli.id).delete()
    # Anonimiza o Cliente em si mas mantém a linha (FK histórica resolvida
    # acima já fez setar pedido.cliente_id=NULL).
    Cliente.query.filter_by(id=cli.id).update({
        'nome': rotulo, 'email': f'excluida-{cli.id}@anonimo.local',
        'telefone': None, 'cpf': None, 'senha_hash': None, 'ativo': False,
    })
    db.session.commit()
    loja_auth.logout_cliente()
    return redirect(url_for('loja.home'))


@loja_bp.route('/conta/pedidos/<codigo>/nf')
@loja_auth.cliente_required
def meu_pedido_danfe(codigo):
    """Redireciona pro DANFE (PDF) do pedido. Mesmo escopo do detalhe:
    404 se o pedido não for do cliente (segurança PII)."""
    from app.models import PedidoOnline
    from app.services import tiny_nf
    cli = loja_auth.cliente_atual()
    p = PedidoOnline.query.filter_by(codigo=codigo,
                                     cliente_id=cli.id).first()
    if not p:
        abort(404)
    url = tiny_nf.link_danfe(p)
    if not url:
        from flask import flash
        flash('A nota fiscal deste pedido ainda não está disponível.',
              'warning')
        return redirect(url_for('loja.meu_pedido', codigo=codigo))
    return redirect(url)


@loja_bp.route('/')
def home():
    itens = loja_catalogo.anotar_esgotado(loja_catalogo.produtos_publicados())
    grupos = loja_catalogo.por_categorias(itens)
    return render_template(
        'loja/home.html',
        grupos=grupos, total_itens=len(itens), em_teste=_em_teste(),
    )


@loja_bp.route('/carrinho')
def carrinho():
    """Página do carrinho. O estado vive na SESSÃO do servidor (fonte de
    verdade — não some quando o navegador descarta o storage); o carrinho.js
    inicializa a partir do carrinho_sessao injetado. Persiste no banco só no
    checkout (cria PedidoOnline).

    LINK DE 1 CLIQUE (`?add=r5:2,p83:1`): o servidor resolve cada item (preço +
    estoque REAIS, autoritativo), MESCLA no carrinho da sessão e redireciona pra
    URL limpa (PRG — um refresh não soma de novo). `r`=receita, `p`=produto."""
    add = (request.args.get('add') or '').strip()
    if add:
        novos, esgotados = _resolver_prefill_carrinho(add)
        # Menu configurável: o link de 1 clique leva a PRÉ-SELEÇÃO. Sem a
        # `comp` a chave da linha ficaria vazia e o mesmo menu apareceria em
        # DUAS linhas no carrinho (a do link e a da página do produto).
        _set_carrinho_sessao(_carrinho_sessao() + [
            {'kind': i['kind'], 'id': i['id'], 'qtd': i['qtd'],
             'comp': (i.get('menu') or {}).get('comp_padrao')}
            for i in novos])
        if esgotados:
            session['_carrinho_esg'] = esgotados
        return redirect(url_for('loja.carrinho'))
    esgotados = session.pop('_carrinho_esg', [])
    return render_template('loja/carrinho.html', em_teste=_em_teste(),
                           prefill_esgotados=esgotados)


def _resolver_prefill_carrinho(add):
    """`add` = 'r5:2,p83:1' (ou 'r5' = qtd 1). Devolve
    ([{kind,id,nome,preco,imagem,categoria,qtd}], [nomes_esgotados]).

    Resolve no SERVIDOR (preço/estoque do nosso catálogo) — o cliente nunca
    dita preço. Item inexistente/não-publicado é ignorado; esgotado entra na
    lista de avisos (some do carrinho mas o cliente fica sabendo)."""
    itens, esgotados, vistos = [], [], set()
    for parte in add.split(','):
        token, _sep, q = parte.strip().partition(':')
        token = token.strip().lower()
        if len(token) < 2 or token[0] not in ('r', 'p'):
            continue
        kind = 'receita' if token[0] == 'r' else 'produto'
        try:
            iid = int(token[1:])
        except ValueError:
            continue
        qtd = int(q) if q.strip().isdigit() else 1
        qtd = max(1, min(qtd, 99))
        chave = (kind, iid)
        if chave in vistos:
            continue
        vistos.add(chave)
        item = loja_catalogo.por_id_publicado(kind, iid)
        if not item:
            continue
        if not loja_catalogo.tem_estoque_site(kind, iid):
            esgotados.append(item['nome'])
            continue
        itens.append({
            'kind': kind, 'id': iid, 'nome': item['nome'],
            'preco': item['preco'], 'imagem': item.get('imagem') or '',
            'categoria': item.get('categoria') or '', 'qtd': qtd,
            # Menu configurável: carrega a pré-seleção pra a linha do
            # carrinho nascer com a MESMA chave das outras portas.
            'menu': item.get('menu'),
        })
    return itens, esgotados


# ── Carrinho na SESSÃO (fonte de verdade) ────────────────────────────────────
# O carrinho vive na sessão do servidor (cookie assinado, HttpOnly) — não no
# localStorage. Assim ele NÃO some quando o Safari/iPhone descarta o storage do
# navegador. Guarda só {kind,id,qtd} (cabe no cookie); preço/nome/estoque são
# resolvidos no servidor a cada render (autoritativo).
_CARRINHO_MAX_ITENS = 60  # teto pra caber no cookie de sessão (~4KB)
# Orçamento de pares [pi_id, qtd] da composição de MENUS somados no carrinho
# inteiro (26/07/2026). Cada par custa ~8 bytes no cookie; 120 pares ≈ 1KB,
# folgado dentro dos ~4KB. Um menu real tem ~6 slots, então isso é ~20 menus
# de composições DIFERENTES — muito além do uso real. Linha que estouraria o
# orçamento é RECUSADA inteira (nunca entra sem a composição: sem ela o
# servidor cairia na pré-seleção e o cliente receberia outra coisa).
_CARRINHO_MAX_PARES_COMP = 120


def _comp_normalizada(bruto):
    """[[pi_id, qtd], ...] saneado (só pares de inteiros positivos), ou None.
    Formato compacto do menu configurável no cookie de sessão — quem valida
    contra o CADASTRO (slot pertence ao menu? total bate?) é o
    `loja_menu`/`montar_itens`; aqui é só higiene de tipo."""
    if not isinstance(bruto, (list, tuple)):
        return None
    out = []
    for par in bruto:
        if not isinstance(par, (list, tuple)) or len(par) != 2:
            continue
        try:
            pi_id, qtd = int(par[0]), int(par[1])
        except (TypeError, ValueError):
            continue
        if pi_id > 0 and qtd > 0:
            out.append([pi_id, qtd])
    return out or None


def _carrinho_sessao():
    """Lista normalizada [{kind,id,qtd,fatiado,comp}] do carrinho na sessão.

    `fatiado` (sourdough, 16/07/2026) e `comp` (menu configurável,
    26/07/2026) são preservados — sem isso a escolha do cliente some antes
    do checkout (a sessão é a fonte de verdade)."""
    out = []
    for it in session.get('carrinho') or []:
        kind = str((it or {}).get('kind') or '').strip().lower()
        if kind not in ('receita', 'produto'):
            continue
        try:
            iid = int(it.get('id'))
            qtd = max(1, min(int(it.get('qtd') or 1), 99))
        except (TypeError, ValueError):
            continue
        out.append({'kind': kind, 'id': iid, 'qtd': qtd,
                    'fatiado': bool(it.get('fatiado')),
                    'comp': _comp_normalizada(it.get('comp'))})
    return out


def _set_carrinho_sessao(itens):
    """Grava o carrinho (substitui) na sessão — validado, com qtd de repetidos
    somada e teto de itens. Devolve a lista normalizada. `fatiado` e a
    composição do MENU entram na chave de dedup: fatiado e inteiro do mesmo
    item — ou dois menus montados DIFERENTE — são linhas separadas (senão
    somariam quantidade numa linha só e o cliente receberia a composição
    errada)."""
    norm, idx = [], {}
    pares_comp = 0
    for it in itens or []:
        kind = str((it or {}).get('kind') or '').strip().lower()
        if kind not in ('receita', 'produto'):
            continue
        try:
            iid = int(it.get('id'))
            qtd = max(1, min(int(it.get('qtd') or 1), 99))
        except (TypeError, ValueError):
            continue
        fatiado = bool(it.get('fatiado'))
        comp = _comp_normalizada(it.get('comp'))
        # Chave da composição pelo helper CANÔNICO (`loja_menu.chave`); o
        # `carrinho.js::_chaveComp` espelha a mesma regra. Reimplementar aqui
        # era a duplicação que o CLAUDE.md manda evitar — divergir faria duas
        # composições diferentes somarem na mesma linha.
        from app.services import loja_menu as _lm
        chave = (kind, iid, fatiado,
                 _lm.chave({p[0]: p[1] for p in (comp or [])}))
        if chave in idx:
            norm[idx[chave]]['qtd'] = min(99, norm[idx[chave]]['qtd'] + qtd)
            continue
        if len(norm) >= _CARRINHO_MAX_ITENS:
            continue
        if comp and pares_comp + len(comp) > _CARRINHO_MAX_PARES_COMP:
            current_app.logger.warning(
                'carrinho: composição de menu recusada (orçamento de cookie '
                'estourado) — item %s:%s', kind, iid)
            continue
        pares_comp += len(comp or [])
        idx[chave] = len(norm)
        linha = {'kind': kind, 'id': iid, 'qtd': qtd, 'fatiado': fatiado}
        # `comp` só entra quando existe: um `"comp":null` em cada uma das 60
        # linhas possíveis desperdiçaria ~700 bytes dos ~4KB do cookie.
        if comp:
            linha['comp'] = comp
        norm.append(linha)
    session['carrinho'] = norm
    session.modified = True
    return norm


def _resolver_carrinho_sessao():
    """Resolve o carrinho da sessão →
    [{kind,id,nome,preco,imagem,categoria,qtd,fatiado}] pro carrinho.js
    renderizar. Dropa item inexistente/despublicado (sem display); mantém
    esgotado (o checkout avisa). Best-effort: nunca quebra a página."""
    from app.models import Produto
    from app.services import loja_menu
    out = []
    try:
        for it in _carrinho_sessao():
            item = loja_catalogo.por_id_publicado(it['kind'], it['id'])
            if not item:
                continue
            # Menu configurável: preço e "o que vem" saem da escolha DESTA
            # linha, pela MESMA rota do checkout — `normalizar` +  `preco`,
            # com `comp` do jeito que estiver (None inclusive).
            #
            # Antes só recalculava QUANDO havia `comp`; linha sem `comp`
            # (sessão antiga, carrinho.js velho no cache, quick-add do card)
            # exibia o `item['preco']`, que virou o MÍNIMO "a partir de",
            # enquanto o `montar_itens` cobrava a PRÉ-SELEÇÃO. O cliente via
            # R$ 300 e pagava R$ 360 — achado de revisão 26/07/2026,
            # dinheiro tem peso especial. Agora carrinho e checkout são o
            # mesmo cálculo por construção.
            preco, comp_resumo, remontar = item['preco'], None, False
            if item.get('menu'):
                prod = Produto.query.get(it['id'])
                if loja_menu.eh_menu(prod):
                    comp = loja_menu.normalizar(prod, it.get('comp'))
                    p = loja_menu.preco(prod, comp)
                    if p is not None:
                        preco = float(p)
                        comp_resumo = loja_menu.resumo(prod, comp)
                    else:
                        # Escolha invalidada (o admin editou a composição
                        # depois): NÃO dá pra precificar. Marca a linha pra
                        # o cliente remontar — o checkout recusaria assim
                        # mesmo, e mostrar um preço qualquer aqui mentiria.
                        remontar = True
            out.append({
                'kind': it['kind'], 'id': it['id'], 'nome': item['nome'],
                'preco': preco, 'imagem': item.get('imagem') or '',
                'categoria': item.get('categoria') or '', 'qtd': it['qtd'],
                'fatiado': it['fatiado'],
                'comp': it.get('comp'), 'comp_resumo': comp_resumo,
                'remontar': remontar,
                # `fatiavel` diz se o carrinho mostra o checkbox 'fatiado'
                # na linha (só sourdough). Item não-fatiável não vira fatiado
                # nem por toggle (o servidor re-sanitiza no checkout).
                'fatiavel': bool(item.get('fatiavel')),
            })
    except Exception:  # noqa: BLE001 — carrinho nunca derruba a loja
        return []
    return out


@loja_bp.context_processor
def _inject_carrinho_sessao():
    """Injeta o carrinho resolvido em TODA página da loja (badge + drawer vivem
    no _base.html). O carrinho.js inicializa o estado a partir disto."""
    return {'carrinho_sessao': _resolver_carrinho_sessao()}


@loja_bp.route('/api/carrinho', methods=['POST'])
def api_carrinho_salvar():
    """Grava o carrinho na sessão (fonte de verdade) — o carrinho.js chama a
    cada mudança. Recebe {itens:[{kind,id,qtd}]}, substitui, devolve a contagem."""
    data = request.get_json(silent=True) or {}
    norm = _set_carrinho_sessao(data.get('itens') or [])
    return jsonify(ok=True, count=sum(i['qtd'] for i in norm), itens=norm)


@loja_bp.route('/checkout', methods=['GET', 'POST'])
def checkout():
    """Checkout do site. GET serve o formulário; POST cria o PedidoOnline.

    O carrinho vem da SESSÃO (fonte de verdade). Fallback no itens_json do form
    só pra clientes em transição com carrinho ainda no navegador. A integridade
    de dinheiro é do SERVIDOR: loja_checkout.criar_pedido re-busca preço no
    catálogo e recomputa o frete — nunca confia no que o cliente mandou.
    PRG: sucesso redireciona pra confirmação e esvazia o carrinho da sessão."""
    if request.method == 'POST':
        itens_raw = _carrinho_sessao()
        if not itens_raw:
            try:
                itens_raw = json.loads(request.form.get('itens_json') or '[]')
            except ValueError:
                itens_raw = []
        pedido, erros = loja_checkout.criar_pedido(request.form, itens_raw)
        if not erros:
            # client_id do GA4 (cookie `_ga`, primeira parte) — permite o
            # purchase server-side deduplicar com o evento do navegador.
            # Best-effort: sem cookie (consentimento negado) fica NULL.
            try:
                from app.extensions import db
                from app.services.analytics_server import ga_client_id_do_cookie
                cid = ga_client_id_do_cookie(request.cookies.get('_ga'))
                if cid:
                    pedido.ga_client_id = cid
                    db.session.commit()
            except Exception:  # noqa: BLE001 — analytics nunca trava o checkout
                current_app.logger.exception('checkout: captura do _ga falhou')
            session.pop('carrinho', None)  # pedido criado → carrinho zerado
            return redirect(url_for('loja.pedido_pagamento',
                                    codigo=pedido.codigo))
        return render_template(
            'loja/checkout.html',
            **_ctx_checkout(erros=erros, form=request.form)), 400
    return render_template('loja/checkout.html', **_ctx_checkout())


@loja_bp.route('/api/cep/<cep>', methods=['GET'])
@limiter.limit('30 per minute')
def api_cep(cep):
    """Lookup de CEP pra autocompletar logradouro/bairro/cidade/UF no
    checkout. BrasilAPI primeiro; se ELA falhar (fora do ar/timeout), cai
    no ViaCEP — a BrasilAPI já degradou em produção mais de uma vez
    (05/07 e 09/07/2026) e, com o checkout CEP-first (campos travados até
    o CEP resolver), a rota fora do ar viraria venda travada.

    Distinção que o front usa: 404 = CEP NÃO EXISTE (cliente confere o
    número; campos seguem travados) × 502 = INFRA fora (front destrava os
    campos pra digitação manual — fail-open, venda nunca fica presa)."""
    import requests
    cep_d = ''.join(c for c in (cep or '') if c.isdigit())
    if len(cep_d) != 8:
        return jsonify(ok=False, erro='CEP precisa ter 8 dígitos.'), 400
    brasilapi_404 = False
    try:
        r = requests.get(
            f'https://brasilapi.com.br/api/cep/v2/{cep_d}', timeout=6)
        if r.status_code == 200:
            j = r.json() or {}
            return jsonify(ok=True,
                           logradouro=j.get('street') or '',
                           bairro=j.get('neighborhood') or '',
                           cidade=j.get('city') or '',
                           uf=j.get('state') or '')
        # SÓ o 404 é evidência de "CEP não existe" (BrasilAPI agrega 3
        # provedores) — ainda tenta o ViaCEP (bases divergem), mas se ele
        # também falhar por INFRA o veredito é 404, não 502. Qualquer OUTRO
        # não-200 (429/5xx) é degradação de INFRA: cai no ViaCEP e, falhando
        # os dois, vira 502 (fail-open no front) — pego em revisão: tratar
        # 5xx como "não existe" reproduzia o fail-closed que o CEP-first
        # veio eliminar.
        if r.status_code == 404:
            brasilapi_404 = True
    except Exception:  # noqa: BLE001
        pass
    try:
        r2 = requests.get(
            f'https://viacep.com.br/ws/{cep_d}/json/', timeout=6)
        if r2.status_code == 200:
            j2 = r2.json() or {}
            if j2.get('erro'):
                return jsonify(ok=False, erro='CEP não encontrado.'), 404
            return jsonify(ok=True,
                           logradouro=j2.get('logradouro') or '',
                           bairro=j2.get('bairro') or '',
                           cidade=j2.get('localidade') or '',
                           uf=j2.get('uf') or '')
    except Exception:  # noqa: BLE001
        pass
    if brasilapi_404:
        return jsonify(ok=False, erro='CEP não encontrado.'), 404
    return jsonify(ok=False, erro='Não consegui consultar o CEP.'), 502


@loja_bp.route('/api/frete', methods=['POST'])
@limiter.limit('30 per minute')
def api_frete():
    """Cotação de frete pro checkout (anéis de distância do frete.py).
    Recebe JSON {endereco, cep}; devolve o dict do consultar_frete.
    Mesma fonte que o servidor usa no POST do checkout (autoritativo)."""
    data = request.get_json(silent=True) or request.form
    endereco = (data.get('endereco') or '').strip()
    cep = (data.get('cep') or '').strip()
    geo = endereco
    if cep and cep not in endereco:
        geo = f'{endereco}, {cep}' if endereco else cep
    if not geo:
        return jsonify(ok=False, erro='Informe o endereço ou o CEP.'), 400
    res = frete_svc.consultar_frete(geo)
    from app.services import frete_sensor, loja_alerta
    if not res.get('ok'):
        codigo = res.get('erro')
        if codigo == 'nao_encontrado':
            # Endereço não localizado = venda que pode ter sido perdida: alerta
            # o dono (WhatsApp) + registra no sensor (async/best-effort).
            loja_alerta.alertar_endereco_falho(endereco or geo, cep)
            frete_sensor.registrar('preview', 'barrado',
                                   endereco=endereco or geo, cep=cep)
        # Traduz o código de máquina ('nao_encontrado') pra mensagem — o JS
        # mostra `data.erro` cru pro cliente (checkout.js). Mesma fonte que o
        # POST do checkout usa (loja_checkout._frete_para).
        res = dict(res, erro=frete_svc.mensagem_erro(codigo))
    elif res.get('fora_area'):
        # Além do raio = venda barrada. Painel registra TODOS; WhatsApp pra
        # quem ficou perto da borda (decisão do dono 09/07 — "quase comprou")
        # OU quando o km é INCERTO (impreciso = veio do centroide do CEP, pode
        # estar dentro da área na verdade — decisão do dono 09/07 pós-revisão).
        km = res.get('distancia_km')
        frete_sensor.registrar('preview', 'fora_area', endereco=endereco or geo,
                               cep=cep, fonte=res.get('fonte'), km=km)
        perto = km is not None and km <= frete_svc.RAIO_MAX_KM + frete_svc.MARGEM_ALERTA_FORA_KM
        if perto or res.get('impreciso'):
            loja_alerta.alertar_endereco_falho(endereco or geo, cep,
                                               motivo='fora_area')
    elif res.get('impreciso'):
        # Cotou só pelo centroide do CEP — a venda passa, mas o frete pode
        # estar errado: alerta o dono pra conferir (decisão do dono 09/07).
        loja_alerta.alertar_endereco_falho(endereco or geo, cep, motivo='impreciso')
        frete_sensor.registrar('preview', 'impreciso', endereco=endereco or geo,
                               cep=cep, fonte=res.get('fonte'),
                               km=res.get('distancia_km'), valor=res.get('valor'))
    return jsonify(res)


@loja_bp.route('/pedido/<codigo>/nf')
def pedido_danfe(codigo):
    """Redireciona pro DANFE (PDF) do pedido. Escopado pelo código (o cliente
    veio do email com o link); sem login pra guest funcionar."""
    from app.models import PedidoOnline
    from app.services import tiny_nf
    p = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not p:
        abort(404)
    url = tiny_nf.link_danfe(p)
    if not url:
        from flask import flash
        flash('A nota fiscal deste pedido ainda não está disponível.',
              'warning')
        return redirect(url_for('loja.pedido_confirmado', codigo=codigo))
    return redirect(url)


@loja_bp.route('/pedido/<codigo>')
def pedido_confirmado(codigo):
    """Confirmação do pedido (PRG). Também é a base do 'meus pedidos'
    (Fase 6). Rota com segmento estático 'pedido' — não colide com
    /<slug_completo> (profundidade diferente)."""
    from app.models import PedidoOnline
    pedido = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not pedido:
        abort(404)
    # Payload do evento `purchase` do GA4 (fecha o funil: visita → carrinho →
    # checkout → COMPRA). Só conta como venda quando o pagamento aconteceu —
    # Pix pendente/cancelado NÃO dispara (no Pix, a página recarrega ao
    # confirmar e aí o evento sobe). Disparado no template, 1x por pedido.
    ga_purchase = None
    if pedido.status not in ('aguardando_pagamento', 'cancelado'):
        ga_purchase = {
            'transaction_id': pedido.codigo,
            'value': float(pedido.valor_total or 0),
            'shipping': float(pedido.frete_valor or 0),
            'currency': 'BRL',
            'items': [{
                'item_id': f'{it.kind}_{it.receita_id or it.produto_id or ""}',
                'item_name': it.nome,
                'price': float(it.preco_unitario or 0),
                'quantity': it.quantidade,
            } for it in pedido.itens],
        }
    return render_template('loja/pedido_confirmado.html', pedido=pedido,
                           ga_purchase=ga_purchase, em_teste=_em_teste())


@loja_bp.route('/<slug_completo>')
def produto(slug_completo):
    """URL canônica: `/loja/<slug>-<r|p><id>` (ex: sourdough-tradicional-r12).
    Se o slug não bate o nome atual (item foi renomeado), 301 pro slug certo."""
    kind, item_id, slug_recebido = loja_catalogo.parse_slug_id(slug_completo)
    if not kind or not item_id:
        abort(404)
    item = loja_catalogo.por_id_publicado(kind, item_id)
    if not item:
        abort(404)
    # 3 estados (22/06/2026 — etapa 4 plano por dia): `esgotado` so vira True
    # se NAO tiver saldo em NENHUM dia da janela. Esgotado-hoje-mas-disponivel-
    # em-outro-dia mostra o seletor de data direto, em vez do bloqueio "Esgotado
    # no momento". `anotar_esgotado` faz o calculo (plano > EstoqueLoja).
    loja_catalogo.anotar_esgotado([item])
    # Slug desatualizado → 301 pra canônica (SEO + URLs sempre limpas)
    if slug_recebido != item['slug']:
        return redirect(url_for('loja.produto',
                                 slug_completo=item['href'].split('/loja/')[-1]),
                         code=301)
    # Cesta personalizada: cliente monta a cesta adicionando outros itens
    # do catálogo (sem ser cesta) ao carrinho junto. (rodada C — 17/06/2026)
    personalizada = loja_catalogo.eh_personalizada(item)
    monte = (loja_catalogo.itens_para_montar(excluir_item=item)
             if personalizada else [])
    # Datas pro seletor de disponibilidade (janela de 14 dias). Default = HOJE
    # se ainda tem saldo, senao PRIMEIRO dia futuro com saldo (cliente nao
    # precisa ficar trocando o seletor sozinho ate achar uma data viavel).
    from datetime import date, timedelta

    from app.utils import hoje
    data_hoje = hoje()
    # SOB ENCOMENDA (D+2): o item é produzido pro pedido, então a primeira
    # data possível é `hoje + ENCOMENDA_LEAD_DIAS` — a MESMA regra que o
    # checkout aplica (`loja_checkout.datas_disponiveis(lead_dias=…)`).
    # Sem isso o seletor abria em HOJE dizendo "✓ disponível pra essa data"
    # e o cliente só descobria o bloqueio no checkout (26/07/2026).
    lead = (loja_checkout.ENCOMENDA_LEAD_DIAS
            if item.get('sob_encomenda') else 0)
    data_min = data_hoje + timedelta(days=lead)
    data_max = data_min + timedelta(days=14)
    data_padrao = data_min
    # A proxima data disponivel ja vem do `anotar_esgotado` (o mesmo numero
    # que a vitrine anuncia). Antes esta rota recalculava com um loop proprio
    # — duas contas do mesmo fato, que podiam divergir e mostrar uma data no
    # card e outra no seletor.
    if lead == 0 and item.get('proxima_data'):
        data_padrao = date.fromisoformat(item['proxima_data'])
    return render_template(
        'loja/produto.html', item=item, em_teste=_em_teste(),
        personalizada=personalizada, monte=monte,
        data_hoje_iso=data_min.isoformat(),   # `min` do seletor (já com lead)
        data_max_iso=data_max.isoformat(),
        data_padrao_iso=data_padrao.isoformat(),
        lead_dias=lead,
    )


@loja_bp.route('/api/disponibilidade-checkout', methods=['POST'])
def api_disponibilidade_checkout():
    """Pro checkout AO MUDAR A DATA: verifica quais itens do carrinho NAO tem
    saldo pra essa data. Devolve a lista dos esgotados (nome + kind + id) e a
    proxima data disponivel pra TODOS — pra o cliente decidir entre trocar a
    data ou remover o(s) item(ns).

    Body JSON: {data: "YYYY-MM-DD", itens: [{kind, id}, ...]}.
    Decisao do dono 23/06/2026: incidente "checkout avancava sem avisar quais
    produtos esgotaram pra data escolhida"."""
    from datetime import date, timedelta

    from app.utils import hoje
    dados = request.get_json(silent=True) or {}
    try:
        d = date.fromisoformat(dados.get('data') or '')
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='data invalida'), 400
    if d < hoje() or d > hoje() + timedelta(days=30):
        return jsonify(ok=False, erro='data fora da janela'), 400

    itens_raw = dados.get('itens') or []
    esgotados = []
    nomes_esgotados = []
    for raw in itens_raw:
        kind = str(raw.get('kind') or '').strip()
        if kind not in ('receita', 'produto'):
            continue
        try:
            item_id = int(raw.get('id'))
        except (TypeError, ValueError):
            continue
        if loja_catalogo.tem_estoque_para_dia(kind, item_id, d):
            continue
        # Esgotado: pega o nome canonico do catalogo (nao confia no nome do
        # carrinho que pode estar desatualizado).
        cat = loja_catalogo.por_id_publicado(kind, item_id)
        nome = (cat or {}).get('nome') or 'produto'
        esgotados.append({'kind': kind, 'id': item_id, 'nome': nome})
        nomes_esgotados.append(nome)

    # Proxima data em que TODOS os itens do carrinho tem saldo (ate +30 dias).
    proxima = None
    if esgotados:
        ids_carrinho = []
        for raw in itens_raw:
            kind = str(raw.get('kind') or '').strip()
            if kind not in ('receita', 'produto'):
                continue
            try:
                ids_carrinho.append((kind, int(raw.get('id'))))
            except (TypeError, ValueError):
                continue
        for i in range(1, 31):
            d2 = hoje() + timedelta(days=i)
            if d2 == d:
                continue
            todos_ok = all(
                loja_catalogo.tem_estoque_para_dia(k, iid, d2)
                for k, iid in ids_carrinho)
            if todos_ok:
                proxima = d2.isoformat()
                break

    return jsonify(ok=True,
                   esgotados=esgotados,
                   nomes=nomes_esgotados,
                   proxima_disponivel=proxima)


@loja_bp.route('/api/disponibilidade-dia')
def api_disponibilidade_dia():
    """JSON pro seletor da pagina de produto: dado (kind, item_id, data),
    devolve `disponivel` (bool). Usa o plano_dia com fallback no EstoqueLoja
    (mesma regra de `tem_estoque_para_dia`).

    Publica — nao precisa de auth. Limita data a 30 dias pra frente pra evitar
    consultas absurdas."""
    from datetime import date, timedelta

    from app.utils import hoje
    kind = (request.args.get('kind') or '').strip()
    if kind not in ('receita', 'produto'):
        return jsonify(disponivel=False, erro='kind invalido'), 400
    try:
        item_id = int(request.args.get('item_id'))
        d = date.fromisoformat(request.args.get('data'))
    except (TypeError, ValueError):
        return jsonify(disponivel=False, erro='parametros invalidos'), 400
    if d < hoje() or d > hoje() + timedelta(days=30):
        return jsonify(disponivel=False, erro='data fora da janela'), 400
    ok = loja_catalogo.tem_estoque_para_dia(kind, item_id, d)
    return jsonify(disponivel=ok)


@loja_bp.route('/manifest.webmanifest')
def pwa_manifest_loja():
    """Manifest do PWA da LOJA (separado do PWA da gestão). Escopo /loja/ —
    quando o cliente "instala", o app abre direto na vitrine, não no admin."""
    from flask import current_app, send_from_directory
    return send_from_directory(
        current_app.static_folder, 'loja/manifest.webmanifest',
        mimetype='application/manifest+json')


@loja_bp.route('/sw.js')
def pwa_service_worker_loja():
    """Service Worker da loja (escopo /loja/). Servido aqui em vez de
    /static/loja/sw.js pra ter escopo `/loja/` natural (SW só controla URLs
    no mesmo path/abaixo de onde foi servido)."""
    from flask import current_app, send_from_directory
    resp = send_from_directory(
        current_app.static_folder, 'loja/sw.js',
        mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    # Não precisa de Service-Worker-Allowed porque já é servido sob /loja/.
    return resp


@loja_bp.route('/robots.txt')
def robots():
    """Robots.txt servido aqui E em /robots.txt (raiz, via alias no main
    blueprint). Aponta o `Sitemap:` pra raiz tambem — eh o padrao Google,
    e o Search Console acusou "sitemap em HTML" antes quando apontava
    pra /loja/sitemap.xml. Com loja em teste, bloqueia tudo."""
    if _loja_visivel_publico():
        body = ('User-agent: *\nAllow: /\n'
                f'Sitemap: {request.url_root}sitemap.xml\n')
    else:
        body = 'User-agent: *\nDisallow: /\n'
    return body, 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ── Paginas legais (CDC + LGPD + Decreto 7.962/2013) ───────────────────
# Obrigatorias pra e-commerce no Brasil. Renderizadas a partir de templates
# estaticos em templates/loja/legal/. Conteudo revisado pelo dono — texto
# referencia leis brasileiras, nao adaptar pra outro pais sem revisao.
# A data de atualizacao bate com a do ultimo deploy intencional dessas
# paginas; quando mudar o texto, atualizar `_LEGAL_ATUALIZADA`.
_LEGAL_ATUALIZADA = '21 de junho de 2026'


def _render_legal(template, titulo, descricao=None):
    return render_template(
        f'loja/legal/{template}',
        titulo=titulo,
        descricao=descricao,
        atualizada=_LEGAL_ATUALIZADA,
        em_teste=_em_teste(),
    )


@loja_bp.route('/privacidade')
def privacidade():
    return _render_legal('privacidade.html', 'Politica de Privacidade',
                         'Como tratamos seus dados pessoais (LGPD).')


@loja_bp.route('/termos')
def termos():
    return _render_legal('termos.html', 'Termos de Uso',
                         'Regras de uso do site e da compra.')


@loja_bp.route('/trocas')
def trocas():
    return _render_legal('trocas.html', 'Troca e Devolucao',
                         'Como pedir troca, devolucao ou reembolso (CDC).')


@loja_bp.route('/contato')
def contato():
    return _render_legal('contato.html', 'Atendimento ao Cliente',
                         'Telefone, WhatsApp, e-mail e enderecos.')


@loja_bp.route('/sitemap.xml')
def sitemap():
    """Sitemap dinamico (XML). Inclui home, paginas legais e cada produto
    publicado. Google indexa via robots.txt -> sitemap. Quando a loja
    estiver oculta (`LOJA_VISIVEL=0`), devolve 404 — nao queremos vazar
    URLs antes do cutover.
    """
    from app.utils import hoje
    if not _loja_visivel_publico():
        abort(404)
    hoje_iso = hoje().isoformat()
    urls = [
        (url_for('loja.home', _external=True), '1.0', 'daily'),
        (url_for('loja.privacidade', _external=True), '0.3', 'monthly'),
        (url_for('loja.termos', _external=True), '0.3', 'monthly'),
        (url_for('loja.trocas', _external=True), '0.3', 'monthly'),
        (url_for('loja.contato', _external=True), '0.5', 'monthly'),
    ]
    # Produtos publicados (preco_site > 0).
    for it in loja_catalogo.produtos_publicados():
        url = request.url_root.rstrip('/') + it['href']
        urls.append((url, '0.8', 'weekly'))
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, prio, freq in urls:
        linhas.append(
            f'  <url><loc>{loc}</loc><lastmod>{hoje_iso}</lastmod>'
            f'<changefreq>{freq}</changefreq>'
            f'<priority>{prio}</priority></url>'
        )
    linhas.append('</urlset>')
    return ('\n'.join(linhas), 200,
            {'Content-Type': 'application/xml; charset=utf-8'})
