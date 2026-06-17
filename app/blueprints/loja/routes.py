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
from app.services import loja_catalogo, loja_checkout, loja_pagamento


def _ctx_checkout(erros=None, form=None):
    """Contexto compartilhado entre GET e POST(falha) do checkout.

    Datas viram min/max pro <input type="date"> (calendário). O range é
    contíguo (entregamos todo dia) e respeita o corte das 17h, então
    [min, max] bate exatamente com o conjunto que o servidor valida."""
    datas = loja_checkout.datas_disponiveis('agendada')
    return dict(
        em_teste=_em_teste(),
        lojas=loja_checkout.lojas_retirada(),
        data_min=(datas[0].isoformat() if datas else ''),
        data_max=(datas[-1].isoformat() if datas else ''),
        janelas=list(loja_checkout.JANELAS_HORARIAS),
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


@loja_bp.route('/pedido/<codigo>/pagamento', methods=['GET'])
def pedido_pagamento(codigo):
    """Tela de pagamento — escolhe método e mostra o resultado."""
    from flask import current_app
    pedido = _pedido_aguardando(codigo)
    pubkey = (current_app.config.get('PAGARME_PUBLIC_KEY') or '')
    pix_pendente = next((p for p in pedido.pagamentos
                         if p.metodo == 'pix' and p.status == 'pendente'),
                        None)
    return render_template('loja/pagamento.html',
                           pedido=pedido, pubkey=pubkey,
                           pix_pendente=pix_pendente, em_teste=_em_teste())


@loja_bp.route('/pedido/<codigo>/pix', methods=['POST'])
def pedido_pix(codigo):
    """Gera Pix (QR + copia-e-cola) pro pedido."""
    pedido = _pedido_aguardando(codigo)
    if pedido.status != 'aguardando_pagamento':
        return redirect(url_for('loja.pedido_confirmado', codigo=codigo))
    pag, erros = loja_pagamento.iniciar_pix(pedido)
    if erros:
        return render_template('loja/pagamento.html',
                               pedido=pedido, erros=erros,
                               em_teste=_em_teste()), 400
    return redirect(url_for('loja.pedido_pagamento', codigo=codigo))


@loja_bp.route('/pedido/<codigo>/cartao', methods=['POST'])
def pedido_cartao(codigo):
    """Processa pagamento em cartão. Recebe `card_token` (já tokenizado no
    front via pk_ do Pagar.me) — servidor NUNCA vê o número do cartão."""
    pedido = _pedido_aguardando(codigo)
    if pedido.status != 'aguardando_pagamento':
        return redirect(url_for('loja.pedido_confirmado', codigo=codigo))
    token = (request.form.get('card_token') or '').strip()
    try:
        parcelas = int(request.form.get('parcelas') or '1')
    except ValueError:
        parcelas = 1
    pag, erros = loja_pagamento.iniciar_cartao(pedido, token, parcelas)
    if erros:
        return render_template('loja/pagamento.html',
                               pedido=pedido, erros=erros,
                               em_teste=_em_teste()), 400
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


@loja_bp.before_request
def _gate_acesso():
    """404 silencioso pra visitante anônimo enquanto LOJA_VISIVEL=0.
    Retorna 404 (não 403) pra não confessar que a rota existe — robots/
    Google não indexa rota inexistente.

    `/loja/robots.txt` é exceção: precisa estar sempre acessível pra dizer
    pro Google "não indexa" enquanto a loja está em teste."""
    if request.endpoint in ('loja.robots', 'loja.webhook_pagarme'):
        return None
    if _loja_visivel_publico():
        return None  # liberada
    if current_user.is_authenticated:
        return None  # staff logado vê pra testar
    abort(404)


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
