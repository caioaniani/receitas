"""Rotas públicas da loja online (Fase 2, 16/06/2026).

Gate: enquanto `LOJA_VISIVEL=0` (padrão), visitante anônimo vê 404 — só
staff logado no admin vê. Pra cutover futuro (Fase 8): `LOJA_VISIVEL=1`
libera pra todo mundo.

Read-only nesta fase: home + página de produto. Botão "Comprar" fica
desabilitado com "Em breve" (carrinho/checkout entra na Fase 3).
"""
import json
import os

from flask import abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.loja import loja_bp
from app.extensions import csrf
from app.services import frete as frete_svc
from app.services import loja_auth, loja_catalogo, loja_checkout, loja_pagamento


def _ctx_checkout(erros=None, form=None):
    """Contexto compartilhado entre GET e POST(falha) do checkout.

    Datas viram min/max pro <input type="date"> (calendário). O range é
    contíguo (entregamos todo dia) e respeita o corte das 17h, então
    [min, max] bate exatamente com o conjunto que o servidor valida."""
    from app.utils import agora
    datas = loja_checkout.datas_disponiveis('agendada')
    base = agora()
    return dict(
        em_teste=_em_teste(),
        lojas=loja_checkout.lojas_retirada(),
        data_min=(datas[0].isoformat() if datas else ''),
        data_max=(datas[-1].isoformat() if datas else ''),
        janelas=list(loja_checkout.JANELAS_HORARIAS),
        # Pro JS filtrar janelas passadas quando a data escolhida é hoje
        # (usa a hora do SERVIDOR — evita divergência de relógio do cliente).
        hoje_iso=base.date().isoformat(),
        min_hora_hoje=base.hour + loja_checkout.LEAD_HORAS,
        express_ok=loja_checkout.express_disponivel(),
        erros=erros, form=(form or {}),
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
def pedido_pix(codigo):
    """Gera Pix (QR + copia-e-cola) pro pedido."""
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
def pedido_cartao(codigo):
    """Processa pagamento em cartão. Recebe `card_token` (já tokenizado no
    front via pk_ do Pagar.me) — servidor NUNCA vê o número do cartão."""
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


@loja_bp.route('/webhook/pagarme', methods=['POST'])
@csrf.exempt
def webhook_pagarme():
    """Webhook do Pagar.me. Protegido por segredo na URL (?k=) — mesmo
    padrão de Chatwoot/Slack/Zapi nesse projeto.

    Idempotente (PagarmeEvento). 'order.paid'/'charge.paid' marca pago e
    baixa estoque (venda_site). 'refunded'/'canceled' estorna. Reentrega do
    mesmo evento devolve 200 sem dupli­car efeito."""
    import hmac

    from flask import current_app
    segredo_esperado = (current_app.config.get('PAGARME_WEBHOOK_SECRET')
                        or '').strip()
    if not segredo_esperado:
        return jsonify(ok=False, erro='webhook desabilitado'), 503
    fornecido = request.args.get('k') or ''
    # Comparação constante — defesa contra timing attack.
    if not hmac.compare_digest(segredo_esperado, fornecido):
        return jsonify(ok=False, erro='unauthorized'), 401
    evento = request.get_json(silent=True) or {}
    res = loja_pagamento.processar_webhook(evento)
    # Devolve 200 mesmo em "sem_pedido"/"ignorado" pra Pagar.me NÃO ficar
    # reentregando indefinidamente um evento que não vai processar.
    return jsonify(res), 200


def _loja_visivel_publico():
    """Lê env `LOJA_VISIVEL`. '1' = pública pra todo mundo (Fase 8).
    Qualquer outro valor = gated (só staff logado vê)."""
    return os.environ.get('LOJA_VISIVEL', '0').strip() == '1'


def _em_teste():
    """Vitrine ainda escondida do público — exibe banner 'em teste' pra
    quem vê (que necessariamente é staff logado)."""
    return not _loja_visivel_publico()


@loja_bp.context_processor
def _injetar_cliente_atual():
    """Expõe `cliente_atual` nos templates da loja (header + base usam)."""
    return {'cliente_atual': loja_auth.cliente_atual()}


@loja_bp.before_request
def _gate_acesso():
    """404 silencioso pra visitante anônimo enquanto LOJA_VISIVEL=0.
    Retorna 404 (não 403) pra não confessar que a rota existe — robots/
    Google não indexa rota inexistente.

    `/loja/robots.txt` é exceção: precisa estar sempre acessível pra dizer
    pro Google "não indexa" enquanto a loja está em teste."""
    # Sempre liberadas: rotas de auth (cliente precisa entrar/cadastrar) +
    # robots + webhook do gateway.
    if request.endpoint in (
            'loja.robots', 'loja.webhook_pagarme',
            'loja.entrar', 'loja.cadastrar', 'loja.sair',
            'loja.esqueci_senha', 'loja.redefinir_senha',
    ):
        return None
    if _loja_visivel_publico():
        return None  # liberada
    if current_user.is_authenticated:
        return None  # staff logado vê pra testar
    if loja_auth.cliente_atual():
        return None  # cliente logado pode entrar mesmo em modo teste
    abort(404)


# ── Auth do cliente (Fase 6) ───────────────────────────────────────────
# Sessão SEPARADA do admin: cliente usa `cliente_id` na sessão; staff usa
# `_user_id` do Flask-Login. Nunca cruzam — privilégio NÃO escala.

@loja_bp.route('/entrar', methods=['GET', 'POST'])
def entrar():
    """Login do cliente final."""
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
def cadastrar():
    """Cadastro de cliente: cria conta nova OU vincula senha a um Cliente
    que já existia como guest (mesmo email)."""
    if loja_auth.cliente_atual():
        return redirect(url_for('loja.minha_conta'))
    erros, form = [], {}
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
            else:
                if not c:
                    c = Cliente(nome=nome, email=email, telefone=telefone)
                    db.session.add(c)
                else:
                    # Guest virou conta: preenche o que faltava sem
                    # sobrescrever o que o cliente já tinha.
                    c.nome = c.nome or nome
                    c.telefone = c.telefone or telefone
                c.set_senha(senha)
                c.aceite_lgpd_em = c.aceite_lgpd_em or agora()
                db.session.commit()
                loja_auth.login_cliente(c)
                return redirect(url_for('loja.minha_conta'))
    return render_template('loja/cadastrar.html',
                           em_teste=_em_teste(),
                           erros=erros, form=form), \
        (400 if erros else 200)


@loja_bp.route('/sair', methods=['POST'])
def sair():
    """Logout do cliente. POST pra não disparar por link/prefetch."""
    loja_auth.logout_cliente()
    return redirect(url_for('loja.home'))


@loja_bp.route('/esqueci-senha', methods=['GET', 'POST'])
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
    itens = loja_catalogo.produtos_publicados()
    grupos = loja_catalogo.por_categorias(itens)
    return render_template(
        'loja/home.html',
        grupos=grupos, total_itens=len(itens), em_teste=_em_teste(),
    )


@loja_bp.route('/carrinho')
def carrinho():
    """Página do carrinho. O estado vive no navegador (localStorage) —
    o servidor só serve a casca; o JS (carrinho.js) renderiza os itens.
    Persiste no banco só no checkout (cria PedidoOnline). Rota estática
    tem prioridade sobre /<slug_completo> no roteamento do Werkzeug."""
    return render_template('loja/carrinho.html', em_teste=_em_teste())


@loja_bp.route('/checkout', methods=['GET', 'POST'])
def checkout():
    """Checkout do site. GET serve o formulário; POST cria o PedidoOnline.

    O carrinho vem do navegador (campo oculto itens_json). A integridade de
    dinheiro é do SERVIDOR: loja_checkout.criar_pedido re-busca preço no
    catálogo e recomputa o frete — nunca confia no que o cliente mandou.
    PRG: sucesso redireciona pra confirmação."""
    if request.method == 'POST':
        try:
            itens_raw = json.loads(request.form.get('itens_json') or '[]')
        except ValueError:
            itens_raw = []
        pedido, erros = loja_checkout.criar_pedido(request.form, itens_raw)
        if not erros:
            return redirect(url_for('loja.pedido_pagamento',
                                    codigo=pedido.codigo))
        return render_template(
            'loja/checkout.html',
            **_ctx_checkout(erros=erros, form=request.form)), 400
    return render_template('loja/checkout.html', **_ctx_checkout())


@loja_bp.route('/api/cep/<cep>', methods=['GET'])
def api_cep(cep):
    """Lookup de CEP via BrasilAPI pra autocompletar logradouro/bairro/
    cidade/UF no checkout. Faz cache simples no servidor não — devolve
    a resposta como veio. Sem rate limit dedicado (o gate da loja já
    barra anônimos enquanto LOJA_VISIVEL=0)."""
    import requests
    cep_d = ''.join(c for c in (cep or '') if c.isdigit())
    if len(cep_d) != 8:
        return jsonify(ok=False, erro='CEP precisa ter 8 dígitos.'), 400
    try:
        r = requests.get(
            f'https://brasilapi.com.br/api/cep/v2/{cep_d}', timeout=6)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, erro='Não consegui consultar o CEP.'), 502
    if r.status_code != 200:
        return jsonify(ok=False, erro='CEP não encontrado.'), 404
    j = r.json() or {}
    return jsonify(ok=True,
                   logradouro=j.get('street') or '',
                   bairro=j.get('neighborhood') or '',
                   cidade=j.get('city') or '',
                   uf=j.get('state') or '')


