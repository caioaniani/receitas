"""Rotas públicas da loja online (Fase 2, 16/06/2026).

Gate: enquanto `LOJA_VISIVEL=0` (padrão), visitante anônimo vê 404 — só
staff logado no admin vê. Pra cutover futuro (Fase 8): `LOJA_VISIVEL=1`
libera pra todo mundo.

Read-only nesta fase: home + página de produto. Botão "Comprar" fica
desabilitado com "Em breve" (carrinho/checkout entra na Fase 3).
"""
import os

from flask import abort, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.loja import loja_bp
from app.services import loja_catalogo


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
    if request.endpoint == 'loja.robots':
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


@loja_bp.route('/checkout')
def checkout():
    """Stub do checkout — a Fase 3 constrói o fluxo real (dados do cliente
    → modo de entrega → endereço/frete ou loja → data/janela → cartinha →
    cria PedidoOnline) no próximo passo. Por ora evita 404 no botão do
    carrinho e deixa o lugar reservado no roteamento."""
    return render_template('loja/checkout.html', em_teste=_em_teste())


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
