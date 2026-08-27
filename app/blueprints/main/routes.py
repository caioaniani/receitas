Warning: truncated output (original token count: 70457)
Total output lines: 6634

import json
from datetime import datetime, timedelta

from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app import nav
from app.blueprints.main import main_bp
from app.decorators import (
    admin_required,
    divulgacao_required,
    gerente_required,
    owner_required,
)
from app.extensions import db
from app.models import (
    AlertaEstoque,
    Atribuicao,
    AtribuicaoEntrega,
    AuditLog,
    Funcionario,
    MateriaPrima,
    MovimentacaoEstoque,
    PedidoLocal,
    PedidoLoja,
    PlanejamentoProducao,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
    Usuario,
)
from app.services.custos import calcular_custos_receitas, calcular_rendimento
from app.utils import agora
from app.utils import hoje as hoje_brt


# ── SEO: sitemap.xml e robots.txt na RAIZ do dominio ──────────────────
# Google e qualquer crawler sempre busca em /sitemap.xml e /robots.txt
# (RFC + Sitemaps Protocol). O conteudo de fato mora no blueprint da loja
# (/loja/sitemap.xml, /loja/robots.txt) porque eh la que o catalogo vive
# — aqui na raiz so reusamos a mesma funcao. Sem isso, o Search Console
# acusa "sitemap em HTML" (cai em 404 do roteamento por host).
@main_bp.route('/sitemap.xml')
def sitemap_root():
    from app.blueprints.loja.routes import sitemap as _loja_sitemap
    return _loja_sitemap()


@main_bp.route('/robots.txt')
def robots_root():
    from app.blueprints.loja.routes import robots as _loja_robots
    return _loja_robots()


@main_bp.route('/')
@login_required
def index():
    if current_user.is_padeiro():
        return redirect(url_for('padeiro.index'))
    # Marketing (21/07/2026): papel enxuto, so lanca divulgacao — vai direto
    # pra tela dele (nao tem home de admin nem outras areas).
    if current_user.is_marketing():
        return redirect(url_for('main.loja_online_divulgacao'))
    if current_user.is_admin():
        # Bloco "Precisa de você hoje": as MESMAS pendências do briefing
        # diário do dono (fonte única em app/services/briefing_dono.py).
        # Itens de tela owner-only só aparecem pro owner (mesmo gate do
        # dashboard).
        from app.services import briefing_dono
        pend = briefing_dono.pendencias(
            incluir_owner=bool(current_user.is_owner))
        # Vendas de ontem SÓ pro dono (faturamento é o cockpit pessoal —
        # mesmo gate do /admin/briefing). capturar=False: a home carrega a
        # toda hora e NUNCA deve bater na API Seru; o cron de 15 min mantém
        # o snapshot de ontem quente.
        vendas = (briefing_dono.vendas_ontem(capturar=False)
                  if current_user.is_owner else None)
        vendas_hoje = (briefing_dono.vendas_hoje(capturar=False)
                       if current_user.is_owner else None)
        from app.ui_v2 import ui_v2_ativo
        template = ('main/home_v2.html' if ui_v2_ativo()
                    else 'main/home.html')
        return render_template(template,
                               areas=nav.areas_visiveis(current_user),
                               pendencias=pend,
                               vendas=vendas,
                               vendas_hoje=vendas_hoje)
    return render_template('main/inicio.html')


@main_bp.route('/area/<slug>')
@login_required
def area(slug):
    """Página de uma ÁREA do hub: lista as funções daquela área (os MESMOS
    links da sidebar, via macro compartilhado `_area_nav.html`). Guarda pela
    mesma permissão do card em `home.html`."""
    meta = nav.area_por_slug(slug)
    if not meta:
        abort(404)
    if not meta['pode'](current_user):
        abort(403)
    from app.ui_v2 import ui_v2_ativo
    template = ('main/area_v2.html' if ui_v2_ativo()
                else 'main/area.html')
    return render_template(template, area=meta)


@main_bp.route('/ui/nova')
@login_required
def ui_nova():
    """Volta ESTE usuário à interface v2 (limpa o cookie de opt-out).
    A v2 é o padrão do sistema interno — este é o caminho de volta pra
    quem clicou em "Interface anterior"."""
    from app.ui_v2 import UI_CLASSIC_COOKIE
    resp = redirect(url_for('main.index'))
    resp.delete_cookie(UI_CLASSIC_COOKIE)
    return resp


@main_bp.route('/ui/classica')
@login_required
def ui_classica():
    """Volta ESTE usuário à interface anterior (cookie de opt-out, 90d).
    Rollback individual — a env `UI_V2_ENABLED` segue valendo pros
    demais; zerar a env desliga a v2 pra todo mundo."""
    from app.ui_v2 import UI_CLASSIC_COOKIE
    resp = redirect(url_for('main.index'))
    resp.set_cookie(UI_CLASSIC_COOKIE, '1', max_age=60 * 60 * 24 * 90,
                    samesite='Lax', httponly=True,
                    secure=request.is_secure)
    return resp


@main_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    # ativas(): KPI do portfolio EM CIRCULACAO — receita arquivada com preco
    # preenchido inflava receita_estimada/margem_geral (varredura 19/07/2026).
    # O custo soma pelos MESMOS nomes ativos pra margem ficar coerente.
    receitas = Receita.ativas().all()

    custo_mp_total = sum(custos_map.get(r.nome, 0) for r in receitas)
    receita_estimada = sum((r.preco_venda or 0) for r in receitas if r.preco_venda)

    # Eager load do cargo evita N+1 — `custo_total()` acessa `self.cargo.salario_base`.
    funcionarios_ativos = (Funcionario.query
                            .options(joinedload(Funcionario.cargo))
                            .filter_by(ativo=True).all())
    custo_mao_obra = sum(f.custo_total() for f in funcionarios_ativos)

    margem_geral = 0
    if receita_estimada > 0:
        # Margem LÍQUIDA: desconta os impostos sobre venda (PIS/COFINS/ICMS,
        # app/services/impostos.py) da receita estimada antes do custo.
        from app.services import impostos
        margem_geral = ((receita_estimada * (1 - impostos.carga_venda())
                         - custo_mp_total) / receita_estimada * 100)

    alertas_estoque = db.session.query(AlertaEstoque).join(MateriaPrima).filter(
        MateriaPrima.estoque_atual < AlertaEstoque.estoque_minimo
    ).count()

    producoes_pendentes = PlanejamentoProducao.query.filter_by(status='rascunho').count()
    atribuicoes_pendentes = Atribuicao.query.filter_by(status='pendente').count()

    # ProdutoItem orfaos: cestas com componente sem FK vinculada.
    # Esses componentes NAO baixam estoque na venda — owner precisa
    # vincular manualmente em /cestas/orfaos.
    from app.services.cestas import contar_produto_itens_orfaos
    cestas_orfaos = contar_produto_itens_orfaos() if current_user.is_owner else 0

    # Pendencias do sync PDV (lojas/produtos nao mapeados travam baixa de
    # estoque na venda). So owner ve — link pro painel /pdv/saude.
    pdv_pendencias = 0
    if current_user.is_owner:
        try:
            from app.services import pdv_saude
            pdv_pendencias = pdv_saude.contar_pendencias()
        except Exception:  # noqa: BLE001
            pdv_pendencias = 0

    hoje = hoje_brt()
    aniversariantes = [f for f in funcionarios_ativos
                       if f.data_nascimento and f.data_nascimento.month == hoje.month]

    return render_template('main/dashboard.html',
                           custo_mp_total=custo_mp_total,
                           receita_estimada=receita_estimada,
                           custo_mao_obra=custo_mao_obra,
                           margem_geral=margem_geral,
                           alertas_estoque=alertas_estoque,
                           producoes_pendentes=producoes_pendentes,
                           atribuicoes_pendentes=atribuicoes_pendentes,
                           cestas_orfaos=cestas_orfaos,
                           pdv_pendencias=pdv_pendencias,
                           aniversariantes=aniversariantes,
                           total_funcionarios=len(funcionarios_ativos))


@main_bp.route('/rentabilidade')
@login_required
def rentabilidade():
    from app.services import impostos

    resultado = calcular_custos_receitas()
    custos_receita = resultado['custos']
    # ativas(): decisao de preco/margem e sobre o portfolio vivo (varredura
    # 19/07/2026 — arquivadas apareciam misturadas sem marcacao).
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    # Impostos sobre venda (PIS/COFINS/ICMS, dono 13/07/2026): lucro/margem
    # exibidos são LÍQUIDOS — preço × (1 − carga) − custo.
    carga = impostos.carga_venda()

    dados = []
    for r in receitas:
        custo_un = custos_receita.get(r.nome, 0)
        rendimento = calcular_rendimento(r)
        custo_total = custo_un * rendimento

        preco_at = r.preco_venda or 0
        lucro_at = impostos.lucro_liquido(preco_at, custo_un, carga)
        margem_at = impostos.margem_liquida(preco_at, custo_un, carga)

        preco_lj = r.preco_loja or 0
        lucro_lj = impostos.lucro_liquido(preco_lj, custo_un, carga)
        margem_lj = impostos.margem_liquida(preco_lj, custo_un, carga)

        preco_st = r.preco_site or 0
        lucro_st = impostos.lucro_liquido(preco_st, custo_un, carga)
        margem_st = impostos.margem_liquida(preco_st, custo_un, carga)

        dados.append({
            'id': r.id,
            'nome': r.nome,
            'categoria': r.categoria or 'Outros',
            'rendimento': rendimento,
            'custo_total': custo_total,
            'custo_un': custo_un,
            'preco_atacado': preco_at,
            'lucro_atacado': lucro_at,
            'margem_atacado': margem_at,
            'preco_loja': preco_lj,
            'lucro_loja': lucro_lj,
            'margem_loja': margem_lj,
            'preco_site': preco_st,
            'lucro_site': lucro_st,
            'margem_site': margem_st,
        })

    return render_template('main/rentabilidade.html', dados=dados,
                           impostos=impostos.aliquotas())


@main_bp.route('/rentabilidade/impostos', methods=['POST'])
@login_required
@admin_required
def rentabilidade_impostos():
    """Atualiza as alíquotas de imposto sobre venda (PIS/COFINS/ICMS) usadas
    nas margens líquidas de TODAS as telas (fonte única em
    app/services/impostos.py). Só exibição/decisão — não mexe em preço."""
    from app.services import impostos

    try:
        a = impostos.salvar_aliquotas(request.form.get('pis'),
                                      request.form.get('cofins'),
                                      request.form.get('icms'))
        flash('Impostos sobre venda atualizados: PIS %.2f%% + COFINS %.2f%% '
              '+ ICMS %.2f%% = %.2f%% — margens recalculadas.'
              % (a['pis'], a['cofins'], a['icms'], a['total']), 'success')
    except ValueError as e:
        flash('Alíquota inválida (%s): use números entre 0 e 95.' % e,
              'warning')
    return redirect(url_for('main.rentabilidade'))


# Regras do pedido de ATACADO — texto livre editavel pelo dono (AppConfig),
# mostrado no topo do /cardapio?tipo=atacado e impresso junto. Cada campo e
# opcional: vazio nao aparece. Informativo (nao trava pedido — atacado entra por
# orcamento/WhatsApp). Editar em /admin/cardapio-atacado/regras. (13/07/2026)
_CARDAPIO_ATACADO_PREFIXO = 'cardapio_atacado_'
CARDAPIO_ATACADO_CAMPOS = [
    ('pedido_minimo', 'Pedido mínimo', 'Ex: R$ 300,00 por pedido'),
    ('prazo', 'Prazo para pedidos', 'Ex: pedir até as 14h do dia anterior'),
    ('pagamento', 'Pagamento', 'Ex: boleto 14 dias, pix à vista ou faturamento mensal'),
    ('entrega', 'Entregas', 'Ex: terça a sábado, período da manhã'),
    ('frete', 'Frete / área de entrega', 'Ex: frete grátis acima de R$ 500; atende zona sul'),
    ('qtd_minima', 'Quantidade mínima por item', 'Ex: pão de queijo em caixa de 10'),
    ('validade', 'Validade da tabela', 'Ex: preços sujeitos a alteração sem aviso'),
    ('contato', 'Pedidos e contato', 'Ex: WhatsApp (11) 90000-0000 — falar com Fulano'),
]


def _regras_atacado():
    """Lista [{label, valor}] das regras de atacado PREENCHIDAS, na ordem de
    exibicao. Vazia = nenhum campo preenchido (bloco nao aparece)."""
    from app.models import AppConfig
    out = []
    for chave, label, _ph in CARDAPIO_ATACADO_CAMPOS:
        val = (AppConfig.get(_CARDAPIO_ATACADO_PREFIXO + chave) or '').strip()
        if val:
            out.append({'label': label, 'valor': val})
    return out


# Metodos de preparo do cardapio de ATACADO (20/07/2026, ditado do dono):
# como o cliente B2B prepara o que compra — backup (congelado cru), assado
# congelado, sourdough 14 fatias, brioche fresco. Texto em AppConfig
# `cardapio_atacado_preparo` (uma linha por metodo, "Rotulo: texto"),
# editavel na tela de regras. CONTRATO: chave AUSENTE (None) = usa o
# default abaixo; chave gravada VAZIA = dono apagou de proposito, bloco
# some. Sem em-dash nos textos (fora do latin-1 do PDF — regra da casa).
_CARDAPIO_PREPARO_KEY = 'cardapio_atacado_preparo'
CARDAPIO_PREPARO_DEFAULT = (
    'Viennoiserie no método backup (congelado cru): a massa vai fermentada '
    'até o ponto de forno e é congelada crua. É só tirar do freezer, '
    'esperar perder o gelo, pincelar com egg wash e assar. É o método que '
    'garante a melhor qualidade no produto final; o croissant tradicional '
    'e o pain au chocolat também funcionam assim.\n'
    'Viennoiserie assada e congelada: opção mais prática; a qualidade fica '
    'um pouco abaixo do método backup.\n'
    'Pães sourdough: vendidos congelados. Cada pão rende 14 fatias, para '
    'lanches e aperitivos.\n'
    'Brioche: entregue fresco, com validade de 3 dias.'
)


def _preparo_atacado():
    """Lista [{label, valor}] dos metodos de preparo do atacado. Cada linha
    do texto vira um item; "Rotulo: resto" separa no PRIMEIRO ':' (rotulo
    curto — ':' tardio e' texto corrido, sem rotulo)."""
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_PREPARO_KEY)
    if raw is None:
        raw = CARDAPIO_PREPARO_DEFAULT
    out = []
    for linha in raw.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        rotulo, sep, resto = linha.partition(':')
        if sep and 0 < len(rotulo.strip()) <= 60 and resto.strip():
            out.append({'label': rotulo.strip(), 'valor': resto.strip()})
        else:
            out.append({'label': None, 'valor': linha})
    return out