@loja_bp.route('/api/frete', methods=['POST'])
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
    return jsonify(frete_svc.consultar_frete(geo))


@loja_bp.route('/pedido/<codigo>')
def pedido_confirmado(codigo):
    """Confirmação do pedido (PRG). Também é a base do 'meus pedidos'
    (Fase 6). Rota com segmento estático 'pedido' — não colide com
    /<slug_completo> (profundidade diferente)."""
    from app.models import PedidoOnline
    pedido = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not pedido:
        abort(404)
    return render_template('loja/pedido_confirmado.html',
                           pedido=pedido, em_teste=_em_teste())


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
    # Slug desatualizado → 301 pra canônica (SEO + URLs sempre limpas)
    if slug_recebido != item['slug']:
        return redirect(url_for('loja.produto',
                                 slug_completo=item['href'].split('/loja/')[-1]),
                         code=301)
    return render_template(
        'loja/produto.html', item=item, em_teste=_em_teste(),
    )


@loja_bp.route('/robots.txt')
def robots():
    """Enquanto em teste, nada indexável. Quando virar pública (Fase 8),
    troca pra um sitemap real."""
    if _loja_visivel_publico():
        body = ('User-agent: *\nAllow: /\n'
                f'Sitemap: {request.url_root}loja/sitemap.xml\n')
    else:
        body = 'User-agent: *\nDisallow: /\n'
    return body, 200, {'Content-Type': 'text/plain; charset=utf-8'}