def _preparo_atacado_raw():
    """Texto cru pro textarea da tela de regras (None = default vigente)."""
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_PREPARO_KEY)
    return CARDAPIO_PREPARO_DEFAULT if raw is None else raw


# "Quem somos nós" do cardapio (21/07/2026, pedido do dono; texto escrito a
# partir da historia da fundacao contada pela Camila): a historia da casa no
# RODAPE do cardapio, antes das regras/metodos. E' texto de MARCA, entao vale
# pros 3 tipos (atacado/loja/site), diferente das regras/preparo (so atacado).
# AppConfig `cardapio_quem_somos`, um PARAGRAFO por linha, editavel na tela
# de regras do atacado (mesma tela do logotipo — config de marca). CONTRATO
# (igual ao preparo): chave AUSENTE = default abaixo; gravada VAZIA = dono
# apagou de proposito, bloco some. Sem em-dash (fora do latin-1 do PDF).
_CARDAPIO_QUEM_SOMOS_KEY = 'cardapio_quem_somos'
CARDAPIO_QUEM_SOMOS_DEFAULT = (
    'O Pão nasceu de uma história de família. Viemos de padarias '
    'tradicionais de São Paulo e nos apaixonamos pela fermentação natural: '
    'os primeiros pães saíram de um cantinho da padaria da família, com '
    'farinha francesa e muita insistência.\n'
    'Na pandemia, recomeçamos do zero, vendendo pães no nosso próprio '
    'prédio. Nossa primeira cesta de presente se chamava Abraço em Forma '
    'de Pão: como ninguém podia se abraçar, a gente mandava um abraço em '
    'forma de pão.\n'
    'O carinho dos clientes transformou aquele começo em produção '
    'artesanal própria e lojas em São Paulo, com o mesmo cuidado do '
    'primeiro dia.\n'
    'Todo pão continua sendo feito como no início: fermentação natural, '
    'farinhas francesas T65 e T45, chocolate belga Callebaut e, '
    'principalmente, tempo. A massa descansa o quanto precisa.'
)


def _quem_somos():
    """Paragrafos do "Quem somos nós" (um por linha nao-vazia).
    [] = bloco escondido (dono apagou)."""
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_QUEM_SOMOS_KEY)
    if raw is None:
        raw = CARDAPIO_QUEM_SOMOS_DEFAULT
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _quem_somos_raw():
    """Texto cru pro textarea da tela de regras (None = default vigente)."""
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_QUEM_SOMOS_KEY)
    return CARDAPIO_QUEM_SOMOS_DEFAULT if raw is None else raw


# Slogan do hero do cardapio (21/07/2026, pedido do dono: "Preciso alterar
# esse slogan"): a linha embaixo do logo, na tela e na capa dos PDFs.
# AppConfig `cardapio_slogan`; MESMO contrato do preparo/quem_somos: chave
# AUSENTE = default abaixo; gravada VAZIA = dono apagou, linha some.
_CARDAPIO_SLOGAN_KEY = 'cardapio_slogan'
CARDAPIO_SLOGAN_DEFAULT = ('Tempo. Fermento. Cuidado. '
                           'Pão de verdade, feito com fermentação natural.')


def _slogan():
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_SLOGAN_KEY)
    return (CARDAPIO_SLOGAN_DEFAULT if raw is None else raw).strip()


def _slogan_raw():
    """Texto cru pro input da tela de regras (None = default vigente)."""
    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_SLOGAN_KEY)
    return CARDAPIO_SLOGAN_DEFAULT if raw is None else raw


# Ordem das seções do cardápio (21/07/2026, pedido do dono: "alterar as
# sessões com um grab and drop em editar regras"). Duas listas arrastáveis
# na tela de regras, salvas em AppConfig como JSON:
# - `cardapio_ordem_categorias`: ordem das categorias de produto (vale pra
#   tela E pros PDFs — a fonte única `_cardapio_categorias` devolve o dict
#   já na ordem final). Categoria fora da lista (nova/renomeada) entra no
#   FIM em ordem alfabética ('Outros' por último) — nunca some.
# - `cardapio_ordem_rodape` (nome histórico da chave): ordem das SEÇÕES da
#   página — quem_somos / regras / preparo / produtos. Em 21/07/2026 o dono
#   pediu "o rodapé venha para cima" com a posição tambem arrastavel, entao
#   'produtos' virou item da lista (SUBSTITUI a decisão de 20/07 "produtos
#   para cima": o DEFAULT agora é blocos antes dos produtos). Seção ausente
#   da lista salva entra no fim, na ordem default.
_CARDAPIO_ORDEM_CATS_KEY = 'cardapio_ordem_categorias'
_CARDAPIO_ORDEM_SECOES_KEY = 'cardapio_ordem_rodape'
CARDAPIO_SECOES = ['quem_somos', 'regras', 'preparo', 'produtos']
SECOES_LABELS = {'quem_somos': 'Quem somos nós',
                 'regras': 'Regras do pedido (atacado)',
                 'preparo': 'Métodos de preparo (atacado)',
                 'produtos': 'Produtos (categorias)'}


def _ordem_categorias_salva():
    """Lista de nomes de categoria na ordem escolhida pelo dono ([] = sem
    preferência, fica a alfabética)."""
    import json

    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_ORDEM_CATS_KEY)
    if not raw:
        return []
    try:
        return [str(c).strip() for c in json.loads(raw) if str(c).strip()]
    except (ValueError, TypeError):
        current_app.logger.warning('cardapio: ordem_categorias inválida')
        return []


def _aplicar_ordem_categorias(categorias):
    """Reordena o dict de categorias: primeiro as da ordem salva, depois as
    demais em alfabética com 'Outros' por último."""
    ordem = _ordem_categorias_salva()

    def chave(c):
        if c in ordem:
            return (0, ordem.index(c), '')
        return (1, c == 'Outros', c)
    return {c: categorias[c] for c in sorted(categorias, key=chave)}


def _ordem_secoes():
    """Ordem das seções da página (sempre todas; seção fora da lista salva
    — ex.: 'produtos' pra quem salvou antes de 21/07 — entra no fim, na
    ordem default)."""
    import json

    from app.models import AppConfig
    raw = AppConfig.get(_CARDAPIO_ORDEM_SECOES_KEY)
    ordem = []
    if raw:
        try:
            # dict.fromkeys = dedupe preservando ordem (valor gravado com
            # repetido desenharia o bloco 2x na tela e no PDF).
            ordem = list(dict.fromkeys(
                b for b in json.loads(raw) if b in CARDAPIO_SECOES))
        except (ValueError, TypeError):
            current_app.logger.warning('cardapio: ordem_secoes inválida')
    return ordem + [b for b in CARDAPIO_SECOES if b not in ordem]


# Foto do "Quem somos nós" (21/07/2026, pedido do dono — mandou a foto da
# fachada da loja): entra ao lado do texto, na tela e no PDF. DEFAULT =
# arquivo commitado em static/img/cardapio_quem_somos.jpg (a foto que o dono
# escolheu — no ar sem gesto nenhum); upload na tela de regras grava data
# URI em AppConfig por cima (mesmo padrão do logotipo — sobrevive a deploy,
# auto-contido). "Remover" volta ao default; não há estado "sem foto" (se o
# dono quiser o bloco sem foto um dia, é decisão nova).
_CARDAPIO_QS_FOTO_KEY = 'cardapio_quem_somos_foto'
_QS_FOTO_STATIC = 'img/cardapio_quem_somos.jpg'


def _quem_somos_foto_src():
    """src pra tag <img> da tela: data URI custom ou o arquivo estático."""
    from app.models import AppConfig
    custom = (AppConfig.get(_CARDAPIO_QS_FOTO_KEY) or '').strip()
    return custom or url_for('static', filename=_QS_FOTO_STATIC)


def _quem_somos_foto_bytes():
    """Bytes da foto pro PDF (data URI decodado; URI quebrado/ausente cai
    no arquivo estático). None só se o estático também falhar — o PDF sai
    sem foto, nunca deixa de gerar."""
    import base64
    import os

    from app.models import AppConfig
    custom = (AppConfig.get(_CARDAPIO_QS_FOTO_KEY) or '').strip()
    if custom and 'base64,' in custom:
        try:
            return base64.b64decode(custom.split('base64,', 1)[1])
        except Exception:  # noqa: BLE001 — URI quebrado cai no estático
            current_app.logger.warning('quem_somos: data URI inválido',
                                       exc_info=True)
    try:
        caminho = os.path.join(current_app.static_folder,
                               *_QS_FOTO_STATIC.split('/'))
        with open(caminho, 'rb') as f:
            return f.read()
    except Exception:  # noqa: BLE001 — foto ruim não derruba o cardápio
        current_app.logger.warning('quem_somos: foto indisponível',
                                   exc_info=True)
        return None


def _processar_foto_quem_somos(data):
    """Upload → data URI JPEG 3:4 (900x1200, crop central) — mesma proporção
    do default; o layout (tela e PDF) conta com ela. ValueError em imagem
    inválida (o chamador flasheia)."""
    import base64
    import io

    from PIL import Image, ImageOps
    if not data:
        raise ValueError('Arquivo vazio')
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert('RGB')
        img = ImageOps.fit(img, (900, 1200))
    except Exception as e:  # noqa: BLE001 — HEIC/formato ruim vira ValueError
        raise ValueError('imagem inválida') from e
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=82, optimize=True)
    raw = buf.getvalue()
    if len(raw) > 1024 * 1024:                       # trava de sanidade
        raise ValueError('foto processada ficou grande demais (>1MB)')
    return 'data:image/jpeg;base64,%s' % base64.b64encode(raw).decode('ascii')


# Logotipo do cardapio (13/07/2026 base; logo 20/07/2026): substitui o
# wordmark "O Pao" no hero da tela e na capa do PDF. Guardado como data URI
# (base64) em AppConfig — auto-contido (sobrevive deploy, sem dependencia de
# host externo/CSP: o hero usa data: e o PDF decodifica os bytes). Vazio =
# cai no texto "O Pao". Config em /admin/cardapio-atacado/regras.
_CARDAPIO_LOGO_KEY = 'cardapio_logo_data'


def _cardapio_logo():
    from app.models import AppConfig
    return (AppConfig.get(_CARDAPIO_LOGO_KEY) or '').strip() or None


def _processar_logo_cardapio(data, *, branco=True):
    """Processa o upload do logo pra data URI. `branco=True` (default, ideal
    pro hero ESCURO): converte a marca preta/monocromatica numa SILHUETA
    BRANCA sobre transparente (ink escuro -> branco opaco, fundo claro ->
    transparente), casando com o "O Pao" branco de hoje. `branco=False`:
    mantem a imagem fiel (PNG se tiver alpha, senao JPEG). Levanta ValueError
    em imagem invalida (o chamador flasheia)."""
    import base64
    import io

    from PIL import Image, ImageChops, ImageOps
    if not data:
        raise ValueError('Arquivo vazio')
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGBA')
        img.thumbnail((900, 900), Image.LANCZOS)
    except Exception as e:  # noqa: BLE001 — HEIC/formato ruim vira ValueError
        raise ValueError('imagem invalida') from e

    if branco:
        r, g, b, a = img.split()
        lum = Image.merge('RGB', (r, g, b)).convert('L')
        ink = lum.point(lambda p: 255 - p)          # escuro = tinta
        novo_alpha = ImageChops.multiply(ink, a)     # respeita transparencia
        saida = Image.new('RGBA', img.size, (255, 255, 255, 0))
        saida.putalpha(novo_alpha)
        fmt, mime = 'PNG', 'image/png'
    elif img.getchannel('A').getextrema()[0] < 255:
        saida, fmt, mime = img, 'PNG', 'image/png'
    else:
        saida, fmt, mime = img.convert('RGB'), 'JPEG', 'image/jpeg'

    buf = io.BytesIO()
    if fmt == 'JPEG':
        saida.save(buf, format='JPEG', quality=85, optimize=True)
    else:
        saida.save(buf, format='PNG', optimize=True)
    raw = buf.getvalue()
    if len(raw) > 1024 * 1024:                       # trava de sanidade
        raise ValueError('logo processado ficou grande demais (>1MB)')
    return 'data:%s;base64,%s' % (mime, base64.b64encode(raw).decode('ascii'))


@main_bp.route('/admin/cardapio-atacado/regras', methods=['GET', 'POST'])
@login_required
@admin_required
def cardapio_atacado_regras():
    """Tela pro dono escrever/mudar as regras do pedido de atacado que saem no
    cardápio. Texto livre por campo, salvo em AppConfig; vazio some do cardápio."""
    from flask import flash, redirect, url_for

    from app.models import AppConfig
    if request.method == 'POST':
        for chave, _label, _ph in CARDAPIO_ATACADO_CAMPOS:
            AppConfig.set(_CARDAPIO_ATACADO_PREFIXO + chave,
                          (request.form.get(chave) or '').strip())
        # Metodos de preparo: gravar ate' vazio ('' = dono apagou, bloco
        # some; so' a chave AUSENTE cai no default — ver _preparo_atacado).
        AppConfig.set(_CARDAPIO_PREPARO_KEY,
                      (request.form.get('preparo') or '').strip())
        # Quem somos nós: mesmo contrato (vazio = escondido de proposito).
        AppConfig.set(_CARDAPIO_QUEM_SOMOS_KEY,
                      (request.form.get('quem_somos') or '').strip())
        # Slogan do hero: mesmo contrato (vazio = linha some). Cap 200 no
        # servidor (o maxlength do input é só client-side; slogan gigante
        # estouraria a faixa escura da capa do PDF).
        AppConfig.set(_CARDAPIO_SLOGAN_KEY,
                      (request.form.get('slogan') or '').strip()[:200])
        # Ordem das seções (drag-and-drop): só mexe se o campo veio no form
        # (POST antigo/teste sem o campo não apaga a ordem salva; '' =
        # navegador sem JS, hidden nunca preenchido — ignorar em silêncio,
        # senão todo save sem JS flasharia aviso falso). JSON inválido ou
        # não-lista = flash, nunca sobrescrever em silêncio. Dedupe
        # preservando ordem (lista com repetido desenharia o bloco 2x).
        import json
        for campo, cfg_key in (('ordem_categorias',
                                _CARDAPIO_ORDEM_CATS_KEY),
                               ('ordem_secoes',
                                _CARDAPIO_ORDEM_SECOES_KEY)):
            raw = request.form.get(campo)
            if not raw:
                continue
            try:
                lst = json.loads(raw)
                if not isinstance(lst, list):
                    raise ValueError('esperava lista')
                lst = list(dict.fromkeys(
                    str(x).strip()[:80] for x in lst if str(x).strip()
                ))[:100]
                AppConfig.set(cfg_key, json.dumps(lst, ensure_ascii=False))
            except (ValueError, TypeError):
                flash('Ordem das seções veio inválida e NÃO foi salva '
                      '(campo %s).' % campo, 'warning')
        db.session.commit()
        flash('Regras do atacado salvas. Elas já aparecem no cardápio.', 'success')
        return redirect(url_for('main.cardapio', tipo='atacado'))
    atuais = {chave: (AppConfig.get(_CARDAPIO_ATACADO_PREFIXO + chave) or '')
              for chave, _l, _p in CARDAPIO_ATACADO_CAMPOS}
    # Categorias existentes (união dos 3 tipos) + as da ordem SALVA que
    # hoje não têm item precificado (achado do revisor: sem a união, um
    # save qualquer regravaria a ordem só com as visíveis e a posição da
    # categoria "adormecida" evaporava quando ela voltasse), já na ordem
    # vigente, pro drag-and-drop da ordem das seções.
    cats_ordem = {}
    for t in ('atacado', 'loja', 'site'):
        cats_t, _ = _cardapio_categorias(t)
        for c in cats_t:
            cats_ordem.setdefault(c, True)
    for c in _ordem_categorias_salva():
        cats_ordem.setdefault(c, True)
    cats_ordem = list(_aplicar_ordem_categorias(cats_ordem))
    return render_template('admin/cardapio_atacado_regras.html',
                           campos=CARDAPIO_ATACADO_CAMPOS, atuais=atuais,
                           cats_ordem=cats_ordem,
                           secoes_ordem=_ordem_secoes(),
                           secoes_labels=SECOES_LABELS,
                           slogan_raw=_slogan_raw(),
                           preparo_raw=_preparo_atacado_raw(),
                           quem_somos_raw=_quem_somos_raw(),
                           quem_somos_foto=_quem_somos_foto_src(),
                           quem_somos_foto_custom=bool(
                               (AppConfig.get(_CARDAPIO_QS_FOTO_KEY)
                                or '').strip()),
                           logo=_cardapio_logo())


@main_bp.route('/admin/cardapio-atacado/logo', methods=['POST'])
@login_required
@admin_required
def cardapio_logo_upload():
    """Sobe o logotipo do cardapio (hero da tela + capa do PDF). Checkbox
    'branco' (default) transforma a marca em silhueta branca pro fundo
    escuro; desmarcado mantem a imagem fiel. Guarda data URI em AppConfig."""
    from flask import flash, redirect, url_for

    from app.models import AppConfig
    back = redirect(url_for('main.cardapio_atacado_regras'))
    f = request.files.get('logo_arquivo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'danger')
        return back
    if not (f.mimetype or '').startswith('image/'):
        flash('Arquivo não é imagem.', 'danger')
        return back
    data = f.read()
    if len(data) > 25 * 1024 * 1024:
        flash('Imagem muito grande (>25MB).', 'danger')
        return back
    branco = bool(request.form.get('branco'))
    try:
        uri = _processar_logo_cardapio(data, branco=branco)
    except ValueError as e:
        flash('Erro processando logo: %s' % e, 'danger')
        return back
    AppConfig.set(_CARDAPIO_LOGO_KEY, uri)
    db.session.commit()
    flash('Logotipo salvo. Já aparece no cardápio e no PDF.', 'success')
    return back


@main_bp.route('/admin/cardapio-atacado/logo/remover', methods=['POST'])
@login_required
@admin_required
def cardapio_logo_remover():
    from flask import flash, redirect, url_for

    from app.models import AppConfig
    AppConfig.set(_CARDAPIO_LOGO_KEY, '')
    db.session.commit()
    flash('Logotipo removido — o cardápio volta ao texto "O Pão".', 'info')
    return redirect(url_for('main.cardapio_atacado_regras'))


@main_bp.route('/admin/cardapio-atacado/quem-somos-foto', methods=['POST'])
@login_required
@admin_required
def cardapio_quem_somos_foto_upload():
    """Troca a foto do "Quem somos nós" (tela + PDF). Grava data URI em
    AppConfig por cima do default estático (a fachada da loja)."""
    from flask import flash, redirect, url_for

    from app.models import AppConfig
    back = redirect(url_for('main.cardapio_atacado_regras'))
    f = request.files.get('qs_foto_arquivo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'danger')
        return back
    if not (f.mimetype or '').startswith('image/'):
        flash('Arquivo não é imagem.', 'danger')
        return back
    data = f.read()
    if len(data) > 25 * 1024 * 1024:
        flash('Imagem muito grande (>25MB).', 'danger')
        return back
    try:
        uri = _processar_foto_quem_somos(data)
    except ValueError as e:
        flash('Erro processando a foto: %s' % e, 'danger')
        return back
    AppConfig.set(_CARDAPIO_QS_FOTO_KEY, uri)
    db.session.commit()
    flash('Foto do "Quem somos nós" salva. Já aparece no cardápio e no PDF.',
          'success')
    return back


@main_bp.route('/admin/cardapio-atacado/quem-somos-foto/remover',
               methods=['POST'])
@login_required
@admin_required
def cardapio_quem_somos_foto_remover():
    from flask import flash, redirect, url_for

    from app.models import AppConfig
    AppConfig.set(_CARDAPIO_QS_FOTO_KEY, '')
    db.session.commit()
    flash('Foto personalizada removida — volta a foto padrão da fachada.',
          'info')
    return redirect(url_for('main.cardapio_atacado_regras'))


def _regra_do_menu(produto):
    """{'total', 'max'} do menu pelo helper canonico, ou None se nao e menu.
    Fonte unica: `loja_menu.regras` ja normaliza "em branco = sem teto" e
    "teto acima do total"."""
    if not getattr(produto, 'menu_configuravel', False):
        return None
    from app.services import loja_menu
    total, teto = loja_menu.regras(produto)
    return {'total': total, 'max': teto}


def _url_site_do_item(kind, item_id, nome):
    """URL ABSOLUTA da pagina do item na loja publica (LOJA_BASE_URL —
    opao.online, nao o dominio da gestao). Absoluta porque o cardapio PDF
    e aberto FORA do navegador (WhatsApp, e-mail, impressao): link
    relativo nao levaria a lugar nenhum."""
    base = (current_app.config.get('LOJA_BASE_URL')
            or 'https://opao.online').rstrip('/')
    from app.services.loja_catalogo import href_publico
    return base + href_publico(kind, item_id, nome)


def _galeria_explodida(produto):
    """Fotos pra "explodir" a cesta no PDF do cardapio (dono 26/07/2026:
    "que o cardapio PDF dos minis exploda para trazer as fotos que estao na
    receita e tambem todas as fotos a mais que vou adicionar no produto").

    Devolve `[{'nome', 'img_ref'|'imagem_url'}]` — a MESMA forma que
    `cardapio_pdf._bytes_foto` ja sabe ler:

    1. fotos EXTRAS do proprio produto (`CatalogoFoto`);
    2. por componente-receita: a CAPA dela + as extras dela.

    Cesta sem componentes e sem extras devolve [] (nada muda no PDF).
    A capa do proprio produto NAO entra: ela ja e a foto do card.

    SO MENU CONFIGURAVEL (decisao do dono 26/07/2026: "nao precisa para
    todas as cestas, queria somente para os minis por enquanto"). A regra
    fecha com o sentido do bloco: num menu que o cliente MONTA, as fotos
    explodidas sao as OPCOES que ele pode escolher — numa cesta de
    composicao fixa seriam so ilustracao. Se um dia uma cesta fixa precisar
    explodir, o caminho e um checkbox proprio no cadastro, nao afrouxar
    este gate (voltaria a explodir tudo)."""
    from app.models import CatalogoFoto
    if not getattr(produto, 'menu_configuravel', False):
        return []
    itens = getattr(produto, 'itens', None) or []
    extras_prod = (CatalogoFoto.query
                   .filter_by(kind='produto', item_id=produto.id)
                   .order_by(CatalogoFoto.ordem.asc(),
                             CatalogoFoto.id.asc()).all())
    if not itens and not extras_prod:
        return []
    # Fotos do PROPRIO produto: sem preco (retratam o menu inteiro, nao um
    # mini). Preco so nas dos COMPONENTES — e o preco POR UNIDADE dentro do
    # menu (`ProdutoItem.preco_menu`), pedido do dono em 26/07/2026.
    out = [{'nome': produto.nome, 'imagem_url': f.dropbox_url, 'preco': None}
           for f in extras_prod if f.dropbox_url]
    for pi in itens:
        if pi.tipo != 'receita' or not pi.receita_id:
            continue
        r = pi.receita
        if r is None:
            continue
        pm = getattr(pi, 'preco_menu', None)
        preco = float(pm) if pm is not None else None
        if r.imagem_dropbox_url or r.imagem_blob:
            out.append({'nome': r.nome, 'img_ref': ('receita', r.id),
                        'preco': preco})
        for f in (CatalogoFoto.query
                  .filter_by(kind='receita', item_id=r.id)
                  .order_by(CatalogoFoto.ordem.asc(),
                            CatalogoFoto.id.asc()).all()):
            if f.dropbox_url:
                out.append({'nome': r.nome, 'imagem_url': f.dropbox_url,
                            'preco': preco})
    return out


def _cardapio_categorias(tipo):
    """Monta as categorias do cardápio — FONTE ÚNICA da tela E do PDF
    (19/07/2026): divergência aqui = cardápio impresso diferente do site.
    Cada item traz `imagem_url` (a tela usa) e `img_ref` ('receita'|'produto',
    id) quando a foto vive no banco/Dropbox (o PDF resolve os bytes por ele).
    Retorna (categorias, regras)."""
    # defer(imagem_blob) — listagem nao precisa do blob (pode ter 100KB+ cada).
    # IDs com foto (blob OU Dropbox URL) vem em query separada.
    from sqlalchemy.orm import defer
    receitas = Receita.query.options(
        defer(Receita.imagem_blob),
        defer(Receita.imagem_mimetype),
    ).filter(Receita.arquivada_em.is_(None)) \
     .order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.options(
        defer(Produto.imagem_blob),
        defer(Produto.imagem_mimetype),
    ).filter_by(ativo=True).order_by(Produto.categoria, Produto.nome).all()

    from sqlalchemy import or_
    receitas_com_foto = {r[0] for r in db.session.query(Receita.id).filter(
        or_(Receita.imagem_blob.isnot(None),
            Receita.imagem_dropbox_url.isnot(None))).all()}
    produtos_com_foto = {p[0] for p in db.session.query(Produto.id).filter(
        or_(Produto.imagem_blob.isnot(None),
            Produto.imagem_dropbox_url.isnot(None))).all()}

    campo = {'atacado': 'preco_venda', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr = campo.get(tipo, 'preco_venda')

    categorias = {}

    # Receitas fabricadas
    for r in receitas:
        preco = getattr(r, attr, None) or (r.preco_venda if tipo == 'atacado' else None)
        if not preco or preco <= 0:
            continue
        cat = r.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        com_foto = r.id in receitas_com_foto
        if com_foto:
            img = url_for('main.cardapio_img', tipo='receita', id=r.id)
        else:
            img = r.imagem_url
        # Descricao SO no atacado (ditado do dono 20/07/2026) — loja/site
        # seguem como eram; produto continua com p.descricao propria.
        desc = (r.descricao_atacado or '').strip() if tipo == 'atacado' else ''
        categorias[cat].append({
            'nome': r.nome,
            'peso_unitario': r.peso_unitario,
            'descricao': desc or None,
            'preco_venda': preco,
            'imagem_url': img,
            'img_ref': ('receita', r.id) if com_foto else None,
            # Link pra pagina do item no SITE — o PDF do cardapio deixa o
            # card CLICAVEL (dono 26/07/2026: "quero que o menu seja
            # clicavel para levar o cliente ate o produto do site"). So quem
            # esta PUBLICADO (preco_site > 0) ganha link; o resto nao tem
            # pagina e levaria o cliente a um 404.
            'href_site': (_url_site_do_item('receita', r.id, r.nome)
                          if (r.preco_site or 0) > 0 else None),
        })

    # Produtos cadastrados (cestas, kits, etc.)
    campo_prod = {'atacado': 'preco_atacado', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr_prod = campo_prod.get(tipo, 'preco_atacado')
    for p in produtos:
        preco = getattr(p, attr_prod, None)
        if not preco or preco <= 0:
            continue
        cat = p.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        com_foto = p.id in produtos_com_foto
        if com_foto:
            img = url_for('main.cardapio_img', tipo='produto', id=p.id)
        else:
            img = p.imagem_url
        categorias[cat].append({
            'nome': p.nome,
            'peso_unitario': None,
            'descricao': p.descricao,
            'preco_venda': preco,
            'imagem_url': img,
            'img_ref': ('produto', p.id) if com_foto else None,
            'href_site': (_url_site_do_item('produto', p.id, p.nome)
                          if (p.preco_site or 0) > 0 else None),
            # "Explodir" a cesta no PDF (dono 26/07/2026): as fotos dos
            # COMPONENTES (capa + galeria de cada receita) mais as fotos
            # EXTRAS do proprio produto. So o PDF usa; a tela segue igual.
            'galeria': _galeria_explodida(p),
            # Regras do menu pro PDF escrever a observacao de pedido minimo
            # (dono 26/07/2026). Vem do helper CANONICO `loja_menu.regras` —
            # reimplementar os defaults aqui fez o PDF prometer "no maximo 10
            # de cada" DEPOIS do dono ter tirado a regra (achado de revisao):
            # o site permitia 30 de um so e o cardapio impresso dizia o
            # contrario. None em produto que nao e menu.
            'menu_regra': _regra_do_menu(p),
            'preco_a_partir': False,      # sobrescrito abaixo em menu
        })
        # MENU CONFIGURAVEL: o preco do cardapio vira o MINIMO possivel, com
        # "a partir de" (dono 26/07/2026). O cliente monta como quiser, entao
        # anunciar o preco fixo do cadastro seria prometer um valor que ele
        # pode nao pagar. MESMO numero nos tres cardapios: o preco do menu
        # sai do `preco_menu` dos componentes, que e unico (nao ha versao
        # atacado/loja/site dele).
        if getattr(p, 'menu_configuravel', False):
            from app.services import loja_menu
            minimo = loja_menu.preco_minimo(p)
            if minimo:
                categorias[cat][-1]['preco_venda'] = float(minimo)
                categorias[cat][-1]['preco_a_partir'] = True
            else:
                # Menu nao precificavel (slot sem preco ou total inalcancavel)
                # esta FORA do site — deixa-lo no cardapio com o preco do
                # cadastro faria a "fonte unica" contradizer a vitrine, e o
                # link levaria a um 404 (achado de revisao 26/07/2026).
                current_app.logger.warning(
                    'cardapio: menu %r fora da lista — sem preco calculavel.',
                    p.nome)
                categorias[cat].pop()
                continue

    regras = _regras_atacado() if tipo == 'atacado' else []
    # Ordem final das categorias (drag-and-drop do dono, 21/07/2026) — aqui
    # na fonte única, então tela e PDFs saem SEMPRE na mesma ordem.
    return _aplicar_ordem_categorias(categorias), regras


@main_bp.route('/cardapio')
@login_required
def cardapio():
    tipo = request.args.get('tipo', 'atacado')
    categorias, regras = _cardapio_categorias(tipo)
    preparo = _preparo_atacado() if tipo == 'atacado' else []
    return render_template('main/cardapio.html', categorias=categorias,
                           tipo=tipo, regras=regras, preparo=preparo,
                           quem_somos=_quem_somos(),
                           quem_somos_foto=_quem_somos_foto_src(),
                           ordem_secoes=_ordem_secoes(),
                           slogan=_slogan(),
                           logo=_cardapio_logo())


@main_bp.route('/cardapio.pdf')
@login_required
def cardapio_pdf_export():
    """Cardápio em PDF pronto pra ENVIAR ao cliente (19/07/2026): o botão
    Imprimir usava window.print() e o navegador re-paginava o site de
    qualquer jeito (cards cortados, URL no rodapé — fotos do dono). Mesma
    regra da impressão de pedidos: PDF do servidor, paginação controlada."""
    from app.services import cardapio_pdf as svc
    tipo = request.args.get('tipo', 'atacado')
    if tipo not in ('atacado', 'loja', 'site'):
        tipo = 'atacado'
    # formato=mobile (21/07/2026): página estreita 9:16 pra mandar por
    # WhatsApp — o A4 aberto no celular fica com o texto minúsculo.
    formato = request.args.get('formato', 'a4')
    if formato not in ('a4', 'mobile'):
        formato = 'a4'
    categorias, regras = _cardapio_categorias(tipo)
    preparo = _preparo_atacado() if tipo == 'atacado' else []
    conteudo = svc.gerar_cardapio_pdf(tipo, categorias, regras,
                                      logo=_cardapio_logo(), preparo=preparo,
                                      quem_somos=_quem_somos(),
                                      quem_somos_foto=_quem_somos_foto_bytes(),
                                      formato=formato,
                                      ordem_secoes=_ordem_secoes(),
                                      slogan=_slogan())
    resp = current_app.response_class(conteudo, mimetype='application/pdf')
    sufixo = '_mobile' if formato == 'mobile' else ''
    resp.headers['Content-Disposition'] = (
        'inline; filename="cardapio_%s%s.pdf"' % (tipo, sufixo))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# Foto de catálogo (receita/produto): resolução e qualidade da compressão.
# 1600px, NÃO os 700 do default de `comprimir_imagem` — a foto do produto é o
# argumento de venda do site: a página exibe ~830px de largura e tela retina
# pede o dobro em pixels reais, então 700px saía visivelmente pixelado
# (relatado pelo dono em 26/07/2026). 1600 q88 ≈ 250-450KB, servido pela CDN
# do Dropbox. Fotos JÁ salvas continuam em 700 — pra melhorar, re-suba.
CARDAPIO_IMG_MAX_PX = 1600
CARDAPIO_IMG_QUALITY = 88


@main_bp.route('/cardapio-img/<tipo>/<int:id>')
def cardapio_img(tipo, id):
    """Serve imagem de receita/produto. Prioriza Dropbox URL (M6+).

    Fallback pra BLOB do banco se foto ainda nao foi migrada.
    """
    import hashlib

    from flask import abort, make_response
    from flask import request as flask_request
    from sqlalchemy.orm import load_only

    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = (Receita.query.options(
            load_only(Receita.imagem_blob, Receita.imagem_mimetype,
                      Receita.imagem_dropbox_url)
        ).get(id))
    elif tipo == 'produto':
        obj = (Produto.query.options(
            load_only(Produto.imagem_blob, Produto.imagem_mimetype,
                      Produto.imagem_dropbox_url)
        ).get(id))
    else:
        abort(404)
    if not obj:
        abort(404)
    if obj.imagem_dropbox_url:
        return redirect(obj.imagem_dropbox_url, code=302)
    if not obj.imagem_blob:
        abort(404)
    etag = hashlib.md5(obj.imagem_blob).hexdigest()[:16]
    if flask_request.headers.get('If-None-Match') == etag:
        return ('', 304)
    resp = make_response(obj.imagem_blob)
    resp.mimetype = obj.imagem_mimetype or 'image/jpeg'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    resp.headers['ETag'] = etag
    return resp


@main_bp.route('/cardapio-img/<tipo>/<int:id>/upload', methods=['POST'])
@login_required
def cardapio_img_upload(tipo, id):
    """Recebe upload de foto pra receita/produto. PIL comprime
    automaticamente: redimensiona pra 700px max + JPEG quality 82.
    Aceita ate 25MB no upload (celular tira fotos enormes), mas o que
    fica no banco e ~50-150KB."""
    from flask import abort, flash, redirect, url_for

    from app.extensions import db as _db
    from app.models import Produto, Receita
    if not current_user.is_admin():
        abort(403)
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
        url_back = url_for('receitas.ficha', id=id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
        url_back = url_for('produtos.detalhe', id=id)
    else:
        abort(404)

    f = request.files.get('imagem_arquivo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'danger')
        return redirect(url_back)
    if not (f.mimetype or '').startswith('image/'):
        flash('Arquivo nao eh imagem.', 'danger')
        return redirect(url_back)
    data = f.read()
    if not data:
        flash('Arquivo vazio.', 'danger')
        return redirect(url_back)
    if len(data) > 25 * 1024 * 1024:
        flash(f'Imagem muito grande ({len(data)//1024//1024}MB > 25MB). '
              'Tira de novo com qualidade menor.', 'danger')
        return redirect(url_back)

    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    try:
        # 1600px (não os 700 do default): a foto do produto é o ARGUMENTO DE
        # VENDA do site. A página do produto exibe ~830px de largura e tela
        # retina pede o dobro em pixels reais — com 700px o navegador
        # esticava 2,4x e a foto saía visivelmente pixelada (26/07/2026).
        # 1600 q88 ≈ 250-450KB, servido pela CDN do Dropbox.
        final = comprimir_imagem(data, max_size=CARDAPIO_IMG_MAX_PX,
                                 quality=CARDAPIO_IMG_QUALITY)
        tamanho_kb = len(final) // 1024
        if dropbox_storage.disponivel():
            # Path deterministico — overwrite ao re-upload do mesmo item.
            path = f'/cardapio/{tipo}/{obj.id}.jpg'
            info = dropbox_storage.upload_publico(
                final, path, mode='overwrite', autorename=False)
            obj.imagem_dropbox_url = info['url']
            obj.imagem_storage_path = info['storage_path']
            obj.imagem_blob = None  # libera legado
        else:
            obj.imagem_blob = final
        obj.imagem_mimetype = 'image/jpeg'
    except Exception as e:  # noqa: BLE001
        flash(f'Erro processando imagem: {e}', 'danger')
        return redirect(url_back)

    _db.session.commit()
    flash(f'Imagem salva ({tamanho_kb} KB apos compressao).', 'success')
    return redirect(url_back)


def _norm(s):
    import unicodedata
    if not s:
        return ''
    nfd = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower().strip()


@main_bp.route('/cardapio-img/revisar')
@login_required
def cardapio_img_revisar():
    """Grid de revisao das fotos atribuidas. Admin ve thumbnail + nome,
    identifica matches errados e remove com 1 clique. Defer blob pra nao
    estourar RAM — o thumbnail eh servido pela rota /cardapio-img/<tipo>/<id>."""
    from flask import abort
    from sqlalchemy import or_
    from sqlalchemy.orm import defer
    if not current_user.is_admin():
        abort(403)
    receitas_com_foto = (Receita.ativas()
                         .options(defer(Receita.imagem_blob),
                                  defer(Receita.imagem_mimetype))
                         .filter(or_(Receita.imagem_blob.isnot(None),
                                     Receita.imagem_dropbox_url.isnot(None)))
                         .order_by(Receita.categoria, Receita.nome).all())
    produtos_com_foto = (Produto.query
                         .options(defer(Produto.imagem_blob),
                                  defer(Produto.imagem_mimetype))
                         .filter(Produto.ativo.is_(True))
                         .filter(or_(Produto.imagem_blob.isnot(None),
                                     Produto.imagem_dropbox_url.isnot(None)))
                         .order_by(Produto.categoria, Produto.nome).all())
    return render_template('main/cardapio_revisar.html',
                            receitas=receitas_com_foto,
                            produtos=produtos_com_foto)


@main_bp.route('/cardapio-img/<tipo>/<int:id>/remover', methods=['POST'])
@login_required
def cardapio_img_remover(tipo, id):
    from flask import abort, flash, redirect, url_for

    from app.extensions import db as _db
    from app.models import Produto, Receita
    if not current_user.is_admin():
        abort(403)
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
        url_back = url_for('receitas.ficha', id=id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
        url_back = url_for('produtos.detalhe', id=id)
    else:
        abort(404)
    # Permite redirect pra revisar (next=revisar) em vez da ficha
    if request.form.get('next') == 'revisar':
        url_back = url_for('main.cardapio_img_revisar')
    # Delete Dropbox file best-effort antes de limpar refs
    if obj.imagem_storage_path:
        from app.services import dropbox_storage
        dropbox_storage.deletar(obj.imagem_storage_path)
    obj.imagem_blob = None
    obj.imagem_mimetype = None
    obj.imagem_url = None
    obj.imagem_dropbox_url = None
    obj.imagem_storage_path = None
    _db.session.commit()
    flash('Imagem removida.', 'info')
    return redirect(url_back)


# Galeria de fotos EXTRAS (26/07/2026, pedido do dono: "pelo menos 4").
# A capa continua sendo `imagem_dropbox_url` — estas sao as SEGUINTES.
GALERIA_MAX_FOTOS = 8


def apagar_galeria_do_item(kind, item_id):
    """Apaga as fotos EXTRAS de uma receita/produto (linhas + arquivos no
    Dropbox). Chamado ANTES do delete do item: `CatalogoFoto` endereca por
    (kind, item_id) sem FK, entao nada apagaria essas linhas sozinho e elas
    virariam lixo permanente — e a rota de remover ficaria inalcancavel (o
    `_alvo_catalogo` faz get_or_404 no item que ja nao existe). Achado de
    revisao 26/07/2026. Best-effort: nao commita nem levanta."""
    from app.models import CatalogoFoto
    fotos = CatalogoFoto.query.filter_by(kind=kind, item_id=item_id).all()
    if not fotos:
        return 0
    from app.services import dropbox_storage
    for f in fotos:
        if f.storage_path:
            try:
                dropbox_storage.deletar(f.storage_path)
            except Exception:  # noqa: BLE001 — arquivo orfao nao trava o delete
                current_app.logger.warning(
                    'galeria: nao deletou %s no Dropbox', f.storage_path)
        db.session.delete(f)
    return len(fotos)


def _alvo_catalogo(tipo, id):
    """(objeto, url_de_volta) pra receita/produto — mesma resolucao das rotas
    de imagem principal. 404 em tipo desconhecido."""
    from flask import abort, url_for

    from app.models import Produto, Receita
    if tipo == 'receita':
        return Receita.query.get_or_404(id), url_for('receitas.ficha', id=id)
    if tipo == 'produto':
        return Produto.query.get_or_404(id), url_for('produtos.detalhe', id=id)
    abort(404)


@main_bp.route('/cardapio-img/<tipo>/<int:id>/galeria', methods=['POST'])
@login_required
def cardapio_galeria_upload(tipo, id):
    """Sobe UMA OU MAIS fotos extras pra a galeria do item.

    Cada foto vira uma linha `CatalogoFoto` + um arquivo proprio no Dropbox
    (path com o id da linha, pra o remover deletar exatamente a dela). Se o
    Dropbox nao estiver configurado a galeria NAO funciona (nao ha fallback
    BLOB de proposito: seria ressuscitar a divida que o M6 esta pagando)."""
    from flask import abort, flash, redirect

    from app.extensions import db as _db
    from app.models import CatalogoFoto
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    if not current_user.is_admin():
        abort(403)
    obj, url_back = _alvo_catalogo(tipo, id)
    if not dropbox_storage.disponivel():
        flash('Dropbox nao configurado — galeria indisponivel.', 'danger')
        return redirect(url_back)

    ja_tem = CatalogoFoto.query.filter_by(kind=tipo, item_id=obj.id).count()
    arquivos = [f for f in request.files.getlist('galeria_arquivos')
                if f and f.filename]
    if not arquivos:
        flash('Selecione ao menos uma imagem.', 'danger')
        return redirect(url_back)
    livres = GALERIA_MAX_FOTOS - ja_tem
    if livres <= 0:
        flash(f'A galeria ja tem o maximo de {GALERIA_MAX_FOTOS} fotos. '
              'Remova alguma antes de subir outra.', 'warning')
        return redirect(url_back)
    ignoradas = max(0, len(arquivos) - livres)
    arquivos = arquivos[:livres]

    salvas, erros = 0, []
    for f in arquivos:
        if not (f.mimetype or '').startswith('image/'):
            erros.append(f'{f.filename}: nao e imagem')
            continue
        data = f.read()
        if not data:
            erros.append(f'{f.filename}: arquivo vazio')
            continue
        if len(data) > 25 * 1024 * 1024:
            erros.append(f'{f.filename}: maior que 25MB')
            continue
        try:
            final = comprimir_imagem(data, max_size=CARDAPIO_IMG_MAX_PX,
                                     quality=CARDAPIO_IMG_QUALITY)
            # A linha nasce ANTES do upload pra o path carregar o id dela —
            # sem isso duas fotos do mesmo item disputariam o mesmo arquivo.
            foto = CatalogoFoto(kind=tipo, item_id=obj.id, dropbox_url='',
                                ordem=ja_tem + salvas + 1)
            _db.session.add(foto)
            _db.session.flush()
            info = dropbox_storage.upload_publico(
                final, f'/cardapio/{tipo}/{obj.id}-g{foto.id}.jpg',
                mode='overwrite', autorename=False)
            foto.dropbox_url = info['url']
            foto.storage_path = info['storage_path']
            # COMMIT POR FOTO: o `rollback` do except desfaz a transacao
            # INTEIRA, entao com commit so no fim uma falha na 3a foto
            # apagava as 2 primeiras do banco — mas os arquivos ja estavam
            # no Dropbox e a mensagem dizia "3 adicionadas" (achado de
            # revisao 26/07/2026). Commitando por foto, o que subiu fica.
            _db.session.commit()
            salvas += 1
        except Exception as e:  # noqa: BLE001 — uma foto ruim nao derruba as outras
            _db.session.rollback()
            erros.append(f'{f.filename}: {e}')

    if salvas:
        flash(f'{salvas} foto(s) adicionada(s) a galeria.', 'success')
    if ignoradas:
        flash(f'{ignoradas} foto(s) ignorada(s) — limite de '
              f'{GALERIA_MAX_FOTOS}.', 'warning')
    for e in erros:
        flash(f'Erro: {e}', 'danger')
    return redirect(url_back)


@main_bp.route('/cardapio-img/galeria/<int:foto_id>/remover', methods=['POST'])
@login_required
def cardapio_galeria_remover(foto_id):
    """Remove UMA foto da galeria (e o arquivo no Dropbox)."""
    from flask import abort, flash, redirect

    from app.extensions import db as _db
    from app.mod…40457 tokens truncated…strip():
                try:
                    bruto = json.loads(comps[i])
                    if isinstance(bruto, dict):
                        comp = bruto
                except (TypeError, ValueError):
                    comp = None
            try:
                itens.append({'kind': kind, 'id': int(sid),
                              'qtd': int(q or 0), 'comp': comp})
            except (TypeError, ValueError):
                continue
        data_str = (request.form.get('data_entrega') or '').strip()
        try:
            data_ent = _date.fromisoformat(data_str) if data_str else None
        except ValueError:
            data_ent = None
        # Endereco: campos estruturados; a linha (snapshot do painel/motorista)
        # e MONTADA deles — "logradouro, numero - bairro, cidade/uf".
        _end = {k: (request.form.get('endereco_' + k) or '').strip()
                for k in ('cep', 'logradouro', 'numero', 'complemento',
                          'bairro', 'cidade', 'uf')}
        _linha = _end['logradouro']
        if _end['numero']:
            _linha += ', ' + _end['numero']
        if _end['complemento']:
            _linha += ' (' + _end['complemento'] + ')'
        if _end['bairro']:
            _linha += ' - ' + _end['bairro']
        if _end['cidade']:
            _linha += ', ' + _end['cidade'] + ('/' + _end['uf'] if _end['uf'] else '')
        endereco = dict(_end, linha=_linha.strip(' ,-'))
        try:
            pedido = div_svc.criar_divulgacao(
                itens=itens, modo_entrega=modo,
                loja_retirada_id=(int(request.form['loja_retirada_id'])
                                  if request.form.get('loja_retirada_id')
                                  else None),
                nome_destinatario=request.form.get('nome_destinatario'),
                telefone=request.form.get('telefone'),
                data_entrega=data_ent,
                janela_entrega=request.form.get('janela_entrega'),
                endereco=endereco,
                cartinha=request.form.get('cartinha'),
                usuario_id=current_user.id,
                # Dono lança pra HOJE se quiser (decisão 08/08/2026);
                # marketing segue "a partir de amanhã" (21/07/2026).
                permitir_hoje=current_user.is_dono())
        except ValueError as e:
            flash('Não deu pra lançar a divulgação: %s' % e, 'danger')
            return redirect(url_for('main.loja_online_divulgacao'))
        flash('⭐ Divulgação %s lançada — já aparece no painel de entregas '
              'do dia %s.' % (pedido.codigo,
                              data_ent.strftime('%d/%m') if data_ent else '—'),
              'success')
        # Marketing não acessa o detalhe do pedido (gerente_required) — volta
        # pro form com o flash de sucesso. Admin/gerente vão pro detalhe.
        if current_user.is_admin() or current_user.is_gerente():
            return redirect(url_for('main.loja_online_pedido_detalhe',
                                    codigo=pedido.codigo))
        return redirect(url_for('main.loja_online_divulgacao'))

    amanha = (hoje_brt() + timedelta(days=1)).isoformat()
    # O calendário abre em amanhã (default planejado) mas o DONO pode voltar
    # até hoje (decisão 08/08/2026); marketing fica travado em amanhã.
    data_min = hoje_brt().isoformat() if current_user.is_dono() else amanha
    return render_template('admin/loja_online_divulgacao.html',
                           catalogo=_catalogo_divulgacao(),
                           menus=_menus_divulgacao(), lojas=lojas,
                           amanha=amanha, data_min=data_min)


@main_bp.route('/admin/loja-online/divulgacao/janelas')
@login_required
@divulgacao_required
def loja_online_divulgacao_janelas():
    """Janelas de horário válidas pra uma data/modo/endereço — MESMA regra do
    site (`loja_checkout.janelas_disponiveis`): agendada corta a 1ª janela da
    manhã quando o endereço está longe (distância do `consultar_frete`);
    retirada não tem distância. Alimenta o select do form (JS)."""
    from app.services import loja_checkout
    modo = (request.args.get('modo') or 'agendada').strip()
    data = (request.args.get('data') or '').strip() or None
    dist = None
    aviso = None
    if modo == 'agendada':
        partes = [request.args.get('logradouro'), request.args.get('numero'),
                  request.args.get('bairro'), request.args.get('cidade')]
        geo = ', '.join(p.strip() for p in partes if (p or '').strip())
        cep = (request.args.get('cep') or '').strip()
        if cep and cep not in geo:
            geo = ('%s, %s' % (geo, cep)) if geo else cep
        if geo:
            try:
                from app.services import frete
                r = frete.consultar_frete(geo)
                if r.get('ok'):
                    dist = r.get('distancia_km')
                    if r.get('fora_area'):
                        aviso = ('endereço fora do raio de entrega do site '
                                 '(%.1f km) — confira com a equipe'
                                 % (dist or 0))
            except Exception:  # noqa: BLE001 — fail-open: sem dist, todas as janelas
                current_app.logger.warning('divulgacao janelas: frete falhou',
                                           exc_info=True)
    janelas = loja_checkout.janelas_disponiveis(modo, data, distancia_km=dist)
    return jsonify(ok=True, janelas=janelas, distancia_km=dist, aviso=aviso)


@main_bp.route('/admin/loja-online/divulgacao/<codigo>/cancelar',
               methods=['POST'])
@login_required
@divulgacao_required
def loja_online_divulgacao_cancelar(codigo):
    """Cancela uma divulgacao devolvendo o estoque baixado (dono/marketing)."""
    from flask import flash, redirect, url_for

    from app.models import PedidoOnline
    from app.services import divulgacao as div_svc
    p = PedidoOnline.query.filter_by(codigo=codigo, divulgacao=True).first_or_404()
    res = div_svc.cancelar_divulgacao(p, usuario_id=current_user.id)
    if res.get('ja_cancelado'):
        flash('Esta divulgação já estava cancelada.', 'info')
    else:
        flash('Divulgação %s cancelada — estoque devolvido à loja.' % codigo,
              'success')
    return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))


@main_bp.route('/admin/loja-online/estoque-vitrine')
@login_required
def loja_online_estoque_vitrine():
    """Diagnóstico (owner): pra cada produto publicado no site, mostra o
    saldo na loja do site e se está EM ESTOQUE ou ESGOTADO (saldo 0 ou sem
    linha = esgotado). Nada some da vitrine — esgotado aparece com selo e sem
    botão de comprar. Use pra preencher estoque em `/pedidos/estoque-loja`."""
    from app.services import loja_catalogo
    from app.services.loja_pagamento import loja_origem_site
    loja = loja_origem_site()
    mapa = loja_catalogo._estoque_site_map() or {}
    itens = []
    for it in loja_catalogo.produtos_publicados():
        saldo = mapa.get((it['kind'], it['id']))  # None = sem linha
        itens.append({
            'nome': it['nome'], 'kind': it['kind'], 'id': it['id'],
            'categoria': it['categoria'], 'saldo': saldo,
            'esgotado': not (saldo and saldo > 0),
        })
    esgotados = [i for i in itens if i['esgotado']]
    return jsonify(
        loja_site=(loja.nome if loja else None),
        total_publicados=len(itens),
        em_estoque=len(itens) - len(esgotados),
        esgotados=len(esgotados),
        itens_esgotados=esgotados,
        itens=itens,
    )


@main_bp.route('/admin/loja-online')
@owner_required
def loja_online_dashboard():
    """Visão geral da loja online: contagens por status, faturamento por
    janela (hoje/semana/mês) e fila do que precisa de ação do admin."""
    from datetime import timedelta

    from sqlalchemy import func as _func

    from app.models import PedidoOnline
    from app.utils import agora
    hoje = agora().date()
    ini_semana = hoje - timedelta(days=hoje.weekday())
    ini_mes = hoje.replace(day=1)

    def _stats(desde):
        # Faturamento e contagem dos pedidos PAGOS (não cancelados) desde X.
        q = db.session.query(
            _func.coalesce(_func.sum(PedidoOnline.valor_total), 0),
            _func.count(PedidoOnline.id),
        ).filter(
            PedidoOnline.criado_em >= desde,
            PedidoOnline.status.in_(
                ('pago', 'em_preparo', 'a_caminho', 'entregue')),
        )
        valor, count = q.first()
        return {'valor': float(valor or 0), 'count': count or 0}

    janelas = {
        'hoje': _stats(hoje),
        'semana': _stats(ini_semana),
        'mes': _stats(ini_mes),
    }

    contagens = dict(db.session.query(PedidoOnline.status, _func.count())
                     .group_by(PedidoOnline.status).all())

    # Fila do admin: precisam de ação (pago = emitir NF + começar preparo;
    # em_preparo + a_caminho = entregar). Aguardando pagamento NÃO entra
    # (cliente é quem age — não atrapalha o admin).
    fila = (PedidoOnline.query
            .filter(PedidoOnline.status.in_(
                ('pago', 'em_preparo', 'a_caminho')))
            .order_by(PedidoOnline.data_entrega.asc().nullslast(),
                      PedidoOnline.criado_em.asc())
            .limit(15).all())

    from app.models import AppConfig
    return render_template(
        'admin/loja_online_dashboard.html',
        janelas=janelas, contagens=contagens, fila=fila,
        labels=_STATUS_PEDIDO_ONLINE_LABEL,
        logo_url=AppConfig.get('loja_logo_url'))


@main_bp.route('/admin/loja-online/pedidos')
@login_required
@gerente_required
def loja_online_pedidos():
    """Lista os pedidos do site (mais recentes primeiro), com filtros por
    status e por data de entrega (?data=YYYY-MM-DD ou intervalo
    ?data_ini=&data_fim=). Mostra a contagem por status (sempre global —
    bate com os botões de filtro)."""
    from datetime import date as _date

    from sqlalchemy import func as _func

    from app.models import PedidoOnline
    # Default ao abrir = "pago" (verde) — a operação do dia trabalha em cima
    # dos pagos. Pra ver tudo, clica na aba "Todos" (vai com ?status=todos).
    # Decisão do dono 22/06/2026.
    status_raw = request.args.get('status')
    status = 'pago' if status_raw is None else status_raw.strip()
    data_str = (request.args.get('data') or '').strip()
    data_ini_str = (request.args.get('data_ini') or '').strip()
    data_fim_str = (request.args.get('data_fim') or '').strip()

    def _parse(s):
        try:
            return _date.fromisoformat(s) if s else None
        except ValueError:
            return None

    data = _parse(data_str)
    data_ini = _parse(data_ini_str)
    data_fim = _parse(data_fim_str)

    q = PedidoOnline.query
    if status and status != 'todos':
        q = q.filter_by(status=status)
    if data:
        q = q.filter(PedidoOnline.data_entrega == data)
    else:
        if data_ini:
            q = q.filter(PedidoOnline.data_entrega >= data_ini)
        if data_fim:
            q = q.filter(PedidoOnline.data_entrega <= data_fim)

    pedidos = q.order_by(PedidoOnline.criado_em.desc()).limit(200).all()
    contagens = dict(db.session.query(PedidoOnline.status, _func.count())
                     .group_by(PedidoOnline.status).all())
    return render_template(
        'admin/loja_online_pedidos.html',
        pedidos=pedidos, status=status, contagens=contagens,
        total=sum(contagens.values()), labels=_STATUS_PEDIDO_ONLINE_LABEL,
        data=data_str, data_ini=data_ini_str, data_fim=data_fim_str,
        filtro_data_ativo=bool(data or data_ini or data_fim))


@main_bp.route('/admin/loja-online/buscar-pedidos')
@login_required
@gerente_required
def loja_online_pedidos_buscar():
    """Busca incremental (AJAX) por nome, telefone, e-mail ou código.
    Respeita o filtro de data ATIVO (passado nos params) — sem isso a busca
    sobrescreveria a lista filtrada por data com pedidos de outros dias,
    confundindo o operador (CLAUDE.md: filtros não podem se ignorar). Sem
    data ativa, busca em todos os pedidos."""
    from datetime import date as _date

    from sqlalchemy import or_

    from app.models import PedidoOnline
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return ''  # nada a buscar — o JS restaura a lista inicial
    termo = f'%{q}%'

    def _parse(s):
        try:
            return _date.fromisoformat(s) if s else None
        except ValueError:
            return None

    data = _parse((request.args.get('data') or '').strip())
    data_ini = _parse((request.args.get('data_ini') or '').strip())
    data_fim = _parse((request.args.get('data_fim') or '').strip())

    qry = PedidoOnline.query.filter(or_(
        PedidoOnline.nome_cliente.ilike(termo),
        PedidoOnline.telefone_cliente.ilike(termo),
        PedidoOnline.email_cliente.ilike(termo),
        PedidoOnline.codigo.ilike(termo),
    ))
    if data:
        qry = qry.filter(PedidoOnline.data_entrega == data)
    else:
        if data_ini:
            qry = qry.filter(PedidoOnline.data_entrega >= data_ini)
        if data_fim:
            qry = qry.filter(PedidoOnline.data_entrega <= data_fim)
    pedidos = (qry.order_by(PedidoOnline.criado_em.desc())
               .limit(50).all())
    return render_template('admin/_loja_online_pedidos_rows.html',
                           pedidos=pedidos, labels=_STATUS_PEDIDO_ONLINE_LABEL)


# Modos de entrega editáveis (espelha loja_checkout.criar_pedido).
_MODOS_ENTREGA = ('agendada', 'retirada', 'express')


def _detalhe_redirect(codigo):
    """Redireciona pro detalhe do pedido preservando o modo `embed` (popup do
    painel de entregas). Sem isso, ao salvar/avançar status dentro do iframe
    do painel a página voltaria com a sidebar do admin (embed perdido no
    redirect). Lê de `request.values` (cobre form POST e query string)."""
    embed = '1' if request.values.get('embed') else None
    return redirect(url_for('main.loja_online_pedido_detalhe',
                            codigo=codigo, embed=embed))


@main_bp.route('/admin/loja-online/pedidos/<codigo>')
@login_required
@gerente_required
def loja_online_pedido_detalhe(codigo):
    from app.models import PedidoOnline
    from app.services import loja_checkout, loja_pagamento
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    # Pedido corrigido após a NF (quantidade reduzida): versão de estoque > 0.
    # A tela avisa que a NF pode estar desatualizada (o Tiny não cancela por
    # aqui — correção é manual).
    estoque_reduzido = loja_pagamento._versao_estoque_atual(p) > 0
    return render_template('admin/loja_online_pedido_detalhe.html',
                           p=p, labels=_STATUS_PEDIDO_ONLINE_LABEL,
                           lojas=loja_checkout.lojas_retirada(),
                           modos=_MODOS_ENTREGA,
                           estoque_reduzido=estoque_reduzido,
                           expedicao_sinal=_expedicao_com_pedido(p))


@main_bp.route('/admin/loja-online/pedidos/<codigo>/editar', methods=['POST'])
@login_required
@gerente_required
def loja_online_pedido_editar(codigo):
    """Edita os dados LOGÍSTICOS/CONTATO do pedido — o que a operação precisa
    corrigir depois do pedido feito: cartinha, data/janela, endereço, contato,
    destinatário, modo de entrega e loja de retirada.

    NÃO mexe em DINHEIRO (itens, subtotal, frete, total ficam intactos —
    CLAUDE.md: dinheiro tem peso especial; mudar valor é reembolso/novo
    pedido). Trocar o endereço NÃO recalcula frete: o cliente já pagou; isto
    é só correção de destino pra entrega."""
    from datetime import date as _date

    from flask import flash

    from app.models import PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    f = request.form

    def _s(k):
        return (f.get(k) or '').strip()

    erros = []
    nome = _s('nome_cliente')
    email = _s('email_cliente')
    if not nome:
        erros.append('Nome do cliente é obrigatório.')
    if '@' not in email:
        erros.append('E-mail do cliente inválido.')
    modo = _s('modo_entrega') or p.modo_entrega
    if modo not in _MODOS_ENTREGA:
        erros.append('Modo de entrega inválido.')
    data_str = _s('data_entrega')
    data_entrega = None
    if data_str:
        try:
            data_entrega = _date.fromisoformat(data_str)
        except ValueError:
            erros.append('Data de entrega inválida (use o seletor).')
    if erros:
        for e in erros:
            flash(e, 'danger')
        return _detalhe_redirect(codigo)

    p.nome_cliente = nome
    p.email_cliente = email
    p.telefone_cliente = _s('telefone_cliente') or None
    p.nome_destinatario = _s('nome_destinatario') or None
    p.telefone_destinatario = _s('telefone_destinatario') or None
    p.modo_entrega = modo
    p.cartinha = _s('cartinha') or None
    p.data_entrega = data_entrega
    p.janela_entrega = _s('janela_entrega') or None

    if modo == 'retirada':
        try:
            p.loja_retirada_id = int(f.get('loja_retirada_id')) or None
        except (TypeError, ValueError):
            p.loja_retirada_id = None
    else:
        p.loja_retirada_id = None
        p.endereco_cep = _s('endereco_cep') or None
        p.endereco_logradouro = _s('endereco_logradouro') or None
        p.endereco_numero = _s('endereco_numero') or None
        p.endereco_complemento = _s('endereco_complemento') or None
        p.endereco_bairro = _s('endereco_bairro') or None
        p.endereco_cidade = _s('endereco_cidade') or None
        p.endereco_uf = (_s('endereco_uf')[:2].upper()) or None
        partes = [p.endereco_logradouro, p.endereco_numero,
                  p.endereco_complemento, p.endereco_bairro,
                  p.endereco_cidade, p.endereco_uf]
        p.endereco_entrega = ', '.join(x for x in partes if x) or None

    db.session.commit()
    current_app.logger.info('pedido online %s editado por uid=%s',
                            codigo, getattr(current_user, 'id', None))
    flash(f'Pedido {p.codigo} atualizado.', 'success')
    return _detalhe_redirect(codigo)


@main_bp.route('/admin/loja-online/pedidos/<codigo>/reenviar-emails',
               methods=['POST'])
@login_required
@gerente_required
def loja_online_pedido_reenviar_emails(codigo):
    """Reenvia os e-mails relevantes pro status atual do pedido, pro
    email_cliente ATUAL (corrija o e-mail antes, se estava errado).

    Caso 24/06/2026: cliente digitou hotmail.con; os 4 e-mails bouncearam.
    Fluxo: editar e-mail -> Salvar -> Reenviar e-mails.

    Quais manda, por status:
    - pago/em_preparo/a_caminho/entregue: "confirmado" (todos já pagaram).
    - a_caminho: + "a caminho".
    - entregue: + "entregue".
    - se a NF foi emitida: + "nota fiscal".
    Sempre manda "recebemos seu pedido" (base). Best-effort por e-mail —
    reporta quantos saíram OK."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import email as email_svc
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()

    destinatario = (p.email_cliente or '').strip()
    if '@' not in destinatario:
        flash('Pedido sem e-mail válido — corrija o e-mail do cliente antes '
              'de reenviar.', 'danger')
        return _detalhe_redirect(codigo)

    # Monta a lista de e-mails a reenviar conforme o status atual.
    envios = [('Recebemos seu pedido', email_svc.enviar_pedido_recebido)]
    pago_ou_alem = p.status in ('pago', 'em_preparo', 'a_caminho', 'entregue')
    if pago_ou_alem:
        envios.append(('Pedido confirmado', email_svc.enviar_confirmacao_pedido))
    if p.status == 'a_caminho':
        envios.append(('A caminho', email_svc.enviar_pedido_a_caminho))
    if p.status == 'entregue':
        envios.append(('Entregue', email_svc.enviar_pedido_entregue))
    if getattr(p, 'nf_emitida_em', None):
        envios.append(('Nota fiscal', email_svc.enviar_nf_emitida))

    ok, falhas = 0, []
    for nome_email, fn in envios:
        try:
            res = fn(p)
            if res and res.get('ok'):
                ok += 1
            else:
                falhas.append(f'{nome_email} ({(res or {}).get("erro", "?")})')
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('reenviar email %s pedido=%s',
                                         nome_email, codigo)
            falhas.append(f'{nome_email} (erro)')

    current_app.logger.info('reenvio emails pedido %s -> %s: %d ok, %d falha '
                            '(uid=%s)', codigo, destinatario, ok, len(falhas),
                            getattr(current_user, 'id', None))
    falhas_txt = ', '.join(falhas)
    if ok and not falhas:
        flash(f'{ok} e-mail(s) reenviado(s) pra {destinatario}.', 'success')
    elif ok:
        flash(f'{ok} reenviado(s); falhou: {falhas_txt}.', 'warning')
    else:
        flash(f'Não consegui reenviar: {falhas_txt or "verifique o Postmark"}.',
              'danger')
    return _detalhe_redirect(codigo)


@main_bp.route('/admin/loja-online/pedidos/<codigo>/imprimir.pdf')
@login_required
@gerente_required
def loja_online_pedido_imprimir(codigo):
    """PDF de impressão do pedido — MESMO layout do /entregas (via cliente +
    via motoboy). Reusa o serializador e o gerador de PDF de entregas pra o
    formato não divergir."""
    from app.blueprints.entregas.routes import (
        _aplicar_cartinhas,
        _serializar_pedido_online,
    )
    from app.models import PedidoOnline
    from app.services import pdf as pdf_svc
    from app.utils import hoje
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    d = _serializar_pedido_online(p)
    _aplicar_cartinhas([d])  # resolve a cartinha (manual sobrepõe, igual painel)
    data = p.data_entrega or hoje()
    conteudo = pdf_svc.gerar_pedidos_pdf([d], ['motorista', 'cliente'], data)
    resp = current_app.response_class(conteudo, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = (
        f'inline; filename="pedido_{p.codigo}.pdf"')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@main_bp.route('/admin/loja-online/pedidos/imprimir-selecao.pdf',
               methods=['POST'])
@login_required
@gerente_required
def loja_online_pedidos_imprimir_selecao():
    """PDF da SELEÇÃO da lista — N pedidos × 2 vias (cliente + motoboy).
    Reusa o gerador do `/entregas/`. Recebe `codigos` (multi-value do form).

    Data do cabeçalho do PDF: se a seleção é toda do mesmo dia, usa essa
    data; misturada (vários dias), usa a data MAIS PRÓXIMA — vai imprimir
    do mesmo lote no mesmo dia, na maioria das vezes."""
    from app.blueprints.entregas.routes import (
        _aplicar_cartinhas,
        _serializar_pedido_online,
    )
    from app.models import PedidoOnline
    from app.services import pdf as pdf_svc
    from app.utils import hoje
    codigos = [c.strip() for c in request.form.getlist('codigos') if c.strip()]
    if not codigos:
        from flask import flash
        flash('Selecione ao menos um pedido pra imprimir.', 'warning')
        return redirect(url_for('main.loja_online_pedidos'))
    # Mantém a ordem que veio do form (operador escolhe a ordem na tela).
    pedidos = (PedidoOnline.query
               .filter(PedidoOnline.codigo.in_(codigos)).all())
    por_codigo = {p.codigo: p for p in pedidos}
    selecionados = [por_codigo[c] for c in codigos if c in por_codigo]
    if not selecionados:
        from flask import flash
        flash('Nenhum dos pedidos selecionados foi encontrado.', 'warning')
        return redirect(url_for('main.loja_online_pedidos'))
    dicts = [_serializar_pedido_online(p) for p in selecionados]
    _aplicar_cartinhas(dicts)
    datas = sorted({p.data_entrega for p in selecionados if p.data_entrega})
    hj = hoje()
    if not datas:
        data = hj
    elif len(datas) == 1:
        data = datas[0]
    else:
        # Mistura: usa a mais próxima de hoje (impressão do lote do dia).
        data = min(datas, key=lambda d: abs((d - hj).days))
    conteudo = pdf_svc.gerar_pedidos_pdf(
        dicts, ['motorista', 'cliente'], data)
    resp = current_app.response_class(conteudo, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = (
        f'inline; filename="pedidos_selecao_{data.isoformat()}.pdf"')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _expedicao_com_pedido(p):
    """Sinal de que a EXPEDIÇÃO já está com o pedido — cancelar/reembolsar
    nesse estágio exige confirmação explícita (caso real 16/07/2026, pedido
    16CF21D8: reembolso com a entrega armada; a cliente recebeu o aviso de
    cancelamento e o time viu "cancelado × em entrega"). Sinais, do mais
    forte pro mais fraco: status operacional avançado, corrida Lalamove
    ativa, cozinha já marcou o card no Painel do Dia. Retorna a descrição
    do sinal ou None."""
    from app.models import LalamoveEntrega, PainelPedidoStatus
    if p.status in ('em_preparo', 'a_caminho'):
        return {'em_preparo': 'em preparo na cozinha',
                'a_caminho': 'EM ROTA'}[p.status]
    lal = (LalamoveEntrega.query
           .filter(LalamoveEntrega.pedido_code == p.codigo,
                   LalamoveEntrega.status.notin_(('cotacao', 'CANCELED')))
           .first())
    if lal:
        return 'motoboy Lalamove chamado (%s)' % lal.status
    pps = PainelPedidoStatus.query.filter_by(pedido_code=p.codigo).first()
    if pps and pps.status in ('visto', 'pronto'):
        return 'cozinha já marcou "%s" no Painel do Dia' % pps.status
    return None


@main_bp.route('/admin/loja-online/pedidos/<codigo>/cancelar', methods=['POST'])
@owner_required
def loja_online_pedido_cancelar(codigo):
    """Cancela/reembolsa um pedido do site.

    - Pago/em_preparo/a_caminho: dispara REEMBOLSO no Pagar.me
      (loja_pagamento.reembolsar_pedido). Antes de 17/07/2026 os status
      operacionais caíam no ramo "só marca cancelado" e o cliente ficava
      SEM reembolso — pego na investigação do caso 16CF21D8.
    - Expedição já com o pedido (em preparo/em rota/Lalamove/cozinha):
      exige `confirmar_expedicao=1` no POST — sem ele, recusa com aviso
      (decisão do dono 17/07/2026, opção "a"). O estoque só volta
      automaticamente se o status ainda era 'pago' (regra existente do
      _marcar_estornado — mercadoria que já saiu não re-entra sozinha).
    - Aguardando pagamento: só marca cancelado (nada foi cobrado/baixado).
    - Entregue/cancelado: bloqueia."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import loja_pagamento
    from app.utils import agora
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    if p.status == 'cancelado':
        flash(f'Pedido {p.codigo} já está cancelado.', 'warning')
    elif p.status == 'entregue':
        # Estorno de pedido ENTREGUE (dono 12/08/2026, caso 131B16EA):
        # permitido SÓ com o gesto explícito `confirmar_entregue=1` (botão
        # próprio com dupla confirmação). Reembolso total via Pagar.me;
        # estoque NÃO re-credita (estado_anterior != 'pago' no
        # _marcar_estornado — a mercadoria saiu de verdade). NF já emitida
        # se cancela no Tiny, manualmente.
        if request.form.get('confirmar_entregue') != '1':
            flash(f'Pedido {p.codigo} está ENTREGUE — use o botão '
                  f'"Estornar pedido entregue" (reembolso total, o estoque '
                  f'não volta sozinho).', 'warning')
        else:
            ok, msg = loja_pagamento.reembolsar_pedido(p)
            flash(f'{p.codigo}: {msg}', 'success' if ok else 'danger')
    elif p.status in ('pago', 'em_preparo', 'a_caminho'):
        sinal = _expedicao_com_pedido(p)
        if sinal and request.form.get('confirmar_expedicao') != '1':
            flash(f'⚠ NÃO cancelado: a expedição já está com o pedido '
                  f'{p.codigo} ({sinal}). Avise a expedição para SEGURAR a '
                  f'entrega e confirme de novo no botão — cancelar aqui não '
                  f'desarma a entrega sozinho.', 'warning')
            return _detalhe_redirect(codigo)
        ok, msg = loja_pagamento.reembolsar_pedido(p)
        flash(f'{p.codigo}: {msg}', 'success' if ok else 'danger')
    else:
        p.status = 'cancelado'
        p.motivo_cancelamento = 'cancelado_admin'
        p.cancelado_em = agora()
        db.session.commit()
        flash(f'Pedido {p.codigo} cancelado.', 'success')
    return _detalhe_redirect(codigo)


@main_bp.route('/admin/loja-online/pedidos/<codigo>/reduzir-item', methods=['POST'])
@owner_required
def loja_online_pedido_reduzir_item(codigo):
    """Reduz a quantidade de UM item de um pedido PAGO (cliente comprou 2 e era
    1). Owner-only: mexe em DINHEIRO (refund parcial) + estoque + plano. NF fica
    manual (Tiny só emite). Ver `loja_pagamento.reduzir_item_pedido_pago`."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import loja_pagamento
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    try:
        item_id = int(request.form.get('item_id') or 0)
        nova_qtd = int(request.form.get('nova_qtd') or 0)
    except (TypeError, ValueError):
        flash('Parâmetros inválidos.', 'warning')
        return _detalhe_redirect(codigo)
    ok, msg = loja_pagamento.reduzir_item_pedido_pago(
        p, item_id, nova_qtd, usuario_id=current_user.id)
    flash(f'{p.codigo}: {msg}', 'success' if ok else 'danger')
    return _detalhe_redirect(codigo)


# Transições válidas de status pra UI (admin). 'cancelado' tem rota própria
# (cancelar) porque envolve reembolso/estorno; aqui só os avanços manuais.
_STATUS_AVANCO = ('em_preparo', 'a_caminho', 'entregue')


@main_bp.route('/admin/loja-online/pedidos/<codigo>/status', methods=['POST'])
@login_required
@gerente_required
def loja_online_pedido_status(codigo):
    """Avança o status do pedido manualmente. Dispara e-mail transacional
    quando entra em `a_caminho`."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import email as email_svc
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    novo = (request.form.get('novo_status') or '').strip()
    if novo not in _STATUS_AVANCO:
        flash(f'Status inválido: {novo}', 'danger')
        return _detalhe_redirect(codigo)
    if p.status in ('cancelado', 'entregue') and novo != p.status:
        flash(f'Pedido {p.codigo} já está {p.status} — não muda.', 'warning')
        return _detalhe_redirect(codigo)
    transicionou_para_caminho = (novo == 'a_caminho' and p.status != 'a_caminho')
    transicionou_para_entregue = (novo == 'entregue' and p.status != 'entregue')
    p.status = novo
    db.session.commit()
    if transicionou_para_caminho:
        # E-mail "saiu pra entrega" — best-effort, não derruba o request.
        try:
            if email_svc.disponivel():
                email_svc.enviar_pedido_a_caminho(p)
        except Exception:  # noqa: BLE001
            current_app.logger.exception('email a_caminho falhou')
    if transicionou_para_entregue:
        # E-mail "pedido entregue" — best-effort.
        try:
            if email_svc.disponivel():
                email_svc.enviar_pedido_entregue(p)
        except Exception:  # noqa: BLE001
            current_app.logger.exception('email entregue falhou')
    flash(f'Pedido {p.codigo}: status atualizado para {novo}.', 'success')
    return _detalhe_redirect(codigo)


# ── Loja Online — Fase 5: mapeamento de SKU do Tiny (NF-e) ────────────────
# Liga cada item publicado no site ao SKU dele no Tiny. Pré-requisito da
# emissão de NF (o Tiny aplica o fiscal do cadastro do produto; nós só
# mandamos SKU + quantidade + valor).

@main_bp.route('/admin/loja-online/tiny-skus')
@owner_required
def loja_online_tiny_skus():
    from app.services import tiny_nf
    itens = tiny_nf.itens_para_mapear(canal='site')
    pendentes = sum(1 for i in itens if i['estado'] != 'mapeado')
    return render_template(
        'tiny_skus.html', itens=itens, pendentes=pendentes,
        total=len(itens),
        titulo='SKUs do Tiny (NF-e) — Site',
        descricao='A lista cobre os itens publicados no site (preço de '
                  'site). O B2B tem mapa próprio em B2B → SKUs do Tiny — '
                  'no Tiny é outro cadastro/lista de preço.',
        url_definir=url_for('main.loja_online_tiny_definir'),
        url_sync=url_for('main.loja_online_tiny_sync'),
        url_importar=url_for('main.loja_online_tiny_importar'),
        vazio_msg='Nenhum item publicado no site ainda (precisa ter preço '
                  'de site).')


@main_bp.route('/admin/loja-online/tiny-skus/sync', methods=['POST'])
@owner_required
def loja_online_tiny_sync():
    """Busca o catálogo do Tiny e sugere SKUs por nome pros não mapeados."""
    from flask import flash

    from app.services import tiny_nf
    res = tiny_nf.sincronizar_sugestoes(user_id=current_user.id)
    if res.get('erro'):
        flash(f'Sincronização falhou: {res["erro"]}', 'danger')
    else:
        flash(f'{res.get("exatos", 0)} confirmados (nome idêntico) + '
              f'{res.get("sugeridos", 0)} sugeridos pra conferir, '
              f'{res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total_tiny", 0)} produtos no Tiny).', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


@main_bp.route('/admin/loja-online/tiny-skus/importar', methods=['POST'])
@owner_required
def loja_online_tiny_importar():
    """Importa o export de produtos do Tiny (.xls/.csv) e mapeia SKUs por
    nome. Nome idêntico confirma automático; parecido vira sugestão."""
    from flask import flash

    from app.services import tiny_nf
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione a planilha de produtos do Tiny (.xls ou .csv).',
              'warning')
        return redirect(url_for('main.loja_online_tiny_skus'))
    conteudo = f.read()
    res = tiny_nf.importar_planilha(conteudo, f.filename,
                                    user_id=current_user.id)
    if res.get('erro'):
        flash(res['erro'], 'danger')
    else:
        flash(f'Planilha importada: {res.get("exatos", 0)} confirmados '
              f'(nome idêntico) + {res.get("sugeridos", 0)} sugeridos pra '
              f'conferir, {res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total", 0)} linhas).', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


@main_bp.route('/admin/loja-online/pedidos/<codigo>/emitir-nf', methods=['POST'])
@owner_required
def loja_online_emitir_nf(codigo):
    """Botão manual de emissão de NF via Tiny (Fase 5 plano A).

    `recriar=1`: descarta a NF rascunho anterior (que a SEFAZ rejeitou) e
    refaz o pedido+nota do zero no Tiny com o payload atual."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import tiny_nf
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    recriar = request.form.get('recriar') in ('1', 'true', 'on')
    res = tiny_nf.emitir_nf(p, user_id=current_user.id, recriar=recriar)
    flash(f'{p.codigo}: {res["msg"]}', 'success' if res.get('ok') else 'danger')
    return _detalhe_redirect(codigo)


@main_bp.route('/admin/loja-online/pedidos/<codigo>/danfe')
@login_required
@gerente_required
def loja_online_danfe(codigo):
    """Redireciona pro DANFE (PDF) da NF no Tiny. Link temporário — busca sob
    demanda (não a cada abertura do pedido)."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import tiny_nf
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    url = tiny_nf.link_danfe(p)
    if not url:
        flash(f'{p.codigo}: não consegui obter o link do DANFE no Tiny '
              '(a NF precisa estar autorizada).', 'warning')
        return _detalhe_redirect(codigo)
    return redirect(url)


@main_bp.route('/admin/loja-online/tiny-skus/definir', methods=['POST'])
@owner_required
def loja_online_tiny_definir():
    """Define/limpa o SKU de um item (kind + item_id + sku)."""
    from flask import flash

    from app.services import tiny_nf
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('main.loja_online_tiny_skus'))
    sku = (request.form.get('sku') or '').strip()
    tiny_nf.definir_sku(kind, item_id, sku, user_id=current_user.id)
    flash('SKU salvo.' if sku else 'SKU removido.', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


# ── Loja Online: Produção do dia (o que PREPARAR, vindo das vendas) ──
# Decisao do dono 23/06/2026: a tela "o que preparar para o dia X de acordo
# com o que foi vendido pra entregar pelo site" tem que viver aqui no
# /admin/loja-online (antes so existia escondida em /pedidos/contagem-dia-site).
# Reusa o servico contagem_para_dia (cestas desempacotadas) e o mesmo template,
# so trocando o link de voltar. NAO confundir com a "Disponibilidade do dia"
# (abaixo), que define quanto PODE VENDER.

@main_bp.route('/admin/loja-online/producao-do-dia')
@owner_required
def loja_online_producao_dia():
    """O que precisa sair de producao pra os pedidos do site de um dia X
    (cestas desempacotadas). Mesma logica do /pedidos/contagem-dia-site,
    surfaceada no painel da Loja Online."""
    from datetime import date as _date

    from app.services import loja_online_vendas
    from app.utils import hoje
    data_str = (request.args.get('data') or '').strip()
    try:
        alvo = _date.fromisoformat(data_str) if data_str else hoje()
    except ValueError:
        alvo = hoje()
    itens = loja_online_vendas.contagem_para_dia(alvo)
    return render_template(
        'pedidos/contagem_dia_site.html',
        itens=itens, data=alvo, data_str=alvo.isoformat(),
        titulo='Produção do dia',
        voltar_url=url_for('main.loja_online_dashboard'),
        voltar_label='Loja Online')


# ── Loja Online: Disponibilidade por dia (22/06/2026, decisao do dono) ──
# Permite "hoje 0 foccacia, sexta 20" sem mexer no estoque fisico. Controla
# o "Esgotado" no site. Reserva acontece no webhook pagar.me; devolve no
# cancelamento. Owner-only. NAO confundir com "Producao do dia" (acima),
# que mostra o que PREPARAR; esta aqui define quanto PODE VENDER.

@main_bp.route('/admin/loja-online/plano-do-dia')
@owner_required
def loja_online_plano_dia():
    """Mostra a lista de itens publicados no site com o plano (qtd disponivel)
    pra a data selecionada. Edicao inline via POST AJAX."""
    from datetime import date as _date

    from app.services import loja_catalogo, loja_plano_dia
    from app.utils import hoje
    data_str = (request.args.get('data') or '').strip()
    try:
        alvo = _date.fromisoformat(data_str) if data_str else hoje()
    except ValueError:
        alvo = hoje()

    # Itens publicados (Receitas + Produtos com preco_site > 0).
    itens_publicados = loja_catalogo.produtos_publicados()

    # Plano atual pra essa data — map de (kind, id) -> row.
    from app.models import EstoqueSitePlano
    rows = {(r.kind, r.item_id): r
            for r in EstoqueSitePlano.query.filter_by(data=alvo).all()}

    # Plano da semana anterior (mesmo dia da semana, -7 dias) — fonte do
    # botao "Copiar da semana passada".
    from datetime import timedelta
    semana_anterior = alvo - timedelta(days=7)
    plano_anterior = {(r.kind, r.item_id): r.qtd_planejada
                      for r in EstoqueSitePlano.query
                      .filter_by(data=semana_anterior).all()}

    itens = []
    for it in itens_publicados:
        row = rows.get((it['kind'], it['id']))
        itens.append({
            'kind': it['kind'], 'id': it['id'],
            'nome': it['nome'], 'categoria': it['categoria'],
            'qtd_planejada': row.qtd_planejada if row else None,
            'qtd_reservada': row.qtd_reservada if row else 0,
            'saldo': loja_plano_dia.saldo(it['kind'], it['id'], alvo),
            'plano_anterior': plano_anterior.get((it['kind'], it['id'])),
        })

    return render_template('admin/loja_online_plano_dia.html',
                           itens=itens, data=alvo,
                           data_str=alvo.isoformat(),
                           data_anterior=semana_anterior.isoformat(),
                           tem_plano_anterior=bool(plano_anterior))


@main_bp.route('/admin/loja-online/plano-do-dia/definir', methods=['POST'])
@owner_required
def loja_online_plano_dia_definir():
    """Salva qtd_planejada de UM item pra UMA data. AJAX/JSON."""
    from datetime import date as _date

    from app.services import loja_plano_dia
    kind = (request.form.get('kind') or '').strip()
    if kind not in ('receita', 'produto'):
        return jsonify(ok=False, erro='kind invalido'), 400
    try:
        item_id = int(request.form.get('item_id'))
        qtd = int(request.form.get('qtd_planejada'))
        data = _date.fromisoformat(request.form.get('data'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros invalidos'), 400
    if qtd < 0:
        return jsonify(ok=False, erro='qtd nao pode ser negativa'), 400
    try:
        loja_plano_dia.definir(kind, item_id, data, qtd)
    except ValueError as e:
        return jsonify(ok=False, erro=str(e)), 400
    saldo = loja_plano_dia.saldo(kind, item_id, data)
    return jsonify(ok=True, saldo=saldo, qtd_planejada=qtd)


@main_bp.route('/admin/loja-online/plano-do-dia/replicar-tudo', methods=['POST'])
@owner_required
def loja_online_plano_dia_replicar_tudo():
    """Replica `qtd_planejada` pros proximos 14 dias pra TODOS os itens
    publicados no site, a partir de `data_inicio`. Sobrescreve.

    Decisao do dono 24/06/2026: evitar o caso "esqueci de clicar ↔ num
    item e ele zerou no site"."""
    from datetime import date as _date

    from app.services import loja_catalogo, loja_plano_dia
    try:
        qtd = int(request.form.get('qtd_planejada'))
        data_inicio = _date.fromisoformat(request.form.get('data_inicio'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros invalidos'), 400
    if qtd < 0:
        return jsonify(ok=False, erro='qtd nao pode ser negativa'), 400
    n_itens = 0
    n_dias_total = 0
    for it in loja_catalogo.produtos_publicados():
        loja_plano_dia.replicar_para_proximos_dias(
            it['kind'], it['id'], qtd,
            data_inicio=data_inicio, dias=14)
        n_itens += 1
        n_dias_total += 14
    return jsonify(ok=True, itens=n_itens, dias_total=n_dias_total)


@main_bp.route('/admin/loja-online/plano-do-dia/reparar-orfas', methods=['POST'])
@owner_required
def loja_online_plano_dia_reparar_orfas():
    """Conserta linhas (planejada=0, reservada>0) criadas pelo bug
    pre-24/06/2026 do `reservar`. Sobe planejada pra DEFAULT + reservada,
    devolvendo saldo positivo. Idempotente."""
    from app.services import loja_plano_dia
    corrigidas = loja_plano_dia.reparar_linhas_orfas()
    return jsonify(ok=True, corrigidas=len(corrigidas),
                   detalhes=corrigidas[:50])


@main_bp.route('/admin/loja-online/plano-do-dia/replicar', methods=['POST'])
@owner_required
def loja_online_plano_dia_replicar():
    """Replica `qtd_planejada` pros proximos N dias a partir de `data_inicio`
    pra UM item. SOBRESCREVE — vai com o que o usuario digitou."""
    from datetime import date as _date

    from app.services import loja_plano_dia
    kind = (request.form.get('kind') or '').strip()
    if kind not in ('receita', 'produto'):
        return jsonify(ok=False, erro='kind invalido'), 400
    try:
        item_id = int(request.form.get('item_id'))
        qtd = int(request.form.get('qtd_planejada'))
        data_inicio = _date.fromisoformat(request.form.get('data_inicio'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros invalidos'), 400
    if qtd < 0:
        return jsonify(ok=False, erro='qtd nao pode ser negativa'), 400
    try:
        n = loja_plano_dia.replicar_para_proximos_dias(
            kind, item_id, qtd, data_inicio=data_inicio, dias=14)
    except ValueError as e:
        return jsonify(ok=False, erro=str(e)), 400
    return jsonify(ok=True, dias=n)


@main_bp.route('/admin/loja-online/plano-do-dia/copiar-semana-passada',
               methods=['POST'])
@owner_required
def loja_online_plano_dia_copiar():
    """Copia o plano da MESMA data 7 dias atras pra a data informada.
    NAO sobrescreve se ja houver linha (idempotente — clicar 2x nao
    duplica nem zera valor digitado pelo dono)."""
    from datetime import date as _date
    from datetime import timedelta

    from app.models import EstoqueSitePlano
    from app.services import loja_plano_dia
    try:
        data = _date.fromisoformat(request.form.get('data'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='data invalida'), 400
    origem = data - timedelta(days=7)
    rows_origem = EstoqueSitePlano.query.filter_by(data=origem).all()
    existentes = {(r.kind, r.item_id)
                  for r in EstoqueSitePlano.query.filter_by(data=data).all()}
    copiados = 0
    for r in rows_origem:
        if (r.kind, r.item_id) in existentes:
            continue
        loja_plano_dia.definir(r.kind, r.item_id, data, r.qtd_planejada)
        copiados += 1
    return jsonify(ok=True, copiados=copiados, origem=origem.isoformat())


# ── Horarios especiais do site (27/07/2026) ──────────────────────────────
#
# Pedido do dono: Dia dos Pais (09/08/2026) so pode ter UMA janela de
# entrega, 06:00-10:00. Escolha dele: TELA pra ele mesmo cadastrar (Natal,
# Dia das Maes) em vez de a data ficar cravada no codigo.

@main_bp.route('/admin/loja-online/horarios-especiais')
@owner_required
def loja_horarios_especiais():
    """Lista as datas com horario diferente do normal + formulario."""
    from datetime import timedelta as _td

    from app.services import loja_checkout, loja_data_especial
    regras = loja_data_especial.listar(desde=hoje_brt() - _td(days=30))
    return render_template(
        'admin/loja_horarios_especiais.html',
        regras=regras,
        # Pedidos JÁ PAGOS pra essas datas com horário fora do novo (a agenda
        # do site é de 14 dias, então dá pra ter venda anterior ao cadastro).
        # Sem isto ninguém descobre até o dia, no painel de entregas.
        fora_do_horario=loja_data_especial.pedidos_fora_do_horario(regras),
        hoje_d=hoje_brt(),
        hoje_iso=hoje_brt().isoformat(),
        hora_abre=loja_checkout.HORA_ABRE,
        hora_fecha=loja_checkout.HORA_FECHA,
    )


@main_bp.route('/admin/loja-online/horarios-especiais/salvar',
               methods=['POST'])
@owner_required
def loja_horarios_especiais_salvar():
    """Cria/atualiza a regra de uma data.

    Horario torto NAO grava nada (`JanelaInvalida`): cadastro pela metade em
    horario de entrega e pior que recusar — o dono corrige e reenvia."""
    from datetime import date as _date

    from app.services import loja_data_especial
    destino = url_for('main.loja_horarios_especiais')
    try:
        data = _date.fromisoformat((request.form.get('data') or '').strip())
    except ValueError:
        flash('Escolha uma data válida.', 'danger')
        return redirect(destino)
    # FECHAR O DIA é um checkbox EXPLÍCITO, não "campo em branco".
    # Achado de revisão 27/07/2026: o formulário nasce vazio e `definir` é
    # upsert, então o dono que reabrisse a tela só pra corrigir o rótulo do
    # Dia dos Pais salvaria com o textarea em branco e FECHARIA o site no dia
    # — a pior falha possível aqui, sem nenhuma confirmação.
    fechar = bool(request.form.get('fechar_dia'))
    janelas = request.form.get('janelas') or ''
    if not fechar and not janelas.strip():
        flash('Informe pelo menos um horário (ex.: 06:00-10:00) — ou marque '
              '"fechar o dia" se a intenção é não vender para essa data.',
              'danger')
        return redirect(destino)
    try:
        loja_data_especial.definir(
            data,
            '' if fechar else janelas,
            express_bloqueado=bool(request.form.get('express_bloqueado')),
            rotulo=request.form.get('rotulo'),
            usuario_id=current_user.id,
            # Passa o valor CRU do form: None quando o campo está AUSENTE
            # (POST antigo/forjado — preserva o gravado) e '' quando veio
            # vazio da tela (limpa de propósito; o Editar pré-carrega o
            # valor atual, então '' na tela é gesto real do dono). Achado
            # do revisor 07/08: `or ''` fazia POST sem o campo limpar tudo.
            bloquear_itens=request.form.get('bloquear_itens'))
    except loja_data_especial.JanelaInvalida as e:
        flash(str(e), 'danger')
        return redirect(destino)
    flash(f'Horário de {data.strftime("%d/%m/%Y")} salvo.', 'success')
    return redirect(destino)


@main_bp.route('/admin/loja-online/horarios-especiais/remover',
               methods=['POST'])
@owner_required
def loja_horarios_especiais_remover():
    """Apaga a regra — o dia volta ao horario normal (08:00-18:00)."""
    from datetime import date as _date

    from app.services import loja_data_especial
    destino = url_for('main.loja_horarios_especiais')
    try:
        data = _date.fromisoformat((request.form.get('data') or '').strip())
    except ValueError:
        flash('Data inválida.', 'danger')
        return redirect(destino)
    if loja_data_especial.remover(data):
        flash(f'{data.strftime("%d/%m/%Y")} voltou ao horário normal.',
              'success')
    return redirect(destino)


# ── Debug VNDA: o que campo a Loja usa pra marcar RETIRADA? (16/06/2026) ──
#
# Bug do dono: "pedidos de retirada nao aparecem em lugar nenhum". Causa
# provavel: `_normalizar_pedido` (vnda.py:344) nao tem deteccao de retirada,
# entao pedidos de pickup chegam misturados como entrega normal mas sem
# endereco — e o painel filtra silenciosamente.
#
# Pra eu fazer o fix sem chutar o nome do shipping_method, esta rota expoe
# o JSON BRUTO de um pedido especifico (owner abre, encontra um pedido de
# retirada que ele conhece, me passa o que aparece em shipping_method_code/
# shipping_method/shipping_label). Owner-only, read-only, nao muta nada.

@main_bp.route('/admin/debug-vnda-pedido/<code>')
@owner_required
def debug_vnda_pedido(code):
    """Mostra o JSON cru de um pedido VNDA + os campos de shipping_method
    em destaque. Owner-only, read-only."""
    from app.services import vnda
    pedido = vnda.buscar_pedido_completo(code)
    if not pedido:
        return jsonify(ok=False, erro=f'pedido {code!r} nao encontrado no VNDA'), 404
    shipping_keys = [
        'shipping_method', 'shipping_method_code', 'shipping_method_name',
        'shipping_name', 'shipping_label', 'delivery_type',
    ]
    destaque = {k: pedido.get(k) for k in shipping_keys}
    extra = pedido.get('extra') or {}
    return jsonify(
        ok=True, code=code,
        shipping_destaque=destaque,
        extra=extra,
        pedido_completo=pedido,
    )


# ── Debug email (Postmark): testa envio sem expor o token (17/06/2026) ────
@main_bp.route('/admin/debug-email', methods=['GET', 'POST'])
@owner_required
def debug_email():
    """Diagnóstico do Postmark (owner-only). GET mostra status da config;
    GET com ?para=<email>&enviar=1 (ou POST) manda um email de teste. NAO
    expoe o server token."""
    from app.services import email as email_svc
    cfg = current_app.config
    status = {
        'postmark_configurado': email_svc.disponivel(),
        'remetente': cfg.get('EMAIL_REMETENTE'),
        'remetente_nome': cfg.get('EMAIL_REMETENTE_NOME'),
        'app_base_url': cfg.get('APP_BASE_URL'),
        'token_len': len((cfg.get('POSTMARK_SERVER_TOKEN') or '')),
    }
    # GET com ?para=<email>&enviar=1 dispara o envio (mais fácil de testar
    # do navegador). POST com ?para=<email> mantém compat programático.
    para = (request.args.get('para') or request.form.get('para') or '').strip()
    deve_enviar = request.method == 'POST' or request.args.get('enviar') == '1'
    if deve_enviar:
        if not para:
            return jsonify(ok=False, erro='passe ?para=<email>&enviar=1',
                           status=status), 400
        res = email_svc.enviar(
            para, 'Teste de email — O Pão',
            '<p>Funcionou! Este é um email de teste do sistema da padaria.</p>',
            texto='Funcionou! Email de teste do sistema da padaria.')
        return jsonify(ok=res.get('ok'), resultado=res, status=status)
    return jsonify(
        ok=True, status=status,
        dica='Abra /admin/debug-email?para=seu@email.com&enviar=1 pra testar')


# ── SEO: descricoes geradas com IA (22/06/2026) ───────────────────────
# Lista produtos publicados com `descricao_seo` vazia, gera sugestao com
# Claude Haiku, dono revisa e salva. Controle total — nunca publica
# automaticamente. Service: app/services/seo_descricoes.py.

@main_bp.route('/admin/seo/descricoes')
@owner_required
def seo_descricoes():
    from app.services import seo_descricoes as svc
    receitas = (Receita.query
                .filter(Receita.preco_site.isnot(None),
                        Receita.preco_site > 0,
                        Receita.arquivada_em.is_(None))
                .order_by(Receita.descricao_seo.is_(None).desc(),
                          Receita.categoria, Receita.nome)
                .all())
    produtos = (Produto.query
                .filter(Produto.preco_site.isnot(None),
                        Produto.preco_site > 0,
                        Produto.ativo.is_(True))
                .order_by(Produto.descricao_seo.is_(None).desc(),
                          Produto.nome)
                .all())
    n_vazias_r = sum(1 for r in receitas if not r.descricao_seo)
    n_vazias_p = sum(1 for p in produtos if not p.descricao_seo)
    return render_template(
        'admin/seo_descricoes.html',
        receitas=receitas, produtos=produtos,
        n_vazias_r=n_vazias_r, n_vazias_p=n_vazias_p,
        api_disponivel=svc.disponivel(),
    )


@main_bp.route('/admin/seo/descricoes/sugerir', methods=['POST'])
@owner_required
def seo_descricoes_sugerir():
    """API: dado {kind:'receita'|'produto', id:int}, gera uma sugestao com
    Claude e devolve {ok, sugestao} ou {ok:False, erro}. NAO salva no banco
    — quem salva eh o /salvar abaixo (apos revisao humana)."""
    from app.services import seo_descricoes as svc
    kind = (request.form.get('kind') or '').strip()
    try:
        id_ = int(request.form.get('id') or 0)
    except ValueError:
        return jsonify(ok=False, erro='id invalido')
    if kind == 'receita':
        obj = Receita.query.get(id_)
        if not obj:
            return jsonify(ok=False, erro='receita nao encontrada')
        texto = svc.sugerir_para_receita(obj)
    elif kind == 'produto':
        obj = Produto.query.get(id_)
        if not obj:
            return jsonify(ok=False, erro='produto nao encontrado')
        texto = svc.sugerir_para_produto(obj)
    else:
        return jsonify(ok=False, erro='kind invalido')
    if not texto:
        return jsonify(ok=False,
                       erro='IA indisponivel (cheque ANTHROPIC_API_KEY)')
    return jsonify(ok=True, sugestao=texto)


@main_bp.route('/admin/seo/descricoes/salvar', methods=['POST'])
@owner_required
def seo_descricoes_salvar():
    """Persiste descricao_seo revisada pelo dono. Aceita string vazia pra
    LIMPAR (volta ao fallback automatico)."""
    kind = (request.form.get('kind') or '').strip()
    try:
        id_ = int(request.form.get('id') or 0)
    except ValueError:
        return jsonify(ok=False, erro='id invalido')
    texto = (request.form.get('descricao') or '').strip()
    # Limite defensivo (DB eh TEXT, sem limite, mas SEO description ideal
    # eh ate ~300 chars).
    if len(texto) > 500:
        texto = texto[:500]
    if kind == 'receita':
        obj = Receita.query.get(id_)
    elif kind == 'produto':
        obj = Produto.query.get(id_)
    else:
        return jsonify(ok=False, erro='kind invalido')
    if not obj:
        return jsonify(ok=False, erro='nao encontrado')
    obj.descricao_seo = texto or None
    db.session.commit()
    return jsonify(ok=True, salvo=bool(texto), len=len(texto))


# ── Spotify (música da tela do padeiro) — conexão da conta (15/07/2026) ─────

@main_bp.route('/admin/spotify')
@admin_required
def spotify_admin():
    """Status da integração Spotify + instruções de setup + botão Conectar.
    A música é controlada pela tela do padeiro (/padeiro, widget 🎵); aqui o
    administrador conecta a conta do Spotify da padaria UMA vez."""
    from app.services import spotify
    return render_template(
        'admin/spotify.html',
        configurado=spotify.configurado(),
        conectado=spotify.conectado(),
        conta=spotify.conta_display(),
        redirect_uri=(spotify.redirect_uri() if spotify.configurado()
                      else url_for('main.spotify_callback', _external=True)))


@main_bp.route('/admin/spotify/conectar')
@admin_required
def spotify_conectar():
    """Manda o admin pro consentimento do Spotify (state anti-CSRF na
    session, conferido no callback)."""
    import secrets

    from flask import session

    from app.services import spotify
    if not spotify.configurado():
        flash('Configure SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET no '
              'Railway antes de conectar.', 'warning')
        return redirect(url_for('main.spotify_admin'))
    state = secrets.token_urlsafe(24)
    session['spotify_oauth_state'] = state
    return redirect(spotify.url_autorizacao(state))


@main_bp.route('/admin/spotify/callback')
@admin_required
def spotify_callback():
    """Volta do consentimento: valida o state e troca o código por tokens."""
    from flask import session

    from app.services import spotify
    state = request.args.get('state') or ''
    esperado = session.pop('spotify_oauth_state', None)
    if not esperado or state != esperado:
        flash('Retorno do Spotify inválido (state não confere) — tente '
              'conectar de novo.', 'danger')
        return redirect(url_for('main.spotify_admin'))
    if request.args.get('error'):
        flash(f'O Spotify recusou a autorização: {request.args["error"]}',
              'danger')
        return redirect(url_for('main.spotify_admin'))
    code = request.args.get('code') or ''
    ok, erro = spotify.trocar_codigo(code)
    if ok:
        flash('Spotify conectado! O widget 🎵 da tela do padeiro já '
              'funciona.', 'success')
    else:
        flash(f'Falha ao conectar o Spotify: {erro}', 'danger')
    return redirect(url_for('main.spotify_admin'))


@main_bp.route('/admin/spotify/desconectar', methods=['POST'])
@admin_required
def spotify_desconectar():
    """Apaga os tokens salvos (o app segue cadastrado no Spotify)."""
    from app.services import spotify
    spotify.desconectar()
    flash('Spotify desconectado.', 'info')
    return redirect(url_for('main.spotify_admin'))
