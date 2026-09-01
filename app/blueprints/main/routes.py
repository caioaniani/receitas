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
    from app.models import CatalogoFoto
    if not current_user.is_admin():
        abort(403)
    foto = CatalogoFoto.query.get_or_404(foto_id)
    _obj, url_back = _alvo_catalogo(foto.kind, foto.item_id)
    if foto.storage_path:
        from app.services import dropbox_storage
        dropbox_storage.deletar(foto.storage_path)   # best-effort
    _db.session.delete(foto)
    _db.session.commit()
    flash('Foto removida da galeria.', 'info')
    return redirect(url_back)


@main_bp.route('/api/exportar')
@login_required
def exportar():
    mps = MateriaPrima.query.order_by(MateriaPrima.id).all()
    receitas = Receita.query.order_by(Receita.id).all()
    produtos = Produto.query.order_by(Produto.id).all()

    data = {
        'materias_primas': [mp.to_dict() for mp in mps],
        'receitas': [r.to_dict() for r in receitas],
        'produtos': [p.to_dict() for p in produtos],
    }

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        json_str,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=padaria_backup.json'}
    )


@main_bp.route('/api/importar', methods=['POST'])
@login_required
@admin_required
def importar():
    file = request.files.get('file')
    if not file:
        return jsonify(success=False, error='Nenhum arquivo enviado')

    try:
        data = json.loads(file.read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify(success=False, error='Arquivo JSON inválido')

    # Validar estrutura antes de apagar qualquer coisa
    if 'materias_primas' not in data or 'receitas' not in data:
        return jsonify(success=False, error='Arquivo nao tem a estrutura esperada')

    try:
        # Limpa tudo
        ProdutoItem.query.delete()
        ReceitaIngrediente.query.delete()
        Receita.query.delete()
        MateriaPrima.query.delete()
        Produto.query.delete()

        # Recria matérias-primas
        for mp_data in data.get('materias_primas', []):
            mp = MateriaPrima(
                nome=mp_data['nome'],
                unidade=mp_data.get('unidade', 'g'),
                custo_por_kg=mp_data['custo_por_kg'],
                fornecedor=mp_data.get('fornecedor') or None,
                observacoes=mp_data.get('observacoes') or None,
            )
            db.session.add(mp)

        db.session.flush()

        # Recria receitas
        for r_data in data.get('receitas', []):
            receita = Receita(
                nome=r_data['nome'],
                categoria=r_data.get('categoria') or None,
                preco_venda=r_data.get('preco_venda'),
                preco_loja=r_data.get('preco_loja'),
                preco_site=r_data.get('preco_site'),
                rendimento_qtd=r_data['rendimento_qtd'],
                rendimento_unidade=r_data['rendimento_unidade'],
                peso_base=r_data['peso_base'],
                peso_unitario=r_data.get('peso_unitario'),
                perda_percentual=r_data.get('perda_percentual', 0),
                custo_embalagem=r_data.get('custo_embalagem', 0),
                modo_preparo=r_data.get('modo_preparo') or None,
                observacao=r_data.get('observacao') or None,
            )
            db.session.add(receita)
            db.session.flush()

            for ing_data in r_data.get('ingredientes', []):
                ing = ReceitaIngrediente(
                    receita_id=receita.id,
                    tipo=ing_data.get('tipo', 'mp'),
                    ingrediente_nome=ing_data['ingrediente_nome'],
                    porcentagem=ing_data['porcentagem'],
                    eh_base=ing_data.get('eh_base', False),
                    nota=ing_data.get('nota') or None,
                )
                db.session.add(ing)

        # Recria produtos (cestas, kits, etc.)
        for p_data in data.get('produtos', []):
            produto = Produto(
                nome=p_data['nome'],
                categoria=p_data.get('categoria') or None,
                descricao=p_data.get('descricao') or None,
                preco_atacado=p_data.get('preco_atacado'),
                preco_loja=p_data.get('preco_loja'),
                preco_site=p_data.get('preco_site'),
                custo_direto=p_data.get('custo_direto'),
                custo_embalagem=p_data.get('custo_embalagem', 0),
                modo_preparo=p_data.get('modo_preparo') or None,
                observacao=p_data.get('observacao') or None,
                ativo=p_data.get('ativo', True),
            )
            db.session.add(produto)
            db.session.flush()

            for item_data in p_data.get('itens', []):
                # Resolve FK por nome — item orfao (sem match) entra com
                # FK NULL e admin resolve em /cestas/orfaos.
                tipo_item = item_data['tipo']
                nome_item = item_data['item_nome']
                receita_id = None
                materia_prima_id = None
                if tipo_item == 'receita':
                    r = Receita.query.filter_by(nome=nome_item).first()
                    receita_id = r.id if r else None
                elif tipo_item == 'mp':
                    m = MateriaPrima.query.filter_by(nome=nome_item).first()
                    materia_prima_id = m.id if m else None
                item = ProdutoItem(
                    produto_id=produto.id,
                    tipo=tipo_item,
                    item_nome=nome_item,
                    receita_id=receita_id,
                    materia_prima_id=materia_prima_id,
                    quantidade=item_data['quantidade'],
                )
                db.session.add(item)

        db.session.commit()
        return jsonify(success=True)

    except Exception:
        db.session.rollback()
        return jsonify(success=False, error='Erro ao importar dados. Verifique o formato do arquivo.')


@main_bp.route('/todo')
@login_required
@admin_required
def todo():
    receitas = Receita.ativas().filter(
        Receita.observacao.isnot(None), Receita.observacao != ''
    ).order_by(Receita.nome).all()
    produtos = Produto.query.filter(
        Produto.ativo.is_(True),
        Produto.observacao.isnot(None), Produto.observacao != ''
    ).order_by(Produto.nome).all()
    return render_template('main/todo.html', receitas=receitas, produtos=produtos)


@main_bp.route("/audit")
@login_required
@admin_required
def audit():
    """Visualizador do audit log. So admin pode ver."""
    import json as _json
    tabela_f = request.args.get("tabela") or None
    usuario_f = request.args.get("usuario_id", type=int)
    acao_f = request.args.get("acao") or None
    registro_f = request.args.get("registro_id", type=int)
    q = AuditLog.query
    if tabela_f:
        q = q.filter_by(tabela=tabela_f)
    if usuario_f:
        q = q.filter_by(usuario_id=usuario_f)
    if acao_f in ("insert", "update", "delete"):
        q = q.filter_by(acao=acao_f)
    if registro_f:
        q = q.filter_by(registro_id=registro_f)
    logs = q.order_by(AuditLog.criado_em.desc()).limit(200).all()

    # "Historico completo" de um PEDIDO (tabela=pedido_loja + registro_id):
    # as mudancas de ITENS moram em linhas pedido_item com registro_id = id
    # do ITEM — sem isto, editar itens aparecia so como "(sem mudancas
    # detectadas)" no pedido (pedido do dono 03/07/2026). O pedido_id vive
    # no JSON do snapshot, entao filtra em Python sobre uma janela recente.
    if registro_f and tabela_f == 'pedido_loja':
        candidatos = (AuditLog.query.filter_by(tabela='pedido_item')
                      .order_by(AuditLog.criado_em.desc())
                      .limit(1000).all())

        def _do_pedido(log_item):
            for bruto in (log_item.depois, log_item.antes):
                if not bruto:
                    continue
                try:
                    if _json.loads(bruto).get('pedido_id') == registro_f:
                        return True
                except (ValueError, TypeError):
                    continue
            return False

        extras = [l for l in candidatos if _do_pedido(l)]
        if extras:
            logs = sorted(logs + extras, key=lambda x: x.criado_em,
                          reverse=True)[:200]
    # Parse JSON dos campos antes/depois + tradução em linguagem natural.
    from app.services import historico_humano
    rows = []
    for l in logs:
        try:
            antes = _json.loads(l.antes) if l.antes else None
        except Exception:
            antes = None
        try:
            depois = _json.loads(l.depois) if l.depois else None
        except Exception:
            depois = None
        traducao = historico_humano.traduzir_audit(l, antes, depois)
        rows.append({
            "log": l, "antes": antes, "depois": depois, "traducao": traducao,
        })
    # Lista de tabelas e usuarios pra filtros
    tabelas = [r[0] for r in db.session.query(AuditLog.tabela).distinct().all()]
    usuarios = Usuario.query.order_by(Usuario.nome).all()
    # Cartinhas atualizadas nas ultimas 48h — pra rastrear pedidos com
    # cartinha cadastrada manualmente (relatorio do auditor "cliente pediu
    # cartinha em pedido ja feito" usa a conversa do Chatwoot; aqui voce
    # ve o que o atendente efetivamente CADASTROU no banco).
    from app.models import CartinhaEntrega
    cartinhas = (CartinhaEntrega.query
                 .filter(CartinhaEntrega.atualizado_em >= agora() - timedelta(hours=48))
                 .order_by(CartinhaEntrega.atualizado_em.desc())
                 .limit(50).all())
    return render_template("main/audit.html", rows=rows, tabelas=sorted(tabelas),
                           usuarios=usuarios, cartinhas=cartinhas,
                           filtros={"tabela": tabela_f,
                           "usuario_id": usuario_f, "acao": acao_f,
                           "registro_id": registro_f})



@main_bp.route('/caixa')
@login_required
@admin_required
def caixa():
    """Dashboard de caixa diario: agrega dados LOCAIS do banco.
    Vendas PDV (Seru) NAO entram aqui pra evitar chamadas externas
    lentas — use /pdv pra esse detalhe."""
    from sqlalchemy import func as sqlfunc

    from app.constants import STATUS_PEDIDO_ENTREGUES

    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        data_alvo = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        data_alvo = hoje_brt()

    ontem = data_alvo - timedelta(days=1)
    semana_atras = data_alvo - timedelta(days=7)

    def metricas_do_dia(d):
        """Sumario de um dia: pedidos locais, pedidos loja, entregas,
        movimentacoes de estoque."""
        # Pedidos locais (entregas avulsas) — tem valor_total
        locais = PedidoLocal.query.filter(PedidoLocal.data_entrega == d).all()
        valor_locais = sum(p.valor_total for p in locais)

        # Pedidos entre lojas — quantidade
        pedidos_loja = PedidoLoja.query.filter(PedidoLoja.data_entrega == d).all()
        n_ped_loja = len(pedidos_loja)
        n_ped_loja_entregue = sum(
            1 for p in pedidos_loja if p.status in STATUS_PEDIDO_ENTREGUES)

        # Entregas atribuidas — quantidade + entregues
        atribs = AtribuicaoEntrega.query.filter(AtribuicaoEntrega.data_entrega == d).all()
        n_entregas = len(atribs)
        n_entregas_feitas = sum(1 for a in atribs if a.status == 'entregue')
        n_entregas_falhas = sum(1 for a in atribs if a.status == 'nao_entregue')

        # Entradas de MP (compras) — valor + count
        movs = MovimentacaoEstoque.query.filter(
            MovimentacaoEstoque.tipo == 'entrada',
            sqlfunc.date(MovimentacaoEstoque.data) == d,
        ).all()
        valor_compras = sum((m.quantidade or 0) * (m.preco_unitario or 0) for m in movs)
        n_compras = len(movs)

        return {
            'data': d,
            'valor_locais': valor_locais,
            'n_locais': len(locais),
            'n_ped_loja': n_ped_loja,
            'n_ped_loja_entregue': n_ped_loja_entregue,
            'n_entregas': n_entregas,
            'n_entregas_feitas': n_entregas_feitas,
            'n_entregas_falhas': n_entregas_falhas,
            'valor_compras': valor_compras,
            'n_compras': n_compras,
        }

    hoje_m = metricas_do_dia(data_alvo)
    ontem_m = metricas_do_dia(ontem)
    semana_m = metricas_do_dia(semana_atras)

    def delta_pct(atual, anterior):
        if not anterior:
            return None
        return ((atual - anterior) / anterior) * 100

    return render_template('main/caixa.html',
                           hoje=hoje_m, ontem=ontem_m, semana=semana_m,
                           data_alvo=data_alvo, ontem_data=ontem,
                           semana_data=semana_atras,
                           delta_locais=delta_pct(hoje_m['valor_locais'], ontem_m['valor_locais']),
                           delta_entregas=delta_pct(hoje_m['n_entregas'], ontem_m['n_entregas']))


@main_bp.route('/admin/debug-papeis')
@owner_required
def debug_papeis():
    """Lista usuarios + papel + tools que o copilot vai oferecer pra cada um.

    Usado pra diagnostico quando alguem reclama 'copilot disse que nao
    posso fazer X'. Owner-only.
    """
    from app.models import SlackVinculo, Usuario
    from app.services.copilot import papel_efetivo, tools_permitidas

    users = Usuario.query.order_by(Usuario.papel, Usuario.nome).all()
    # Indexa vinculos por usuario_id — pode haver MAIS DE UM por user, em
    # tese (slack_user_id diferentes). Lista pra ver todos.
    vinculos_por_user = {}
    for v in SlackVinculo.query.filter_by(ativo=True).all():
        vinculos_por_user.setdefault(v.usuario_id, []).append(v.slack_user_id)

    linhas = []
    for u in users:
        papel = papel_efetivo(u)
        tools = sorted([t['name'] for t in tools_permitidas(u)])
        slacks = vinculos_por_user.get(u.id, [])
        linhas.append({
            'id': u.id,
            'nome': u.nome,
            'login': u.login,
            'papel_db': u.papel,
            'is_owner': bool(getattr(u, 'is_owner', False)),
            'papel_efetivo': papel,
            'loja_id': u.loja_id,
            'tools_count': len(tools),
            'tem_criar_pedido': 'criar_pedido' in tools,
            'tem_receber_mp': 'receber_mp' in tools,
            'tem_registrar_desperdicio': 'registrar_desperdicio' in tools,
            'slack_user_ids': slacks,
        })

    # Tabela secundaria: TODOS os vinculos slack ativos com slack_user_id
    # e quem cada um aponta. Util pra detectar vinculo apontando pra
    # usuario errado (ex: slack do Kelvin vinculado a um funcionario).
    todos_vinculos = []
    user_por_id = {u.id: u for u in users}
    for v in SlackVinculo.query.filter_by(ativo=True).order_by(SlackVinculo.slack_user_id).all():
        alvo = user_por_id.get(v.usuario_id)
        todos_vinculos.append({
            'slack_user_id': v.slack_user_id,
            'usuario_id': v.usuario_id,
            'alvo_nome': alvo.nome if alvo else '(usuario nao encontrado!)',
            'alvo_papel': alvo.papel if alvo else '?',
        })
    return render_template('main/debug_papeis.html', linhas=linhas,
                           todos_vinculos=todos_vinculos)


@main_bp.route('/admin/permissoes', methods=['GET', 'POST'])
@owner_required
def permissoes_editar():
    """Matriz editavel papel x capacidade (web + copilot + Slack). Owner-only.

    Admin/owner nao aparecem na matriz (acesso total fixo). Os padroes espelham
    o comportamento legado — so o que voce mudar aqui passa a valer (na hora)."""
    from flask import flash

    from app.services import permissoes as perm_svc

    if request.method == 'POST':
        perm_svc.salvar(request.form)
        flash('Permissões atualizadas.', 'success')
        return redirect(url_for('main.permissoes_editar'))

    return render_template('main/permissoes.html',
                           linhas=perm_svc.estado_atual(),
                           papeis=perm_svc.PAPEIS_EDITAVEIS,
                           papel_label=perm_svc.PAPEL_LABEL)


@main_bp.route('/admin/debug-schema')
@owner_required
def debug_schema():
    """Diagnostico de schema/migrations Alembic. Owner-only."""

    from flask import current_app as _app
    from sqlalchemy import inspect, text

    from app.services import chatbot_vigia, seru_cron

    info = {
        'alembic_current': None,
        'alembic_heads': [],
        'pendentes': [],
        'erro_alembic': None,
        'colunas': [],
        'erro_colunas': None,
        'last_upgrade_log': request.args.get('log'),
        'last_upgrade_ok': request.args.get('ok'),
        'backup_status': seru_cron.status_backup(),
        'vigia_status': {
            'ligado': bool(_app.config.get('CHATBOT_VIGIA')),
            'numero_destino': chatbot_vigia._numero_destino(),
        },
    }

    # 1. Alembic current vs heads
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        cfg = Config('migrations/alembic.ini')
        cfg.set_main_option('script_location', 'migrations')
        script = ScriptDirectory.from_config(cfg)
        info['alembic_heads'] = list(script.get_heads())

        with db.engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current = ctx.get_current_revision()
            info['alembic_current'] = current

        if info['alembic_heads']:
            # walk_revisions vai de HEAD pra BASE. Pendentes = tudo desde
            # head ate (exclusive) o current. Se current=None, tudo eh
            # pendente. Se current=head, nada.
            pendentes_revs = []
            for rev in script.walk_revisions(base='base', head='heads'):
                if rev.revision == info['alembic_current']:
                    break
                pendentes_revs.append({
                    'revision': rev.revision,
                    'down': rev.down_revision,
                    'doc': (rev.doc or '')[:120],
                })
            # walk vai do head pra base, mas queremos mostrar a ordem de
            # aplicacao (base → head): inverte.
            info['pendentes'] = list(reversed(pendentes_revs))
    except Exception as e:  # noqa: BLE001
        info['erro_alembic'] = f'{type(e).__name__}: {e}'

    # 2. Colunas criticas (resultado das migrations B4/B5)
    try:
        insp = inspect(db.engine)

        def col_info(tabela, coluna):
            try:
                cols = {c['name']: c for c in insp.get_columns(tabela)}
                if coluna not in cols:
                    return {'tabela': tabela, 'coluna': coluna, 'existe': False,
                            'tipo': None, 'nullable': None}
                c = cols[coluna]
                return {'tabela': tabela, 'coluna': coluna, 'existe': True,
                        'tipo': str(c.get('type')), 'nullable': c.get('nullable')}
            except Exception as e:  # noqa: BLE001
                return {'tabela': tabela, 'coluna': coluna, 'existe': None,
                        'tipo': f'ERRO: {type(e).__name__}: {e}',
                        'nullable': None}

        info['colunas'] = [
            col_info('produto_item', 'receita_id'),
            col_info('produto_item', 'materia_prima_id'),
            col_info('produto_item', 'item_nome'),
            col_info('venda_b2b', 'valor_total'),
            col_info('venda_b2b_item', 'preco_unitario'),
            col_info('venda_b2b_parcela', 'valor'),
            col_info('venda_b2b_parcela', 'valor_pago'),
            col_info('venda_manual_loja', 'valor_unitario'),
            col_info('seru_debito_mov', 'fracao'),
        ]
    except Exception as e:  # noqa: BLE001
        info['erro_colunas'] = f'{type(e).__name__}: {e}'

    # 3. Detecta estado misto: DDL aplicado parcialmente mas alembic_version
    # atrasado. Usado pra sugerir stamp manual antes de tentar upgrade.
    info['estado_misto'] = None
    try:
        insp = inspect(db.engine)
        tabelas = set(insp.get_table_names())
        cols_pi = {c['name'] for c in insp.get_columns('produto_item')}
        cols_receita = {c['name'] for c in insp.get_columns('receita')}
        cols_produto = {c['name'] for c in insp.get_columns('produto')}
        cols_pedido_loja = {c['name'] for c in insp.get_columns('pedido_loja')}
        cols_pedido_item = {c['name'] for c in insp.get_columns('pedido_item')}
        cols_estoque_producao = {
            c['name'] for c in insp.get_columns('estoque_producao')
        }
        cols_estoque_loja = {c['name'] for c in insp.get_columns('estoque_loja')}
        cols_notificacao = {
            c['name'] for c in insp.get_columns('notificacao_whatsapp')
        }
        cols_vb2b = {c['name']: c for c in insp.get_columns('venda_b2b')}
        vt = cols_vb2b.get('valor_total', {})
        vt_tipo = str(vt.get('type', '')) if vt else ''

        b9_ddl = 'seru_debito_mov' in tabelas
        b4_ddl = 'NUMERIC' in vt_tipo.upper()
        b5_ddl = 'receita_id' in cols_pi
        # B11/B12 tambem podem ter sido aplicadas antes de o marcador do
        # Alembic ser atualizado. Sem essas verificacoes, o diagnostico
        # recomendava voltar ate B5 e o upgrade tentava recriar colunas que ja
        # existiam (DuplicateColumn em produto_componente_id).
        b11_ddl = 'produto_componente_id' in cols_pi
        b12_ddl = ('reaproveitavel' in cols_receita
                   and 'reaproveitavel' in cols_produto)
        b13_ddl = 'pedido_item_foto' in tabelas
        b14_ddl = 'driver_id' in cols_pedido_loja
        b15_ddl = 'driver_magic_token' in tabelas
        b16_ddl = (
            'familia' in cols_receita
            and 'estado' in cols_pedido_item
            and 'estado' in cols_estoque_producao
            and 'estado' in cols_estoque_loja
        )
        b17_ddl = 'zaap_id' in cols_notificacao

        # Calcula qual e a revision mais avancada que ja teve seu DDL aplicado
        ddl_avancado_em = '69d82afed149'  # baseline
        if b9_ddl:
            ddl_avancado_em = 'ac57b6648ec4'  # B9
        if b9_ddl and b4_ddl:
            ddl_avancado_em = '643bd66e89c3'  # B4
        if b9_ddl and b4_ddl and b5_ddl:
            ddl_avancado_em = 'efb6e5837fd0'  # B5
        if b9_ddl and b4_ddl and b5_ddl and b11_ddl:
            ddl_avancado_em = '8f2c4a1b7d9e'  # B11
        if b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl:
            ddl_avancado_em = '9c3d1a5e8b2f'  # B12
        if (b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl
                and b13_ddl):
            ddl_avancado_em = '4a8e2d6f1c5b'  # B13
        if (b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl
                and b13_ddl and b14_ddl):
            ddl_avancado_em = 'd2f5c9a1b7e3'  # B14
        if (b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl
                and b13_ddl and b14_ddl and b15_ddl):
            ddl_avancado_em = 'e7b4c2a8d5f1'  # B15
        if (b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl
                and b13_ddl and b14_ddl and b15_ddl and b16_ddl):
            ddl_avancado_em = '1f7a3c9d8e4b'  # B16
        if (b9_ddl and b4_ddl and b5_ddl and b11_ddl and b12_ddl
                and b13_ddl and b14_ddl and b15_ddl and b16_ddl
                and b17_ddl):
            ddl_avancado_em = '2b8d4e6f0a1c'  # B17

        if info['alembic_current'] != ddl_avancado_em:
            info['estado_misto'] = {
                'alembic_diz': info['alembic_current'],
                'ddl_real': ddl_avancado_em,
                'b9_ddl': b9_ddl,
                'b4_ddl': b4_ddl,
                'b5_ddl': b5_ddl,
                'b11_ddl': b11_ddl,
                'b12_ddl': b12_ddl,
                'b13_ddl': b13_ddl,
                'b14_ddl': b14_ddl,
                'b15_ddl': b15_ddl,
                'b16_ddl': b16_ddl,
                'b17_ddl': b17_ddl,
            }
    except Exception as e:  # noqa: BLE001
        info['estado_misto'] = {'erro': f'{type(e).__name__}: {e}'}

    # 4. Contagem rapida de orfaos (so se B5 ja aplicou)
    info['orfaos'] = None
    try:
        cols_pi = {c['name'] for c in inspect(db.engine).get_columns('produto_item')}
        if 'receita_id' in cols_pi:
            with db.engine.connect() as conn:
                o_r = conn.execute(text(
                    "SELECT COUNT(*) FROM produto_item "
                    "WHERE tipo = 'receita' AND receita_id IS NULL"
                )).scalar() or 0
                o_m = conn.execute(text(
                    "SELECT COUNT(*) FROM produto_item "
                    "WHERE tipo = 'mp' AND materia_prima_id IS NULL"
                )).scalar() or 0
                info['orfaos'] = {'receita': o_r, 'mp': o_m}
    except Exception as e:  # noqa: BLE001
        info['orfaos'] = {'erro': f'{type(e).__name__}: {e}'}

    return render_template('main/debug_schema.html', info=info)


@main_bp.route('/admin/debug-tiny')
@owner_required
def debug_tiny():
    """Owner-only: testa busca no Tiny pra (CPF, numero). Mostra exatamente o
    que a API v2 do Tiny retornou — util pra debugar bot achando 'nao
    encontrado' quando o pedido existe no painel."""
    from app.services import tiny
    cpf = (request.args.get('cpf') or '').strip()
    numero = (request.args.get('numero') or '').strip()
    resultado: dict = {'cpf': cpf, 'numero': numero,
                       'tiny_disponivel': tiny.disponivel()}
    if cpf and numero:
        try:
            cpf_d = ''.join(c for c in cpf if c.isdigit())
            # 1. Pesquisa por CPF (v2 ignora filtros de numero — visto antes)
            r_pesq = tiny._get('pedidos.pesquisa.php',
                                params={'cpf_cnpj': cpf_d, 'pagina': '1'})
            pesq_dict = r_pesq if isinstance(r_pesq, dict) else {}
            primeiros = pesq_dict.get('pedidos') or []
            campos = []
            if primeiros and isinstance(primeiros[0], dict):
                p0 = primeiros[0].get('pedido') or {}
                if isinstance(p0, dict):
                    campos = list(p0.keys())
            resultado['pesquisa'] = {
                'status': pesq_dict.get('status'),
                'qtd': len(primeiros),
                'campos_disponiveis': campos,
            }

            # 2. Funcao de alto nivel — o que o bot enxerga
            pedido = tiny.buscar_pedido_por_cpf_e_numero(cpf, numero)
            resultado['pedido_resolvido'] = pedido

            # 3. Se achou o pedido, traz o detalhe CRU
            if pedido and pedido.get('id'):
                r_det = tiny._get('pedido.obter.php',
                                   params={'id': pedido['id']})
                resultado['detalhe_cru'] = r_det if isinstance(r_det, dict) else None
                if isinstance(r_det, dict):
                    p_det = r_det.get('pedido') or {}
                    if not isinstance(p_det, dict):
                        p_det = {}
                    # v2 retorna nota_fiscal (sing) OU notas_fiscais (lista)
                    nf = p_det.get('nota_fiscal')
                    if not isinstance(nf, dict):
                        nf = {}
                    if not nf:
                        lista = p_det.get('notas_fiscais') or []
                        if isinstance(lista, list) and lista:
                            primeiro = lista[0]
                            if isinstance(primeiro, dict):
                                nf = primeiro.get('nota_fiscal') or primeiro
                                if not isinstance(nf, dict):
                                    nf = {}
                    resultado['nota_fiscal_extraida'] = nf
                    nf_id = nf.get('id') if isinstance(nf, dict) else None
                    if nf_id:
                        r_link = tiny._get('nota.fiscal.obter.link.php',
                                            params={'id': str(nf_id)})
                        resultado['link_resposta'] = r_link if isinstance(r_link, dict) else None
                        resultado['link_resolvido'] = tiny.obter_link_nota_fiscal(nf_id)
        except Exception as exc:  # noqa: BLE001
            resultado['erro_exception'] = f'{type(exc).__name__}: {exc}'
            import traceback
            resultado['traceback'] = traceback.format_exc()[-1500:]

    return jsonify(resultado), 200


@main_bp.route('/admin/frete-sensores')
@owner_required
def frete_sensores():
    """Sensor do geocode do frete (09/07/2026): mostra ao dono os endereços
    que BARRARAM venda, os que cotaram IMPRECISO (centroide de CEP) e o uso/
    custo do Google — pra saber se está perdendo venda por endereço."""
    from app.services import frete_sensor
    dias = request.args.get('dias', type=int) or 7
    return render_template('admin/frete_sensores.html',
                           r=frete_sensor.resumo(dias), dias=dias)


# ── Marketing por e-mail (Listmonk) — 05/08/2026 ──

@main_bp.route('/admin/marketing')
@owner_required
def marketing_painel():
    """Painel do e-mail marketing: estado das listas, texto do aniversário e
    a chave que liga o disparo automático."""
    from app.services import marketing
    return render_template('admin/marketing.html', r=marketing.resumo())


@main_bp.route('/admin/marketing/sincronizar', methods=['POST'])
@owner_required
def marketing_sincronizar():
    """Empurra a base pro Listmonk agora (o cron já faz isso às 09:00)."""
    from app.services import marketing
    st = marketing.sincronizar()
    if st.get('erro'):
        flash(f'Sincronização falhou: {st["erro"]}', 'danger')
    else:
        flash(f'Base sincronizada: {st["site"]} do site, {st["wifi"]} do '
              f'Wi-Fi, {st["descadastros"]} descadastro(s) registrado(s).',
              'success')
    return redirect(url_for('main.marketing_painel'))


@main_bp.route('/admin/marketing/importar', methods=['POST'])
@owner_required
def marketing_importar_planilha():
    """Sobe uma planilha (sorteio, evento, lista de papel) pra uma lista."""
    from app.services import marketing
    arq = request.files.get('planilha')
    if not arq or not arq.filename:
        flash('Escolha um arquivo .xlsx ou .csv.', 'warning')
        return redirect(url_for('main.marketing_painel'))
    lista = (request.form.get('lista') or marketing.LISTA_SORTEIO).strip()
    st = marketing.importar_planilha(arq.stream, arq.filename, lista)
    if st.get('erro'):
        flash(f'Import falhou: {st["erro"]}', 'danger')
    else:
        flash(f'{st["validos"]} e-mail(s) importados para "{lista}" '
              f'({st.get("repetidos", 0)} repetidos e '
              f'{st.get("invalidos", 0)} inválidos ficaram de fora).',
              'success')
    return redirect(url_for('main.marketing_painel'))


@main_bp.route('/admin/marketing/teste', methods=['POST'])
@owner_required
def marketing_teste():
    """Manda uma peça (HTML) pro e-mail informado, sem disparar campanha.

    Serve pra conferir no Gmail antes de mandar pra base — o preview do
    navegador não mostra o que o cliente de e-mail faz com o HTML.
    """
    from app.services import marketing
    st = marketing.enviar_teste(request.form.get('assunto'),
                                request.form.get('corpo'),
                                request.form.get('email'),
                                request.form.get('nome_peca') or 'Peça')
    if st.get('erro'):
        flash(f'Teste não saiu: {st["erro"]}', 'danger')
    else:
        flash(f'Teste enviado para {request.form.get("email")}. '
              f'Se não chegar em alguns minutos, olhe o spam.', 'success')
    return redirect(url_for('main.marketing_painel'))


@main_bp.route('/admin/marketing/rascunho', methods=['POST'])
@owner_required
def marketing_rascunho():
    """Cria a campanha no Listmonk (rascunho) a partir do HTML da tela."""
    from app.services import marketing
    st = marketing.criar_rascunho(request.form.get('assunto'),
                                  request.form.get('corpo'),
                                  request.form.get('lista'),
                                  request.form.get('nome_peca'))
    if st.get('erro'):
        flash(f'Rascunho não criado: {st["erro"]}', 'danger')
    else:
        flash(f'Rascunho criado no Listmonk para a lista "{st["lista"]}". '
              f'Confira e dispare por lá: {st["url"]}', 'success')
    return redirect(url_for('main.marketing_painel'))


@main_bp.route('/admin/marketing/salvar', methods=['POST'])
@owner_required
def marketing_salvar():
    """Salva o texto do e-mail de aniversário e a chave do automático."""
    from app.models import AppConfig
    from app.services import marketing
    AppConfig.set(marketing.CFG_ANIV_ASSUNTO,
                  (request.form.get('assunto') or '').strip())
    AppConfig.set(marketing.CFG_ANIV_CORPO,
                  (request.form.get('corpo') or '').strip())
    ligado = request.form.get('auto') == '1'
    AppConfig.set(marketing.CFG_ANIV_ATIVO, '1' if ligado else '0')
    db.session.commit()
    flash('Salvo. Disparo automático %s.'
          % ('LIGADO — sai todo dia às 09:00' if ligado else 'desligado'),
          'success' if ligado else 'info')
    return redirect(url_for('main.marketing_painel'))


@main_bp.route('/admin/marketing/aniversario', methods=['POST'])
@owner_required
def marketing_aniversario_agora():
    """Monta a campanha de aniversário de hoje AGORA.

    `enviar=1` dispara de verdade (gesto explícito, mesmo com o automático
    desligado); sem ele, cria só o rascunho pra conferir no Listmonk.
    """
    from app.services import marketing
    enviar = request.form.get('enviar') == '1'
    st = marketing.campanha_aniversario(enviar=enviar, forcar=True)
    if st.get('erro'):
        flash(f'Campanha falhou: {st["erro"]}', 'danger')
    elif st.get('enviada'):
        flash(f'Campanha enviada para {st["n"]} aniversariante(s).', 'success')
    elif st.get('campanha_id'):
        flash(f'Rascunho criado no Listmonk para {st["n"]} aniversariante(s) '
              f'— confira e envie por lá.', 'info')
    else:
        flash(f'Nada a enviar: {st.get("pulou") or "sem aniversariantes"}.',
              'info')
    return redirect(url_for('main.marketing_painel'))


# ── Avaliacoes do Google (Business Profile) — 12/07/2026 ──

@main_bp.route('/admin/avaliacoes-google')
@owner_required
def avaliacoes_google():
    """Painel de avaliacoes do Google: nota geral, lista, responder + vincular
    location->loja. Dormente ate o OAuth+aprovacao do Google (mostra o estado
    'nao conectado' com o botao de conectar)."""
    from app.models import Loja
    from app.services import google_reviews
    nota = request.args.get('nota', type=int)
    sem_resposta = request.args.get('sem_resposta') == '1'
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template(
        'admin/avaliacoes_google.html',
        r=google_reviews.resumo(nota=nota, sem_resposta=sem_resposta),
        lojas=lojas)


@main_bp.route('/admin/avaliacoes-google/conectar')
@owner_required
def avaliacoes_google_conectar():
    """Inicia o OAuth: gera state anti-CSRF na sessao e manda pro Google."""
    import secrets

    from flask import flash, redirect, session, url_for

    from app.services import google_reviews
    estado = secrets.token_urlsafe(24)
    session['google_oauth_state'] = estado
    redirect_uri = url_for('main.avaliacoes_google_callback', _external=True)
    url = google_reviews.url_autorizacao(redirect_uri, estado)
    if not url:
        flash('Configure GOOGLE_OAUTH_CLIENT_ID/SECRET no Railway primeiro.',
              'danger')
        return redirect(url_for('main.avaliacoes_google'))
    return redirect(url)


@main_bp.route('/admin/avaliacoes-google/callback')
@owner_required
def avaliacoes_google_callback():
    """Callback do OAuth: valida o state e troca o code por tokens."""
    from flask import flash, redirect, session, url_for

    from app.services import google_reviews
    if request.args.get('error'):
        flash(f'Google recusou: {request.args.get("error")}', 'danger')
        return redirect(url_for('main.avaliacoes_google'))
    esperado = session.pop('google_oauth_state', None)
    if not esperado or request.args.get('state') != esperado:
        flash('Falha de seguranca na conexao (state invalido). Tente de novo.',
              'danger')
        return redirect(url_for('main.avaliacoes_google'))
    code = request.args.get('code')
    if not code:
        flash('Google nao devolveu o codigo de autorizacao.', 'danger')
        return redirect(url_for('main.avaliacoes_google'))
    redirect_uri = url_for('main.avaliacoes_google_callback', _external=True)
    ok, msg = google_reviews.trocar_codigo(code, redirect_uri)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('main.avaliacoes_google'))


@main_bp.route('/admin/avaliacoes-google/desconectar', methods=['POST'])
@owner_required
def avaliacoes_google_desconectar():
    from flask import flash, redirect, url_for

    from app.services import google_reviews
    google_reviews.desconectar()
    flash('Conta Google desconectada.', 'info')
    return redirect(url_for('main.avaliacoes_google'))


@main_bp.route('/admin/avaliacoes-google/sincronizar', methods=['POST'])
@owner_required
def avaliacoes_google_sincronizar():
    from flask import flash, redirect, url_for

    from app.services import google_reviews
    if not google_reviews.disponivel():
        flash('Conecte a conta Google antes de sincronizar.', 'warning')
        return redirect(url_for('main.avaliacoes_google'))
    try:
        novas = google_reviews.sincronizar()
        flash(f'Sincronizado. {len(novas)} avaliacao(oes) nova(s).', 'success')
    except Exception:  # noqa: BLE001 — a tela nunca deve dar 500 no sync manual
        current_app.logger.exception('sincronizacao manual de reviews falhou')
        db.session.rollback()
        flash('Falha ao sincronizar com o Google. Tente de novo.', 'danger')
    return redirect(url_for('main.avaliacoes_google'))


@main_bp.route('/admin/avaliacoes-google/<int:rid>/responder', methods=['POST'])
@owner_required
def avaliacoes_google_responder(rid):
    from flask import flash, redirect, url_for

    from app.services import google_reviews
    texto = (request.form.get('resposta') or '').strip()
    ok, msg = google_reviews.responder(rid, texto, user_id=current_user.id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('main.avaliacoes_google'))


@main_bp.route('/admin/avaliacoes-google/<int:rid>/rascunho', methods=['POST'])
@owner_required
def avaliacoes_google_rascunho(rid):
    """Rascunho de resposta por IA (nao publica). Devolve JSON pro JS preencher
    a caixa de resposta."""
    from app.services import google_reviews
    texto, msg = google_reviews.rascunho_resposta(rid)
    return jsonify(ok=bool(texto), texto=texto or '', msg=msg)


@main_bp.route('/admin/avaliacoes-google/location/<int:lid>/vincular',
               methods=['POST'])
@owner_required
def avaliacoes_google_vincular_loja(lid):
    from flask import flash, redirect, url_for

    from app.models import GoogleReviewLocation
    loc = GoogleReviewLocation.query.get_or_404(lid)
    loja_id = request.form.get('loja_id', type=int)
    loc.loja_id = loja_id or None
    db.session.commit()
    flash(f'Location "{loc.apelido or loc.location_name}" vinculada.', 'success')
    return redirect(url_for('main.avaliacoes_google'))


@main_bp.route('/admin/debug-tiny-nota')
@owner_required
def debug_tiny_nota():
    """Owner-only: mostra a resposta CRUA do Tiny pro link do DANFE de uma
    NF, pelo id da nota. Uso: /admin/debug-tiny-nota?id=909358497
    (o id aparece em rosa no card 'Nota Fiscal (Tiny)' do detalhe da venda).

    Serve pra saber por que o 'Ver DANFE'/'Enviar NF' falha mesmo com a NF
    autorizada — mostra os campos de link que o Tiny devolveu e a causa
    exata da falha (em vez do genérico 'precisa estar autorizada')."""
    from app.services import tiny, tiny_nf
    nota_id = (request.args.get('id') or '').strip()
    out = {'nota_id': nota_id, 'tiny_disponivel': tiny.disponivel()}
    if not nota_id:
        out['uso'] = 'passe ?id=<id_da_nota_no_tiny>'
        return jsonify(out), 400
    retorno = tiny._get('nota.fiscal.obter.link.php',
                        params={'id': nota_id}, retornar_erro=True)
    if isinstance(retorno, dict):
        out['retorno_status'] = retorno.get('status')
        # Só as chaves de link (não vaza o resto) + erros, se houver.
        out['campos_link'] = {k: retorno.get(k) for k in
                              ('link_danfe', 'link_pdf', 'link_nfe', 'link')
                              if retorno.get(k)}
        out['erros'] = tiny._extrair_erros(retorno) or None
    else:
        out['retorno'] = None
        out['motivo_falha'] = tiny._consumir_falha()
    link, motivo = tiny.obter_link_nota_fiscal_com_motivo(nota_id)
    out['link_resolvido'] = link
    out['motivo'] = motivo
    if link:
        # Baixa e confere se é PDF de verdade (é onde o e-mail engasga).
        _pdf, motivo_pdf = tiny_nf.baixar_danfe_pdf_com_motivo(nota_id)
        out['pdf_ok'] = bool(_pdf)
        out['pdf_motivo'] = motivo_pdf
        out['pdf_tamanho'] = len(_pdf) if _pdf else 0
        # Se ainda falhou, mostra a estrutura da página do Olist (candidatos
        # de PDF + trecho) pra eu saber como extrair o PDF embutido.
        if not _pdf:
            import requests
            try:
                r = requests.get(link, timeout=20,
                                 headers={'User-Agent': tiny_nf._UA_NAVEGADOR})
                out['pagina_status'] = r.status_code
                out['pagina_ctype'] = r.headers.get('Content-Type')
                if 'html' in (r.headers.get('Content-Type') or '').lower():
                    out['pdf_candidatos'] = tiny_nf._candidatos_pdf_na_pagina(
                        r.text, r.url)
                    out['html_inicio'] = (r.text or '')[:800]
            except requests.RequestException as exc:
                out['pagina_erro'] = str(exc)
    return jsonify(out), 200


@main_bp.route('/admin/debug-foto-pdf')
@owner_required
def debug_foto_pdf():
    """Owner-only: por que a foto N nao vai pro PDF?

    Uso: /admin/debug-foto-pdf?foto_id=123  (ou ?pedido_id=999 = primeira foto)

    Testa cada caminho de download separado e mostra o que veio:
    1. API autenticada (storage_path)
    2. Shared link com User-Agent + raw=1
    3. Shared link cru (como estava antes do fix)
    4. BLOB legado
    """
    import requests as _req

    from app.models import FotoRecebimento
    from app.services import dropbox_storage as ds
    foto_id = request.args.get('foto_id', type=int)
    pedido_id = request.args.get('pedido_id', type=int)
    if foto_id:
        f = FotoRecebimento.query.get(foto_id)
    elif pedido_id:
        f = FotoRecebimento.query.filter_by(pedido_id=pedido_id).first()
    else:
        return jsonify(erro='passe ?foto_id=N ou ?pedido_id=N'), 400
    if not f:
        return jsonify(erro='foto nao encontrada'), 404

    out = {
        'foto_id': f.id, 'pedido_id': f.pedido_id,
        'tem_imagem_url': bool(f.imagem_url),
        'imagem_url': f.imagem_url,
        'tem_storage_path': bool(f.imagem_storage_path),
        'storage_path': f.imagem_storage_path,
        'tem_blob': bool(f.imagem),
        'blob_len': len(f.imagem) if f.imagem else 0,
        'mimetype': f.mimetype,
        'dropbox_configurado': ds.disponivel(),
    }

    # 1. API autenticada
    if f.imagem_storage_path and ds.disponivel():
        b = ds.baixar(f.imagem_storage_path)
        out['api_autenticada'] = {
            'ok': bool(b),
            'tamanho': len(b) if b else 0,
            'magic': (b[:4].hex() if b else None),
            'motivo_falha': ds.consumir_falha_download() if not b else None,
        }
    # 2. Shared link com User-Agent + raw
    if f.imagem_url:
        try:
            url_raw = ds._converter_para_raw(f.imagem_url)
            r = _req.get(url_raw, timeout=15,
                          headers={'User-Agent':
                                   'Mozilla/5.0 (compatible; DebugPDF/1.0)'})
            ct = r.headers.get('Content-Type', '')
            corpo = r.content or b''
            out['shared_link_raw_com_ua'] = {
                'url': url_raw,
                'status': r.status_code,
                'content_type': ct,
                'tamanho': len(corpo),
                'magic': corpo[:4].hex() if corpo else None,
                'eh_html': corpo[:200].lstrip().lower().startswith(b'<!doctype')
                            or corpo[:200].lstrip().lower().startswith(b'<html'),
                'inicio': corpo[:80].decode('latin-1', errors='replace'),
            }
        except Exception as e:  # noqa: BLE001
            out['shared_link_raw_com_ua'] = {'erro': str(e)}
        # 3. Shared link sem User-Agent + sem raw (jeito antigo)
        try:
            r2 = _req.get(f.imagem_url, timeout=15)
            corpo2 = r2.content or b''
            out['shared_link_cru'] = {
                'status': r2.status_code,
                'content_type': r2.headers.get('Content-Type', ''),
                'tamanho': len(corpo2),
                'magic': corpo2[:4].hex() if corpo2 else None,
            }
        except Exception as e:  # noqa: BLE001
            out['shared_link_cru'] = {'erro': str(e)}

    return jsonify(out), 200


@main_bp.route('/admin/debug-nflog')
@owner_required
def debug_nflog():
    """Owner-only: ultimas 50 entradas do NFLog (audit das solicitacoes de NF
    pelo bot). Util pra ver POR QUE o bot disse 'nao encontrei' num caso real:
    o `resultado` + `detalhe` revelam onde foi recusado e com qual numero."""
    from app.models import NFLog
    qs = NFLog.query.order_by(NFLog.id.desc()).limit(50).all()
    return jsonify([{
        'id': r.id,
        'em': r.criado_em.isoformat() if r.criado_em else None,
        'conv': r.conv_id, 'canal': r.canal,
        'cpf_4': r.cpf_4ultimos,
        'numero_buscado': r.numero_pedido,
        'resultado': r.resultado,
        'detalhe': r.detalhe,
    } for r in qs]), 200


@main_bp.route('/admin/debug-vnda-cartinha')
@owner_required
def debug_vnda_cartinha():
    """Owner-only: sonda a API VNDA pra descobrir se da pra ESCREVER a cartinha
    (customization) de um pedido ja fechado. So GET + OPTIONS — NAO grava nada.

    A cartinha no VNDA se grava no CARRINHO (/carts/...), nao no pedido
    (/orders/... e read-only). Esta rota investiga se o carrinho do pedido
    ainda eh alcancavel/gravavel depois de fechado.

    Uso: /admin/debug-vnda-cartinha?code=CODIGO_DO_PEDIDO
    """
    import requests

    from app.services import vnda
    code = (request.args.get('code') or '').strip()
    out: dict = {'code': code}
    if not code:
        out['erro'] = 'passe ?code=CODIGO_DO_PEDIDO (ex: ?code=DA19F38765)'
        return jsonify(out), 200

    base = vnda._base_url()
    headers = vnda._headers()

    def _probe(method, path, **kw):
        """GET/OPTIONS seguro. Devolve status + Allow + corpo (truncado)."""
        try:
            r = requests.request(method, f'{base}{path}', headers=headers,
                                  timeout=10, **kw)
            try:
                body = r.json()
            except ValueError:
                body = (r.text or '')[:400]
            return {'status': r.status_code, 'allow': r.headers.get('Allow'),
                    'body': body}
        except requests.RequestException as e:
            return {'erro': str(e)}

    try:
        # 1. Pedido completo: chaves + campos candidatos a ligar no carrinho
        ped = _probe('GET', f'/orders/{code}')
        out['pedido_status'] = ped.get('status')
        body = ped.get('body') if isinstance(ped.get('body'), dict) else {}
        out['pedido_chaves'] = sorted(body.keys()) if body else None
        # Campos que tipicamente referenciam o carrinho (sem despejar PII)
        out['campos_cart'] = {k: body.get(k) for k in
                              ('token', 'cart_id', 'cart_token', 'cart', 'id',
                               'number', 'code')
                              if k in body}
        itens = body.get('items') or []
        out['itens'] = [{'id': it.get('id'), 'sku': it.get('sku'),
                         'nome': it.get('product_name') or it.get('name'),
                         'has_customizations': it.get('has_customizations')}
                        for it in itens]

        # Campos de NIVEL DE PEDIDO que poderiam conter a "cartinha escondida"
        # (mensagem de entrega / observacao). Se o texto da cartinha aparecer
        # aqui, da pra editar via PATCH /orders — bem mais facil que o carrinho.
        out['campos_mensagem'] = {k: body.get(k) for k in
                                  ('note', 'delivery_message', 'extra',
                                   'user_code', 'agent', 'channel')
                                  if k in body}

        # 2. Customizations atuais (READ — ja sabemos que funciona)
        cust = {}
        for it in itens[:5]:
            iid = it.get('id')
            if iid:
                cust[str(iid)] = _probe(
                    'GET', f'/orders/{code}/items/{iid}/customizations')
        out['customizations_pedido'] = cust

        # 3. Tenta alcancar o CARRINHO por token E por cart_id numerico.
        # (O token deu carrinho vazio antes; o cart_id numerico pode diferir.)
        out['cart_por_token'] = None
        out['cart_por_id'] = None
        tok = body.get('token')
        cid = body.get('cart_id')
        if tok:
            out['cart_por_token'] = _probe('GET', f'/carts/{tok}/items')
        if cid:
            out['cart_por_id_meta'] = _probe('GET', f'/carts/{cid}')
            out['cart_por_id'] = _probe('GET', f'/carts/{cid}/items')
    except Exception as exc:  # noqa: BLE001
        import traceback
        out['erro_exception'] = f'{type(exc).__name__}: {exc}'
        out['traceback'] = traceback.format_exc()[-1500:]

    return jsonify(out), 200


@main_bp.route('/admin/debug-vnda-cartinha-write')
@owner_required
def debug_vnda_cartinha_write():
    """Owner-only TESTE DE ESCRITA da cartinha no pedido. Tenta um metodo HTTP
    (POST/PUT/DELETE) no endpoint de customizations do PEDIDO e reporta o
    status cru do VNDA. ESCREVE de verdade — por isso exige ?confirmo=sim.

    ⚠️ USE EM PEDIDO DE TESTE. Pode alterar/duplicar a cartinha real.

    Parametros:
      code, item_id   obrigatorios (vem do sondador read-only)
      metodo          post (default) | put | delete
      texto           texto da cartinha de teste
      grupo           group_name (default 'Cartinha')
      cust_id         id da customization (pra put/delete em recurso especifico)
      formato         body1 (default {group_name,name}) | body2 ({customizations:[...]})
    """
    import requests

    from app.services import vnda
    code = (request.args.get('code') or '').strip()
    item_id = (request.args.get('item_id') or '').strip()
    metodo = (request.args.get('metodo') or 'post').lower()
    texto = (request.args.get('texto') or 'TESTE BOT - pode apagar').strip()
    grupo = (request.args.get('grupo') or 'Cartinha').strip()
    cust_id = (request.args.get('cust_id') or '').strip()
    formato = (request.args.get('formato') or 'body1').strip()

    out: dict = {'code': code, 'item_id': item_id, 'metodo': metodo,
                 'formato': formato}
    if request.args.get('confirmo') != 'sim':
        out['erro'] = ('Faltou ?confirmo=sim. ATENCAO: esta rota ESCREVE no '
                       'VNDA. Rode so em pedido de TESTE.')
        return jsonify(out), 200
    if not code or not item_id:
        out['erro'] = 'precisa de ?code=...&item_id=... (pegue do sondador read-only)'
        return jsonify(out), 200

    base = vnda._base_url()
    headers = vnda._headers()
    path = f'/orders/{code}/items/{item_id}/customizations'
    if metodo in ('put', 'delete') and cust_id:
        path = f'{path}/{cust_id}'

    # Dois palpites de corpo — VNDA pode querer chave plana ou aninhada.
    if formato == 'body2':
        payload = {'customizations': [{'group_name': grupo, 'name': texto}]}
    else:
        payload = {'group_name': grupo, 'name': texto}

    out['url'] = f'{base}{path}'
    out['payload_enviado'] = payload
    try:
        kwargs = {} if metodo == 'delete' else {'json': payload}
        r = requests.request(metodo.upper(), f'{base}{path}',
                             headers=headers, timeout=12, **kwargs)
        try:
            rbody = r.json()
        except ValueError:
            rbody = (r.text or '')[:600]
        out['resposta'] = {'status': r.status_code,
                           'allow': r.headers.get('Allow'), 'body': rbody}
    except requests.RequestException as e:
        out['erro_req'] = str(e)

    return jsonify(out), 200


@main_bp.route('/admin/debug-schema/upgrade', methods=['POST'])
@owner_required
def debug_schema_upgrade():
    """Aplica migrations pendentes manualmente. Owner-only."""
    import io
    import logging
    import traceback as _tb

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    original_level = root.level
    root.setLevel(logging.INFO)

    ok = '1'
    try:
        from flask_migrate import upgrade as _upgrade
        _upgrade(directory='migrations')
        log_buf.write('\nOK: upgrade concluido sem exception.')
    except Exception:  # noqa: BLE001
        ok = '0'
        log_buf.write('\n--- TRACEBACK ---\n')
        log_buf.write(_tb.format_exc())
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    return redirect(url_for('main.debug_schema',
                            log=log_buf.getvalue()[-3000:], ok=ok))


@main_bp.route('/admin/venda-mapa/backfill', methods=['POST'])
@owner_required
def venda_mapa_backfill():
    """Backfill do VendaMapa unificado a partir de SeruProdutoMap +
    LojaProdutoMap. Idempotente, aditivo (nao muda comportamento). Owner-only."""
    from app.services.venda_mapa_migracao import backfill_venda_mapa
    try:
        r = backfill_venda_mapa()
        msg = ('VendaMapa backfill: %d novo(s) do Seru, %d do lote.'
               % (r['seru_novos'], r['lote_novos']))
        ok = '1'
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        msg, ok = 'Falha no backfill: %s' % e, '0'
    return redirect(url_for('main.debug_schema', log=msg, ok=ok))


@main_bp.route('/admin/venda-mapa/migrar-fracoes', methods=['POST'])
@owner_required
def venda_mapa_migrar_fracoes():
    """Migra as fracoes pendentes (SeruDebito + LojaDebito) pro DebitoEstoque
    unificado e zera as fontes. Passo de CUTOVER — idempotente. Owner-only."""
    from app.services.venda_mapa_migracao import migrar_fracoes_para_debito_estoque
    try:
        r = migrar_fracoes_para_debito_estoque(usuario_id=current_user.id)
        msg = ('Fracoes migradas: %d item(ns), %d inteiro(s) baixado(s).'
               % (r['itens'], r['inteiros_baixados']))
        ok = '1'
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        msg, ok = 'Falha na migracao de fracoes: %s' % e, '0'
    return redirect(url_for('main.debug_schema', log=msg, ok=ok))


@main_bp.route('/admin/debug-schema/stamp', methods=['POST'])
@owner_required
def debug_schema_stamp():
    """Marca alembic_version pra revision indicada SEM aplicar DDL.

    Uso: quando DDL ja foi aplicado por outro caminho (ex: tabela
    seru_debito_mov foi criada mas alembic_version voltou pra baseline
    por algum reset). Stamp realinha o controle sem executar migration.
    """
    import io
    import traceback as _tb

    revision = (request.form.get('revision') or '').strip()
    log_buf = io.StringIO()
    log_buf.write(f'Stamp pedido: revision={revision!r}\n')

    if not revision or len(revision) > 32 or not revision.replace('_', '').isalnum():
        log_buf.write('ERRO: revision invalida (precisa ser ID alfanumerico).')
        return redirect(url_for('main.debug_schema',
                                log=log_buf.getvalue(), ok='0'))

    ok = '1'
    try:
        from flask_migrate import stamp as _stamp
        _stamp(directory='migrations', revision=revision)
        log_buf.write(f'OK: alembic_version stampada em {revision}.\n')
    except Exception:  # noqa: BLE001
        ok = '0'
        log_buf.write('\n--- TRACEBACK ---\n')
        log_buf.write(_tb.format_exc())

    return redirect(url_for('main.debug_schema',
                            log=log_buf.getvalue()[-3000:], ok=ok))


@main_bp.route('/admin/slack/diagnostico')
@owner_required
def slack_diagnostico():
    """Diagnostico dos avisos via Slack (canais, envio, alerta de desperdicio).

    Resolve o caso comum: "recebi WhatsApp mas nao vi no Slack".
    Mostra config + permite disparar alerta na hora pra ler o motivo real.
    """
    from flask import current_app

    from app.services import desperdicio_alerta, slack

    cfg = current_app.config
    canais = [
        ('Resumo diario (04:00)', 'SLACK_CANAL_RESUMO_DIARIO',
         (cfg.get('SLACK_CANAL_RESUMO_DIARIO') or '').strip()),
        ('Lembretes pedido amanha (9/12/16/19h)', 'SLACK_CANAL_PEDIDOS',
         (cfg.get('SLACK_CANAL_PEDIDOS') or '').strip()),
        ('Alerta desperdicio (20:10/15/20/25)', 'SLACK_CANAL_COPILOT',
         (cfg.get('SLACK_CANAL_COPILOT') or '').strip()),
    ]
    info = {
        'bot_token_setado': bool((cfg.get('SLACK_BOT_TOKEN') or '').strip()),
        'signing_setado': bool((cfg.get('SLACK_SIGNING_SECRET') or '').strip()),
        'disponivel': slack.disponivel(),
        'canais': canais,
        'lojas_sem_desperdicio': desperdicio_alerta.lojas_sem_desperdicio(),
        'ultimo_resultado': request.args.get('resultado'),
    }
    return render_template('main/slack_diagnostico.html', info=info)


@main_bp.route('/admin/slack/diagnostico/testar-canal', methods=['POST'])
@owner_required
def slack_diagnostico_testar_canal():
    from flask import flash

    from app.services import slack

    canal = (request.form.get('canal') or '').strip()
    if not canal:
        flash('Canal vazio — configure a env var antes.', 'warning')
        return redirect(url_for('main.slack_diagnostico'))
    res = slack.post_message(
        canal, ':test_tube: Teste de envio do diagnostico Slack.')
    if res.get('ok'):
        msg = f'OK: mensagem postada no canal {canal} (ts={res.get("ts")}).'
        nivel = 'success'
    else:
        msg = f'FALHA ao postar em {canal}: {res.get("erro")}'
        nivel = 'danger'
    flash(msg, nivel)
    return redirect(url_for('main.slack_diagnostico'))


@main_bp.route('/admin/slack/diagnostico/disparar-desperdicio', methods=['POST'])
@owner_required
def slack_diagnostico_disparar_desperdicio():
    """Dispara `alertar_slack_pendentes` na hora e mostra retorno bruto.

    Util pra entender por que o cron 20:10/15/20/25 nao apareceu no canal.
    """
    from flask import flash

    from app.services import desperdicio_alerta

    # claim=False: re-envio DELIBERADO do owner nunca e bloqueado pelo
    # anti-duplicata do cron (que e por tick de minuto).
    res = desperdicio_alerta.alertar_slack_pendentes(claim=False)
    if res.get('enviado'):
        flash(f'Alerta enviado no Slack ({res.get("pendentes")} loja[s] pendente[s]).',
              'success')
    else:
        motivo = res.get('motivo')
        erro = res.get('erro')
        flash(f'NAO enviado. motivo={motivo}'
              + (f' · erro={erro}' if erro else ''),
              'warning' if motivo == 'sem_pendencias' else 'danger')
    return redirect(url_for('main.slack_diagnostico'))


@main_bp.route('/admin/backup/debug-env')
@owner_required
def backup_debug_env():
    """Diagnostico do ambiente — mostra PATH, locais com pg_dump, versao.

    Usado quando backup falha com "pg_dump nao encontrado" pra entender se
    o nixpacks.toml aplicou ou se o binario esta noutro lugar.
    """
    import os as _os
    import shutil
    import subprocess

    info = {
        'PATH': _os.environ.get('PATH', ''),
        'which_pg_dump': shutil.which('pg_dump'),
    }

    # Procura pg_dump em locais comuns
    locais = []
    for caminho in ['/usr/bin', '/usr/local/bin', '/nix/store', '/usr/lib/postgresql']:
        try:
            r = subprocess.run(['bash', '-c', f'ls -la {caminho} 2>/dev/null | grep -i pg_'],
                               capture_output=True, text=True, timeout=5)
            if r.stdout:
                locais.append(f'{caminho}:\n{r.stdout}')
        except Exception as e:  # noqa: BLE001
            locais.append(f'{caminho}: ERRO {e}')

    # Procura recursiva no /nix/store (Nixpacks instala la)
    try:
        r = subprocess.run(['bash', '-c', 'find /nix/store -name pg_dump 2>/dev/null | head -5'],
                           capture_output=True, text=True, timeout=15)
        info['find_nix_pg_dump'] = r.stdout or '(nada encontrado)'
    except Exception as e:  # noqa: BLE001
        info['find_nix_pg_dump'] = f'ERRO: {e}'

    # Tenta executar
    try:
        r = subprocess.run(['pg_dump', '--version'], capture_output=True, text=True, timeout=5)
        info['pg_dump_version'] = r.stdout or r.stderr
    except FileNotFoundError:
        info['pg_dump_version'] = '(nao encontrado no PATH)'
    except Exception as e:  # noqa: BLE001
        info['pg_dump_version'] = f'ERRO: {e}'

    # Diagnostico extra: identifica se imagem eh Dockerfile-based ou Nixpacks
    try:
        r = subprocess.run(['bash', '-c',
                            'ls -la / 2>&1 | head -30; echo ---; '
                            'cat /etc/os-release 2>&1 | head -5; echo ---; '
                            'dpkg -l 2>/dev/null | grep -iE "postgres|libpq" || echo "(sem dpkg ou sem postgres)"'],
                           capture_output=True, text=True, timeout=10)
        info['ambiente'] = r.stdout
    except Exception as e:  # noqa: BLE001
        info['ambiente'] = f'ERRO: {e}'

    info['locais_listagem'] = '\n\n'.join(locais)

    # Onde fotos de entrega DEVERIAM estar indo
    from flask import current_app, jsonify

    from app.models import EntregaFoto
    info['dropbox_pasta_base_config'] = (
        current_app.config.get('DROPBOX_PASTA_BASE') or '(usando default /Apps/Receitas-Entregas)'
    )
    info['dropbox_backup_pasta_config'] = (
        current_app.config.get('DROPBOX_BACKUP_PASTA') or '(usando default /backups-postgres)'
    )
    info['entrega_foto_count'] = EntregaFoto.query.count()
    foto_recente = EntregaFoto.query.order_by(EntregaFoto.id.desc()).first()
    if foto_recente:
        info['entrega_foto_amostra'] = {
            'id': foto_recente.id,
            'storage_path': foto_recente.storage_path,
            'url': foto_recente.url,
            'tirada_em': str(foto_recente.tirada_em),
        }
    else:
        info['entrega_foto_amostra'] = '(sem fotos no banco)'

    # M6 debug: URL de uma receita migrada
    from app.models import Produto, Receita
    r = (Receita.query
         .filter(Receita.imagem_dropbox_url.isnot(None))
         .order_by(Receita.id.desc()).first())
    if r:
        info['receita_amostra'] = {
            'id': r.id, 'nome': r.nome,
            'imagem_dropbox_url': r.imagem_dropbox_url,
            'imagem_storage_path': r.imagem_storage_path,
            'tem_blob': r.imagem_blob is not None,
        }
    else:
        info['receita_amostra'] = '(nenhuma receita migrada)'

    p = (Produto.query
         .filter(Produto.imagem_dropbox_url.isnot(None))
         .order_by(Produto.id.desc()).first())
    if p:
        info['produto_amostra'] = {
            'id': p.id, 'nome': p.nome,
            'imagem_dropbox_url': p.imagem_dropbox_url,
            'imagem_storage_path': p.imagem_storage_path,
            'tem_blob': p.imagem_blob is not None,
        }
    else:
        info['produto_amostra'] = '(nenhum produto migrado)'

    return jsonify(info)


@main_bp.route('/admin/blobs/migrar/<modelo>', methods=['POST'])
@owner_required
def blobs_migrar(modelo):
    """Backfill de BLOBs antigos pra Dropbox (M6). Owner-only.

    Modelos suportados: pedido_item_foto.
    Idempotente. Processa em batches, advisory lock single-worker.
    """
    from flask import flash

    from app.services import blob_migrator

    if modelo == 'pedido_item_foto':
        resultado = blob_migrator.migrar_pedido_item_foto()
    elif modelo == 'foto_recebimento':
        resultado = blob_migrator.migrar_foto_recebimento()
    elif modelo == 'receita':
        resultado = blob_migrator.migrar_receita_imagem()
    elif modelo == 'produto':
        resultado = blob_migrator.migrar_produto_imagem()
    else:
        flash(f'Modelo invalido: {modelo}', 'danger')
        return redirect(url_for('main.debug_schema'))

    if not resultado.get('ok'):
        flash(f'Migracao falhou: {resultado.get("motivo")}', 'danger')
    else:
        msg = (f'Migracao {modelo}: {resultado["migradas"]}/{resultado["total"]} '
               f'migradas, {resultado["erros"]} erros')
        if resultado.get('detalhes'):
            msg += '. Primeiros detalhes: ' + ' | '.join(resultado['detalhes'][:3])
        cat = 'success' if resultado['erros'] == 0 else 'warning'
        flash(msg, cat)
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/blobs/fix-urls-dropbox', methods=['POST'])
@owner_required
def blobs_fix_urls():
    """One-shot: substitui dl=0 por raw=1 em URLs Dropbox ja populadas.

    Bug originalmente em `_converter_para_raw` deixou URLs com formato
    `...?rlkey=X&dl=0&raw=1`. Dropbox prioriza dl=0 e serve HTML preview.
    Esta rota corrige UPDATE direto no banco — sem precisar reupload.
    """
    from flask import flash
    from sqlalchemy import text

    from app.extensions import db as _db

    tabelas = [
        ('pedido_item_foto', 'imagem_url'),
        ('foto_recebimento', 'imagem_url'),
        ('receita', 'imagem_dropbox_url'),
        ('produto', 'imagem_dropbox_url'),
    ]
    # Normalizacao: itera linhas com URL Dropbox e aplica
    # _converter_para_raw (robusto a dl=0, raw=1 duplicado, etc).
    from app.services.dropbox_storage import _converter_para_raw
    resumo = []
    for tabela, coluna in tabelas:
        with _db.engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT id, {coluna} FROM {tabela} "
                f"WHERE {coluna} IS NOT NULL"
            )).fetchall()
            corrigidas = 0
            for row in rows:
                nova_url = _converter_para_raw(row[1])
                if nova_url != row[1]:
                    conn.execute(
                        text(f"UPDATE {tabela} SET {coluna} = :u "
                             f"WHERE id = :i"),
                        {'u': nova_url, 'i': row[0]})
                    corrigidas += 1
            resumo.append(f'{tabela}.{coluna}: {corrigidas}/{len(rows)}')
    flash('URLs Dropbox corrigidas: ' + ' · '.join(resumo), 'success')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/backup/run', methods=['POST'])
@owner_required
def backup_run():
    """Dispara backup manual do Postgres pro Dropbox. Owner-only.

    Uso: pra testar a configuracao e gerar dump on-demand. O job
    automatico roda diariamente as 04:00 BRT via APScheduler.
    """
    from flask import flash

    from app.services import backup as backup_svc

    resultado = backup_svc.executar_backup(forcar=True)
    if resultado['ok']:
        mb = resultado['tamanho'] / 1024 / 1024
        flash(f'Backup OK: {mb:.2f} MB em {resultado["arquivo"]}', 'success')
    else:
        flash(f'Backup falhou: {resultado.get("motivo") or "ver logs"}', 'danger')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/backup/drill')
@owner_required
def backup_drill():
    """Drill de restore do backup (owner-only): prova que o dump do Dropbox
    eh restauravel. Sem parametro = mostra status do ultimo drill.

    ?iniciar=1     baixa o dump mais recente + valida estrutura (pg_restore
                   --list). Rapido (~1 min), nao toca em banco nenhum.
    ?iniciar=full  alem do acima, restaura num banco temporario
                   (drill_restore_tmp), conta linhas de tabelas-chave e dropa.
                   Prova completa. Pode levar varios minutos — acompanhe
                   recarregando esta rota.

    O status fica em arquivo compartilhado (/tmp) — qualquer worker gunicorn
    responde o mesmo estado, e o resultado sobrevive a reinicio de worker.
    """
    from app.services import backup as backup_svc

    iniciar = (request.args.get('iniciar') or '').strip().lower()
    if iniciar in ('1', 'full'):
        out = backup_svc.iniciar_drill(full=(iniciar == 'full'))
        out['status'] = backup_svc.drill_status()
        return jsonify(out), 200
    return jsonify(backup_svc.drill_status()), 200


@main_bp.route('/admin/dropbox/reauth')
@owner_required
def dropbox_reauth():
    """Re-autorizacao OAuth da app Dropbox (owner-only), pra quando os ESCOPOS
    mudam — permissao nova (ex: files.content.read pro drill de restore) NAO
    vale pra refresh token ja emitido; precisa autorizar de novo.

    Fluxo em 2 passos, sem curl:
      1. GET sem parametro: mostra o link de autorizacao do Dropbox. Abra,
         clique em Permitir, copie o codigo exibido.
      2. GET ?code=<codigo>: troca o codigo por um refresh token NOVO e mostra
         o valor pra voce colar em Railway -> Variables -> DROPBOX_REFRESH_TOKEN.
    """
    import requests as _requests
    app_key = (current_app.config.get('DROPBOX_APP_KEY') or '').strip()
    app_secret = (current_app.config.get('DROPBOX_APP_SECRET') or '').strip()
    if not app_key or not app_secret:
        return jsonify(erro='DROPBOX_APP_KEY/SECRET nao configurados no env'), 200

    code = (request.args.get('code') or '').strip()
    if not code:
        url_auth = ('https://www.dropbox.com/oauth2/authorize'
                    f'?client_id={app_key}&response_type=code'
                    '&token_access_type=offline')
        return jsonify(
            passo_1=('Confirme ANTES no App Console que o escopo novo esta '
                     'marcado (Permissions -> files.content.read -> Submit).'),
            passo_2=f'Abra e autorize: {url_auth}',
            passo_3=('Copie o codigo que o Dropbox mostrar e volte aqui com '
                     '?code=<codigo>'),
        ), 200

    r = _requests.post(
        'https://api.dropbox.com/oauth2/token',
        data={'grant_type': 'authorization_code', 'code': code},
        auth=(app_key, app_secret),
        timeout=15,
    )
    if r.status_code != 200:
        return jsonify(erro=f'troca do codigo falhou: HTTP {r.status_code}',
                       detalhe=(r.text or '')[:300],
                       dica=('Codigo expira em minutos e so vale 1 vez — '
                             'gere outro no link do passo 2.')), 200
    body = r.json()
    novo_refresh = body.get('refresh_token') or ''
    if not novo_refresh:
        return jsonify(erro='resposta sem refresh_token',
                       detalhe=str(body)[:300]), 200
    return jsonify(
        ok=True,
        refresh_token=novo_refresh,
        proximo_passo=('Railway -> servico web -> Variables -> substitua '
                       'DROPBOX_REFRESH_TOKEN por este valor e salve. Apos o '
                       'redeploy, rode o drill de novo: '
                       '/admin/backup/drill?iniciar=full'),
    ), 200


@main_bp.route('/admin/teste-aviso-recebimento')
@owner_required
def teste_aviso_recebimento():
    """Teste end-to-end do aviso de pedido recebido (owner-only).

    Sem parametro: cria um PedidoLoja de TESTE (sem itens — nao toca
    estoque), sobe 2 fotos geradas pra /recebimento/<id>/ no Dropbox, marca
    'entregue' e dispara o aviso pro WhatsApp do dono com o link da pasta.

    ?limpar=<id>: apaga o pedido de teste (so se tiver o marcador de teste
    na observacao — pedido real e recusado), as fotos do banco e os
    arquivos do Dropbox.
    """
    import io
    import time as _time

    from app.models import FotoRecebimento, Loja, PedidoLoja
    from app.services import dropbox_storage, pedidos_notificacao

    MARCADOR = '[PEDIDO-TESTE-AVISO]'

    limpar_id = request.args.get('limpar')
    if limpar_id:
        p = PedidoLoja.query.get(int(limpar_id))
        if not p:
            return jsonify(erro='pedido nao encontrado'), 200
        if MARCADOR not in (p.observacao or ''):
            return jsonify(erro='esse pedido NAO eh de teste — recusado'), 200
        for f in list(p.fotos or []):
            if f.imagem_storage_path:
                dropbox_storage.deletar(f.imagem_storage_path)
        db.session.delete(p)   # cascade apaga FotoRecebimento
        db.session.commit()
        return jsonify(ok=True, apagado=int(limpar_id)), 200

    loja = Loja.query.filter_by(ativa=True).first()
    if not loja:
        return jsonify(erro='nenhuma loja ativa'), 200

    p = PedidoLoja(loja_id=loja.id, status='entregue',
                   observacao=f'{MARCADOR} criado via /admin/teste-aviso-recebimento',
                   criado_por=current_user.id)
    db.session.add(p)
    db.session.flush()

    # 2 fotos geradas (quadrados coloridos) pra pasta ter conteudo real
    fotos_ok = 0
    if dropbox_storage.disponivel():
        from PIL import Image
        for cor in ((220, 60, 90), (60, 140, 220)):
            img = Image.new('RGB', (320, 320), cor)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            try:
                info = dropbox_storage.upload_publico(
                    buf.getvalue(),
                    f'/recebimento/{p.id}/teste_{int(_time.time() * 1000)}.jpg',
                    mode='add', autorename=True)
                db.session.add(FotoRecebimento(
                    pedido_id=p.id, imagem_url=info['url'],
                    imagem_storage_path=info['storage_path'],
                    mimetype='image/jpeg', enviada_por=current_user.id))
                fotos_ok += 1
            except RuntimeError:
                current_app.logger.exception('teste-aviso: upload falhou')
    db.session.commit()

    pedidos_notificacao.notificar_pedido_recebido(p)

    return jsonify(
        ok=True,
        pedido_id=p.id,
        loja=loja.nome,
        fotos_enviadas=fotos_ok,
        confira='o aviso deve ter chegado no seu WhatsApp',
        limpar_depois=f'/admin/teste-aviso-recebimento?limpar={p.id}',
    ), 200


@main_bp.route('/admin/saude')
@owner_required
def saude_negocio_admin():
    """Radar de saude do negocio (owner-only): contas a pagar + receitas.

    O mesmo conteudo chega as 07:30 no WhatsApp do dono (job
    `zapi-digest-saude`; DIGEST_SAUDE=0 desliga). Aqui e a versao on-demand
    com os detalhes completos (listas, nao so contagens).
    ?enviar=1 dispara o digest no WhatsApp agora (teste)."""
    from app.services import saude_negocio

    out = {
        'contas': saude_negocio.resumo_contas(),
        'receitas': saude_negocio.resumo_receitas(),
    }
    if request.args.get('enviar') == '1':
        out['envio'] = saude_negocio.enviar_digest_saude()
    return jsonify(out), 200


@main_bp.route('/admin/debug-handshake-bypass')
@owner_required
def debug_handshake_bypass():
    """Owner-only: pedidos que avancaram (em_transporte/entregue) SEM o
    handshake de QR — responde "alguem pulou o QR?".

    Bypasses LEGITIMOS aparecem identificados: forcar_entrega (admin, gera
    HandshakeAudit proprio) e copilot via Slack (sem HandshakeAudit nenhum).
    ?dias=N (default 30) controla a janela. Atribuir motorista NAO e bypass
    (e o passo anterior ao QR)."""
    from datetime import timedelta as _td

    from app.models import HandshakeAudit, PedidoLoja
    from app.utils import agora as _agora
    dias = request.args.get('dias', 30, type=int)
    corte = _agora() - _td(days=dias)

    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.status.in_(('em_transporte', 'entregue')))
               .filter(PedidoLoja.criado_em >= corte)
               .order_by(PedidoLoja.id.desc()).all())
    ids = [p.id for p in pedidos]
    audits = {}
    if ids:
        for a in HandshakeAudit.query.filter(
                HandshakeAudit.pedido_id.in_(ids)).all():
            audits.setdefault(a.pedido_id, []).append(a)

    suspeitos = []
    com_handshake = 0
    forcados = 0
    for p in pedidos:
        regs = audits.get(p.id, [])
        sucessos = [a for a in regs if a.etapa == 'sucesso']
        forcou = [a for a in regs if a.etapa == 'forcar_entrega']
        if forcou:
            forcados += 1
            suspeitos.append({
                'pedido_id': p.id, 'status': p.status,
                'classificacao': 'forcado_pelo_admin',
                'detalhe': (forcou[0].detalhe or '')[:120],
                'quando': forcou[0].momento.isoformat() if forcou[0].momento else None,
            })
        elif sucessos:
            com_handshake += 1
        else:
            suspeitos.append({
                'pedido_id': p.id, 'status': p.status,
                'classificacao': 'sem_handshake (provavel copilot/Slack)',
                'loja_id': p.loja_id,
                'driver_id': p.driver_id,
                'criado_em': p.criado_em.isoformat() if p.criado_em else None,
            })

    return jsonify(
        janela_dias=dias,
        total_avancados=len(pedidos),
        com_handshake_ok=com_handshake,
        forcados_pelo_admin=forcados,
        sem_handshake=len(suspeitos) - forcados,
        suspeitos=suspeitos[:100],
        dica=('sem_handshake = avancou sem NENHUM scan de QR. Caminho '
              'legitimo: copilot/Slack ("recebi o pedido X"). Se nao foi '
              'copilot, investigue no /audit filtrando o pedido.'),
    ), 200


@main_bp.route('/admin/debug-chapa')
@owner_required
def debug_chapa():
    """Owner-only: raio-X da baixa fracionaria (itens de chapa) no Seru.

    Responde "o desconto de fatias de pao esta funcionando?" com dados reais:
    mapeamentos com fator fracionario, debitos acumulados (fatias aguardando
    fechar 1 pao), orfaos (FK morta) e movimentos fracionarios recentes
    (prova de execucao)."""
    from datetime import timedelta as _td

    from app.models import (
        MovEstoqueLoja,
        Receita,
        SeruDebito,
        VendaMapa,
    )
    from app.utils import agora as _agora

    out = {}

    mapeados = VendaMapa.query.filter(
        VendaMapa.canal == 'seru',
        (VendaMapa.receita_id.isnot(None))
        | (VendaMapa.produto_id.isnot(None))).all()
    com_fator = []
    orfaos = []
    for m in mapeados:
        fator = float(m.fator_quantidade or 1.0)
        alvo = None
        if m.receita_id:
            r = Receita.query.get(m.receita_id)
            alvo = r.nome if r else None
            if r is None:
                orfaos.append({'map_id': m.id, 'seru_nome': m.seru_nome,
                               'problema': f'receita_id={m.receita_id} nao existe'})
        if fator != 1.0:
            com_fator.append({'seru_nome': m.seru_nome, 'fator': fator,
                              'alvo': alvo})
    out['mapeados_total'] = len(mapeados)
    out['com_fator_fracionario'] = sorted(com_fator,
                                          key=lambda x: x['seru_nome'])
    out['orfaos_fk_morta'] = orfaos

    debitos = SeruDebito.query.filter(
        SeruDebito.fracao_pendente > 0.001).all()
    out['debitos_acumulados'] = [
        {'loja_id': d.loja_id, 'map_id': d.seru_produto_map_id,
         'fracao_pendente': round(float(d.fracao_pendente or 0), 3)}
        for d in debitos]

    corte = _agora() - _td(days=7)
    out['movs_fracionarios_7d'] = (
        MovEstoqueLoja.query
        .filter(MovEstoqueLoja.tipo == 'venda_seru')
        .filter(MovEstoqueLoja.referencia.like('%(fator%'))
        .filter(MovEstoqueLoja.data >= corte).count())
    out['interpretacao'] = (
        'com_fator_fracionario vazio = NENHUM item de chapa configurado '
        '(va em /pdv/mapeamentos e ajuste o fator de cada item de chapa). '
        'movs_fracionarios_7d > 0 = o desconto ESTA rodando. '
        'debitos_acumulados = fatias ja vendidas aguardando fechar 1 pao '
        'inteiro pra baixar do estoque.')
    return jsonify(out), 200


@main_bp.route('/admin/retencao')
@owner_required
def retencao_admin():
    """Retencao de dados (owner-only). Sem parametro = DRY-RUN: mostra o que
    SERIA apagado por alvo, sem tocar em nada. ?executar=1 apaga de verdade.

    O ciclo automatico roda no cron diario apos o backup OK (RETENCAO_AUTO=0
    desliga). Prazos via env: RETENCAO_LOGS_DIAS(365) /
    RETENCAO_CONVERSAS_DIAS(180) / RETENCAO_EVENTOS_DIAS(7) /
    RETENCAO_BACKUPS_DIAS(90).
    """
    from app.services import retencao

    executar = request.args.get('executar') == '1'
    rel = retencao.executar_limpeza(dry_run=not executar)
    rel['prazos_dias'] = {
        'logs': current_app.config['RETENCAO_LOGS_DIAS'],
        'conversas': current_app.config['RETENCAO_CONVERSAS_DIAS'],
        'eventos': current_app.config['RETENCAO_EVENTOS_DIAS'],
        'backups': current_app.config['RETENCAO_BACKUPS_DIAS'],
    }
    rel['auto_diaria'] = bool(current_app.config.get('RETENCAO_AUTO', True))
    return jsonify(rel), 200


@main_bp.route('/admin/acerto-despacho')
@owner_required
def acerto_despacho():
    """Acerto de DESPACHO DIRETO da indústria (owner-only, 08/08/2026 —
    Dia dos Pais; decisão do dono: "ajuste cirúrgico por pedido").

    Pedidos do site do dia informado saíram DIRETO da indústria: o acerto
    estorna da loja de origem o que cada pedido baixou no pagamento e
    debita a indústria pela composição despachada. Sem `?executar=1` =
    DRY-RUN (plano completo, nada escrito). Idempotente por pedido —
    rodar de novo só pega pedidos novos (ex.: cancelamentos tardios já
    ficam de fora sozinhos). RODAR SÓ DEPOIS do despacho físico.
    """
    from datetime import date as _date

    from app.services import acerto_despacho as svc
    data_str = (request.args.get('data') or '').strip()
    if not data_str:
        return jsonify(ok=False,
                       erro='informe ?data=YYYY-MM-DD (o dia do despacho)'), 400
    try:
        alvo = _date.fromisoformat(data_str)
    except ValueError:
        return jsonify(ok=False, erro='data inválida'), 400
    executar = request.args.get('executar') == '1'
    # `entregas_concluidas=1` (dono 09/08/2026, noite do Dia dos Pais:
    # "o 7 podemos fazer hoje?"): permite executar NO PRÓPRIO dia quando o
    # dono afirma que o despacho físico terminou — as guardas pós-acerto
    # (`_acertado_no_despacho`) já cobrem cancelamento tardio sem crédito
    # em dobro, e o acerto é idempotente por pedido (pedido pago DEPOIS da
    # execução entra numa re-rodada). Data FUTURA segue recusada sempre.
    hoje_ok = request.args.get('entregas_concluidas') == '1'
    if executar and (alvo > hoje_brt()
                     or (alvo == hoje_brt() and not hoje_ok)):
        # Trava de sequência: o acerto pressupõe mercadoria JÁ despachada.
        return jsonify(ok=False, erro='o acerto só roda DEPOIS do dia do '
                       'despacho — hoje ainda é %s. Se as entregas do dia '
                       'JÁ terminaram, adicione &entregas_concluidas=1.'
                       % hoje_brt().isoformat()), 400
    plano = svc.acertar(alvo, executar=executar,
                        usuario_id=current_user.id)
    return jsonify(ok=True, **plano)


@main_bp.route('/admin/arquivadas-saldo')
@owner_required
def arquivadas_saldo():
    """Saldo fisico preso em item ARQUIVADO/INATIVO (owner-only, 19/07/2026).

    Depois da varredura "arquivado fora de fluxo ativo", receita arquivada
    com saldo vivo ficou sem caminho de escoamento (o balanco nao casa mais
    com ela). Caso real na criacao: 5 receitas arquivadas somando ~204 mil
    un de ledger morto (Croissant Nutella com 99.971 na Anesio etc.),
    poluindo toda soma de estoque. Dono: "pode arquivar tudo".

    Sem parametro = DRY-RUN (lista o que seria zerado). ?executar=1 zera:
    quantidade -> 0 + movimento 'ajuste' com referencia rastreavel (o
    historico da linha conta a historia; nada e apagado). Linha com
    reserva de site (quantidade_reservada > 0) e PULADA com aviso —
    zerar por baixo de reserva quebraria a consistencia do checkout.
    """
    from app.models import (
        EstoqueLoja,
        EstoqueProducao,
        MovEstoqueLoja,
        MovEstoqueProducao,
        Produto,
        Receita,
    )

    executar = request.args.get('executar') == '1'
    ref = 'Zerar saldo de item arquivado (limpeza owner /admin/arquivadas-saldo)'
    linhas, pulados = [], []
    zerados = 0

    def _morto(el):
        if el.receita_id and el.receita and el.receita.arquivada_em:
            return 'receita arquivada', el.receita.nome
        if el.produto_id and el.produto and not el.produto.ativo:
            return 'produto inativo', el.produto.nome
        return None, None

    for el in (EstoqueLoja.query
               .filter(EstoqueLoja.quantidade != 0)
               .outerjoin(Receita, EstoqueLoja.receita_id == Receita.id)
               .outerjoin(Produto, EstoqueLoja.produto_id == Produto.id)
               .filter(db.or_(Receita.arquivada_em.isnot(None),
                              Produto.ativo.is_(False))).all()):
        motivo, nome = _morto(el)
        if not motivo:
            continue
        info = {'onde': f'loja: {el.loja.nome if el.loja else el.loja_id}',
                'item': nome, 'motivo': motivo,
                'quantidade': el.quantidade}
        if (el.quantidade_reservada or 0) > 0:
            info['aviso'] = ('PULADO — tem reserva de site '
                             f'({el.quantidade_reservada})')
            pulados.append(info)
            continue
        linhas.append(info)
        if executar:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='ajuste',
                quantidade=el.quantidade, referencia=ref,
                usuario_id=current_user.id))
            el.quantidade = 0
            zerados += 1

    for ep in (EstoqueProducao.query
               .filter(EstoqueProducao.quantidade != 0)
               .outerjoin(Receita, EstoqueProducao.receita_id == Receita.id)
               .outerjoin(Produto, EstoqueProducao.produto_id == Produto.id)
               .filter(db.or_(Receita.arquivada_em.isnot(None),
                              Produto.ativo.is_(False))).all()):
        motivo, nome = _morto(ep)
        if not motivo:
            continue
        linhas.append({'onde': 'industria', 'item': nome, 'motivo': motivo,
                       'quantidade': ep.quantidade})
        if executar:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='ajuste',
                quantidade=ep.quantidade, referencia=ref,
                usuario_id=current_user.id))
            ep.quantidade = 0
            zerados += 1

    if executar:
        db.session.commit()
    return jsonify({
        'ok': True,
        'dry_run': not executar,
        'linhas': linhas,
        'pulados_com_reserva': pulados,
        'total_linhas': len(linhas),
        'total_unidades': sum(li['quantidade'] for li in linhas),
        'zerados': zerados,
        'como_executar': (None if executar
                          else 'repita com ?executar=1 pra zerar'),
    }), 200


@main_bp.route('/admin/db-vacuum')
@owner_required
def db_vacuum():
    """VACUUM FULL numa tabela de LOG (17/07/2026, volume Railway a 75%).

    A retenção (DELETE) devolve o espaço pra REUSO interno do Postgres, mas
    só o VACUUM FULL devolve ao DISCO — o número que o Railway mede.
    Allowlist FECHADA de tabelas de log (nunca negócio); trava a tabela por
    alguns segundos, por isso é gesto manual do dono, não cron. Sem
    ?executar=1 só mostra o tamanho atual (dry-run, padrão da retenção)."""
    from sqlalchemy import text as _text
    permitidas = ('slack_acao_pendente', 'audit_log', 'vigia_veredito',
                  'chatbot_conversa')
    if db.engine.dialect.name != 'postgresql':
        return jsonify(ok=False, erro='so em Postgres (prod)'), 400
    tabela = (request.args.get('tabela') or '').strip()
    if tabela not in permitidas:
        return jsonify(ok=False, permitidas=list(permitidas),
                       erro='passe ?tabela= um dos alvos permitidos'), 400

    def _tam(conn):
        return conn.execute(
            _text('SELECT pg_total_relation_size(CAST(:t AS regclass))'),
            {'t': tabela}).scalar()
    with db.engine.connect() as conn:
        antes = _tam(conn)
    if request.args.get('executar') != '1':
        return jsonify(ok=True, tabela=tabela, dry_run=True,
                       tamanho_mb=round(antes / 1048576, 1),
                       dica='?executar=1 roda o VACUUM FULL '
                            '(trava a tabela por alguns segundos)')
    # VACUUM nao roda dentro de transacao — conexao AUTOCOMMIT dedicada.
    # O nome da tabela vem da allowlist acima, nunca do usuario.
    with db.engine.connect().execution_options(
            isolation_level='AUTOCOMMIT') as conn:
        conn.execute(_text(f'VACUUM FULL {tabela}'))
        depois = _tam(conn)
    return jsonify(ok=True, tabela=tabela,
                   antes_mb=round(antes / 1048576, 1),
                   depois_mb=round(depois / 1048576, 1))


def _saldo_lalamove_json():
    from app.models import LalamoveSaldo
    s = db.session.get(LalamoveSaldo, 1)
    if not s:
        return ('ainda sem evento de carteira — chega no primeiro '
                'debito/recarga apos ativar o webhook')
    return {'valor': str(s.valor) if s.valor is not None else None,
            'moeda': s.moeda,
            'atualizado_em': s.atualizado_em.isoformat(sep=' ',
                                                       timespec='seconds')
            if s.atualizado_em else None,
            'payload_cru': (s.payload_json or '')[:400]}


@main_bp.route('/admin/debug-lalamove')
@owner_required
def debug_lalamove():
    """Diagnóstico das credenciais Lalamove (owner-only). Mostra prefixos
    (nunca a chave inteira) e bate num endpoint autenticado neutro
    (GET /v3/cities): 200 = chave+assinatura OK; 401 = credencial/conta;
    outro = corpo do erro pra leitura."""
    from app.services import lalamove
    key = lalamove._cfg('LALAMOVE_API_KEY') or ''
    secret = lalamove._cfg('LALAMOVE_API_SECRET') or ''
    from app.blueprints.lalamove.routes import ultimo_hit
    out = {
        'configurado': lalamove.disponivel(),
        # ultimo acesso registrado no /lalamove/webhook deste container —
        # diz se o probe do portal chegou ao servidor ou morreu no caminho.
        'webhook_ultimo_hit': (ultimo_hit() or
                               'nenhum acesso DESDE O ULTIMO DEPLOY (o '
                               'rastro zera a cada deploy) — abra '
                               '/lalamove/webhook no navegador e recarregue '
                               'aqui pra testar o caminho de entrada'),
        'saldo_carteira': _saldo_lalamove_json(),
        'key_prefixo': key[:8] + '...' if key else None,
        'key_tamanho': len(key),
        'secret_prefixo': secret[:8] + '...' if secret else None,
        'secret_tamanho': len(secret),
        # espaco/quebra de linha copiado junto e causa classica de 401
        'key_tem_espaco': key != key.strip(),
        'secret_tem_espaco': secret != secret.strip(),
        'base_url': lalamove._base_url(),
        'market': lalamove._cfg('LALAMOVE_MARKET', 'BR') or 'BR',
        'origem_latlng_env': bool(lalamove._cfg('LALAMOVE_ORIGEM_LATLNG')),
    }
    if not out['configurado']:
        out['erro'] = 'LALAMOVE_API_KEY/SECRET ausentes'
        return jsonify(out), 200
    try:
        status, corpo = lalamove._request('GET', '/v3/cities')
        out['teste_cities_status'] = status
        out['teste_cities_ok'] = status == 200
        if status == 200:
            dados = corpo.get('data') or []
            out['cidades'] = [c.get('locode') or c.get('id') for c in dados][:10]
            out['conclusao'] = ('Credenciais e assinatura OK. Se a cotação '
                                'ainda falhar, o problema é no payload — me '
                                'mande este JSON.')
        else:
            out['teste_cities_corpo'] = str(corpo)[:600]
            out['conclusao'] = ('401/erro também no endpoint neutro = chave/'
                                'secret não conferem ou conta sem produção '
                                'ativa (Wallet/aprovação no portal). Não é '
                                'problema do payload de cotação.')
    except Exception as exc:  # noqa: BLE001
        out['erro'] = f'{type(exc).__name__}: {exc}'
    return jsonify(out), 200


@main_bp.route('/admin/debug-sentry')
@owner_required
def debug_sentry():
    """Status do monitoramento de erros (owner-only). ?testar=1 manda um
    evento de teste pro Sentry — confira se chegou no painel sentry.io."""
    import os as _os
    dsn = (_os.environ.get('SENTRY_DSN') or '').strip()
    out = {
        'dsn_configurado': bool(dsn),
        'ambiente': _os.environ.get('SENTRY_ENV', 'production'),
    }
    try:
        import sentry_sdk
        out['sdk_instalado'] = True
        client = sentry_sdk.Hub.current.client
        out['sdk_ativo'] = client is not None
        if request.args.get('testar') == '1':
            if not out['sdk_ativo']:
                out['teste'] = ('NAO enviado: SDK inativo. Configure SENTRY_DSN '
                                'no Railway e redeploye.')
            else:
                event_id = sentry_sdk.capture_message(
                    'Teste manual via /admin/debug-sentry', level='warning')
                out['teste'] = f'enviado (event_id={event_id}) — confira no sentry.io'
    except ImportError:
        out['sdk_instalado'] = False
    if not dsn:
        out['como_ativar'] = (
            '1) Crie projeto Flask gratis em sentry.io; 2) copie o DSN; '
            '3) Railway -> Variables -> SENTRY_DSN=<dsn>; 4) aguarde redeploy; '
            '5) volte aqui com ?testar=1.')
    return jsonify(out), 200


@main_bp.route('/admin/vigia-site')
@owner_required
def vigia_site():
    """Vigia do SITE sob demanda (owner-only) — mesmos checks do cron de 2h
    (canários de frete, catálogo, agenda). Criado em 05/07/2026 no incidente
    do frete. `?alertar=1` roda o fluxo completo com anti-spam/WhatsApp;
    sem parâmetro, só mostra o resultado (não mexe no estado do vigia)."""
    from app.services import site_vigia

    if request.args.get('alertar') == '1':
        return jsonify(site_vigia.vigiar()), 200
    return jsonify(site_vigia.rodar_checks()), 200


@main_bp.route('/admin/vigia-uso-ia')
@owner_required
def vigia_uso_ia():
    """Vigia de CUSTO de IA sob demanda (owner-only) — mesmo check do cron
    de 1h (gasto de hoje em UsoIA × teto USO_IA_TETO_DIA_USD). Criado em
    11/07/2026: o /admin/uso-ia é passivo; este vigia é quem avisa.
    `?alertar=1` roda o fluxo completo com anti-spam/WhatsApp; sem
    parâmetro, só mostra o resultado (não mexe no estado do vigia)."""
    from app.services import uso_ia_vigia

    if request.args.get('alertar') == '1':
        return jsonify(uso_ia_vigia.vigiar()), 200
    return jsonify(uso_ia_vigia.rodar_checks()), 200


@main_bp.route('/admin/vigia-estorno-pendente')
@owner_required
def vigia_estorno_pendente():
    """Vendas canceladas que NAO devolveram o estoque (owner-only).

    O estorno de pedido ja processado dispara por `canceledAt`; cancelamento
    feito so pelo `status` nunca fecha essa condicao e o estoque fica
    baixado pra sempre (4 casos reais em 22-24/07/2026, 7 itens). Decisao do
    dono 26/07/2026: ALERTAR, sem mexer no gatilho.

    Read-only sempre: usa a MESMA regra do sync (`e_estorno_pendente`) sem
    baixar nem estornar nada. Sem parametro lista o que ha agora + o estado
    de dedup; `?alertar=1` dispara o WhatsApp das novas."""
    from datetime import timedelta as _td

    from app.services import estorno_pendente_vigia
    from app.utils import hoje as _hoje

    hoje_d = _hoje()
    di, df = hoje_d - _td(days=2), hoje_d
    try:
        pendentes = estorno_pendente_vigia.detectar(di, df)
    except Exception as e:  # noqa: BLE001 — API fora nao vira 500
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:200]}'), 502

    def _detalhar(c):
        itens, fracs = estorno_pendente_vigia.itens_baixados(c['id'])
        return dict(c, saiu_do_estoque=[{'item': n, 'qtd': q}
                                        for n, q in itens],
                    baixas_fracionarias=fracs)

    detalhe = [_detalhar(c) for c in pendentes]
    janela = [di.isoformat(), df.isoformat()]
    if request.args.get('alertar') == '1':
        # Corrida com o ciclo do cron (mesmo fix do vigia irmao): sem trava,
        # os dois leem o estado velho, mandam a MESMA mensagem e o ultimo
        # `_gravar_estado` apaga os ids marcados pelo outro. Usa o try-lock
        # do PROPRIO sync (que envolve o vigia no cron); ocupado = o ciclo
        # esta rodando agora e ja cobre. SQLite (dev/teste) roda direto.
        from sqlalchemy import text as _text

        from app.extensions import db as _db
        from app.services.seru_cron import LOCK_KEY as _LOCK_SYNC
        uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        if 'postgresql' in uri:
            conn = _db.engine.connect()
            try:
                got = conn.execute(_text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': _LOCK_SYNC}).scalar()
                if not got:
                    return jsonify(
                        ok=False,
                        erro='ciclo do sync Seru rodando agora (o vigia dele '
                             'ja cobre) — tente em ~1 min'), 409
                try:
                    return jsonify(
                        ok=True, janela=janela, pendentes=detalhe,
                        resultado=estorno_pendente_vigia.alertar(
                            pendentes)), 200
                finally:
                    try:
                        conn.execute(_text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': _LOCK_SYNC})
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                conn.close()
        return jsonify(ok=True, janela=janela, pendentes=detalhe,
                       resultado=estorno_pendente_vigia.alertar(
                           pendentes)), 200
    return jsonify(ok=True, dry_run=True, janela=janela,
                   total=len(detalhe), pendentes=detalhe,
                   estado=estorno_pendente_vigia.estado_dedup()), 200


@main_bp.route('/admin/vigia-venda-sem-item')
@owner_required
def vigia_venda_sem_item():
    """Vigia de venda SEM itens sob demanda (owner-only) — mesmo check do
    ciclo de 15min do sync Seru (caso Nebraska 17/07/2026: 23 cobranças
    "PDV Fácil" só-valor, R$ 7.028,50, todas sem NF). Sem parâmetro: DRY-RUN
    — lista as cobranças de ontem+hoje e o estado de dedup, sem WhatsApp e
    sem marcar nada. `?alertar=1` roda o fluxo completo (alerta as novas e
    marca)."""
    from datetime import timedelta as _td

    from app.services import venda_sem_item_vigia
    from app.utils import hoje as _hoje

    if request.args.get('alertar') == '1':
        # Corrida com o ciclo do cron (achado de revisão): sem trava, os
        # dois leriam o estado velho e mandariam a MESMA mensagem. Usa o
        # try-lock do PRÓPRIO sync (que envolve o vigia no cron) — ocupado
        # = ciclo rodando agora, o alerta manual seria redundante mesmo.
        # SQLite (dev/teste, 1 processo) roda direto.
        from sqlalchemy import text as _text

        from app.extensions import db as _db
        from app.services.seru_cron import LOCK_KEY as _LOCK_SYNC
        uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        if 'postgresql' in uri:
            conn = _db.engine.connect()
            try:
                got = conn.execute(_text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': _LOCK_SYNC}).scalar()
                if not got:
                    return jsonify(
                        ok=False,
                        erro='ciclo do sync Seru rodando agora (o vigia '
                             'dele já cobre) — tente em ~1 min'), 409
                try:
                    return jsonify(venda_sem_item_vigia.vigiar()), 200
                finally:
                    try:
                        conn.execute(_text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': _LOCK_SYNC})
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                conn.close()
        return jsonify(venda_sem_item_vigia.vigiar()), 200
    hoje_d = _hoje()
    janela = [hoje_d - _td(days=1), hoje_d]
    try:
        cobrancas = venda_sem_item_vigia.cobrancas_sem_itens(
            janela[0], janela[-1])
    except Exception as e:  # noqa: BLE001 — dry-run mostra o erro cru
        return jsonify(ok=False,
                       erro=f'{type(e).__name__}: {str(e)[:200]}'), 502
    estado = venda_sem_item_vigia.estado_dedup(janela)
    ja = {i for ids in estado['ids'].values() for i in ids}
    return jsonify(ok=True,
                   cobrancas=cobrancas,
                   ja_alertadas=sum(1 for c in cobrancas if c['id'] in ja),
                   novas=sum(1 for c in cobrancas if c['id'] not in ja),
                   ultimo_envio=estado.get('ultimo_envio'),
                   envios_hoje=estado['envios'].get(hoje_d.isoformat(), 0),
                   piso_valor=float(venda_sem_item_vigia.min_valor()))


@main_bp.route('/admin/debug-chatwoot')
@owner_required
def debug_chatwoot():
    """Diagnostico do Chatwoot rodando DO SERVIDOR de prod (owner-only).

    Criado em 12/06/2026 durante incidente (WhatsApp "Falha ao enviar" +
    IG "400 Session Invalid" + app "unexpected error"). Distingue em uma
    chamada: hospedagem do Chatwoot fora x token nosso invalido x canais
    Meta desconectados — cada um tem dono e correcao diferentes.

    ?conversa=<id>: alem do diagnostico, busca a conversa e o que falhou
    (erro bruto da Meta). Numero da conversa = o #NNN no topo do
    Chatwoot."""
    from app.services import chatwoot
    out = chatwoot.diagnostico()
    conv = (request.args.get('conversa') or '').strip()
    if conv.isdigit():
        cid = int(conv)
        out['erros_da_conversa_' + conv] = chatwoot.erros_de_envio(cid)
        out['historico_da_conversa_' + conv] = (
            chatwoot.buscar_historico(cid, limite=40))
    return jsonify(out), 200


@main_bp.route('/admin/debug-omada')
@owner_required
def debug_omada():
    """Diagnóstico da Open API do Omada (portal Wi-Fi — fase 2, 12/07/2026).

    Sem parâmetro: mostra a presença das envs OMADA_* (nunca o valor) e
    tenta obter o token OAuth na nuvem TP-Link. Com
    ?autorizar_mac=<MAC do celular>: autoriza o aparelho por 60 min —
    usar com um celular conectado no O_Pao_Clientes pra validar o
    enforcement de ponta a ponta antes de ligar o portal pros clientes."""
    from app.services import omada
    envs = {}
    for k in ('OMADA_API_URL', 'OMADA_CLIENT_ID', 'OMADA_CLIENT_SECRET',
              'OMADA_OMADAC_ID', 'OMADA_SITE_ID'):
        v = (current_app.config.get(k) or '').strip()
        envs[k] = {'presente': bool(v), 'tamanho': len(v)}
    out = {'envs': envs, 'configurado': omada.disponivel()}
    # As 4 envs do token bastam pra testar a nuvem e LISTAR os sites —
    # o OMADA_SITE_ID pode ser copiado da própria resposta.
    if not all(envs[k]['presente'] for k in
               ('OMADA_API_URL', 'OMADA_CLIENT_ID', 'OMADA_CLIENT_SECRET',
                'OMADA_OMADAC_ID')):
        out['conclusao'] = (
            'Faltam envs OMADA_* no Railway — gerar as credenciais no '
            'Omada (Settings → Platform Integration → Open API) e setar '
            'ao menos URL, client_id, client_secret e omadac_id.')
        return jsonify(out), 200
    try:
        omada._token()
        out['token'] = 'ok'
    except Exception as exc:  # noqa: BLE001 — diagnóstico mostra o erro cru
        out['token'] = f'FALHOU: {str(exc)[:300]}'
        out['conclusao'] = (
            'Credenciais/URL erradas ou controlador sem Cloud Access — '
            'conferir OMADA_API_URL (endereço da interface mostrado na '
            'tela do Open API) e o par client_id/client_secret.')
        return jsonify(out), 200
    try:
        out['sites'] = omada.listar_sites()
    except Exception as exc:  # noqa: BLE001 — diagnóstico mostra o erro cru
        out['sites_erro'] = str(exc)[:300]
    if not out['configurado']:
        out['conclusao'] = (
            'Token OK. Falta o OMADA_SITE_ID — copie o "id" do site '
            'Ribeiro do Vale na lista `sites` desta resposta.')
        return jsonify(out), 200
    mac = (request.args.get('autorizar_mac') or '').strip()
    if mac:
        out['autorizacao_teste'] = omada.autorizar_cliente(mac, minutos=60)
    out.setdefault('conclusao', (
        'Token OK — Open API acessível. Pra validar de ponta a ponta: '
        'conectar um celular no O_Pao_Clientes e chamar '
        '?autorizar_mac=<MAC dele>.'))
    return jsonify(out), 200


@main_bp.route('/admin/wifi-vouchers', methods=['GET', 'POST'])
@owner_required
def wifi_vouchers():
    """Estoque de vouchers do portal Wi-Fi (trava dura sem API, 12/07/2026).

    O OC200 não fala com a Open API da nuvem (ver CLAUDE.md), então o
    portal do controlador roda no modo Voucher: o dono gera o lote no
    Hotspot Manager do Omada, exporta e sobe aqui; o WhatsApp entrega um
    código por cadastro validado (`wifi_portal.alocar_voucher`)."""
    from app.models import WifiVoucher
    from app.services import wifi_portal as wifi_svc
    resultado = None
    if request.method == 'POST':
        texto = (request.form.get('vouchers') or '').strip()
        lote = (request.form.get('lote') or '').strip()
        arq = request.files.get('arquivo')
        if arq and arq.filename:
            texto = arq.read().decode('utf-8', errors='replace')
            lote = lote or arq.filename
        if texto.strip():
            imp, dup, ign = wifi_svc.importar_vouchers(texto, lote)
            resultado = {'importados': imp, 'duplicados': dup,
                         'ignorados': ign}
        else:
            resultado = {'erro': 'Nenhum arquivo ou código enviado.'}
    livres = wifi_svc.vouchers_restantes()
    usados = WifiVoucher.query.filter(
        WifiVoucher.usado_em.isnot(None)).count()
    ultimos = (WifiVoucher.query
               .filter(WifiVoucher.usado_em.isnot(None))
               .order_by(WifiVoucher.usado_em.desc()).limit(10).all())
    return render_template(
        'main/wifi_vouchers.html', livres=livres, usados=usados,
        ultimos=ultimos, resultado=resultado,
        aviso_min=current_app.config.get('WIFI_VOUCHER_AVISO_MIN', 50))


_MESES_PT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago',
             'Set', 'Out', 'Nov', 'Dez']


def _clientes_query(q, so_conta, aniv_mes):
    """Query base da lista de clientes do varejo (Cliente). Compartilhada
    entre a tela e o export XLSX pra os dois mostrarem exatamente o mesmo."""
    from app.models import Cliente
    query = Cliente.query
    if q:
        like = f'%{q.lower()}%'
        query = query.filter(db.or_(
            db.func.lower(Cliente.nome).like(like),
            db.func.lower(Cliente.email).like(like),
            Cliente.telefone.like(f'%{q}%')))
    if so_conta:
        query = query.filter(Cliente.senha_hash.isnot(None))
    if aniv_mes:
        query = query.filter(Cliente.aniversario_mes == aniv_mes)
    return query.order_by(Cliente.criado_em.desc())


@main_bp.route('/admin/clientes')
@login_required
@admin_required
def clientes():
    """Lista os clientes do VAREJO (Cliente) — cadastros do site e do portal
    Wi-Fi das lojas (13/07/2026). PII/LGPD: admin+owner. Filtros: busca
    (nome/email/telefone), só-com-conta e aniversariantes do mês. Export
    XLSX pra campanhas."""
    from app.models import Cliente
    q = (request.args.get('q') or '').strip()
    so_conta = request.args.get('conta') == '1'
    aniv_mes = request.args.get('aniv_mes')
    aniv_mes = int(aniv_mes) if (aniv_mes or '').isdigit() else None
    try:
        pagina = max(1, int(request.args.get('p', 1)))
    except (TypeError, ValueError):
        pagina = 1
    por_pagina = 50
    query = _clientes_query(q, so_conta, aniv_mes)
    total = query.count()
    linhas = (query.limit(por_pagina)
              .offset((pagina - 1) * por_pagina).all())
    total_geral = Cliente.query.count()
    com_conta = Cliente.query.filter(Cliente.senha_hash.isnot(None)).count()
    return render_template(
        'main/clientes.html', linhas=linhas, total=total,
        total_geral=total_geral, com_conta=com_conta, q=q,
        so_conta=so_conta, aniv_mes=aniv_mes, pagina=pagina,
        por_pagina=por_pagina, meses=_MESES_PT)


@main_bp.route('/admin/clientes.xlsx')
@login_required
@admin_required
def clientes_xlsx():
    """Export da lista de clientes (mesmos filtros da tela) pra campanhas."""
    import io

    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font
    q = (request.args.get('q') or '').strip()
    so_conta = request.args.get('conta') == '1'
    aniv_mes = request.args.get('aniv_mes')
    aniv_mes = int(aniv_mes) if (aniv_mes or '').isdigit() else None
    wb = Workbook()
    ws = wb.active
    ws.title = 'Clientes'
    cabec = ['Nome', 'E-mail', 'WhatsApp', 'Aniversário', 'Tem conta',
             'Cadastrado em']
    ws.append(cabec)
    for cel in ws[1]:
        cel.font = Font(bold=True)
    for c in _clientes_query(q, so_conta, aniv_mes).all():
        aniv = (f'{c.aniversario_dia:02d}/{c.aniversario_mes:02d}'
                if c.aniversario_dia and c.aniversario_mes else '')
        if aniv and c.nascimento_ano:
            aniv += f'/{c.nascimento_ano}'
        ws.append([c.nome, c.email, c.telefone or '', aniv,
                   'sim' if c.tem_conta else 'não',
                   c.criado_em.strftime('%d/%m/%Y') if c.criado_em else ''])
    for col, larg in zip('ABCDEF', (26, 30, 18, 14, 10, 14)):
        ws.column_dimensions[col].width = larg
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, mimetype=('application/vnd.openxmlformats-officedocument.'
                       'spreadsheetml.sheet'),
        as_attachment=True, download_name='clientes.xlsx')


@main_bp.route('/admin/vnda/contatos')
@login_required
def vnda_contatos():
    """Endereco + contato + DATA DE ENTREGA de uma lista de codes VNDA.

    Criado em 12/06/2026 pro caso operacional 'preciso achar 11 clientes
    pra repor produto estragado'. Aceita ?codes=A,B,C (mais um por linha
    quebrada/espaco/virgula — robusto pra copia-cola do print).

    Acesso: TODOS os usuarios logados (decisao do dono 12/06/2026 — a
    equipe operacional usa pra repor/contatar; mesma classe de PII que
    /entregas/, ja aberta a todos). Era owner-only no nascimento.

    Data de entrega: a OPERACIONAL — se houver OverrideEntrega pro code
    (data alterada no nosso sistema), ela prevalece sobre a do VNDA e
    vem marcada com `data_alterada` + a original.

    Resposta:
      {ok, total, achados, nao_achados,
       clientes: [{code, destinatario, telefone, endereco, data_entrega,
                   data_alterada, data_original, periodo,
                   itens: [{nome,qtd}]}]}.
    """
    import re

    from flask import render_template

    from app.models import OverrideEntrega
    from app.services import vnda
    raw = request.args.get('codes') or request.args.get('q') or ''
    # split robusto: virgula, espaco, quebra de linha, tabs
    codes = [c.strip().upper() for c in re.split(r'[,\s]+', raw) if c.strip()]
    # dedup mantendo ordem
    seen = set()
    codes = [c for c in codes if not (c in seen or seen.add(c))]
    formato = (request.args.get('formato') or '').lower()

    overrides = {}
    if codes:
        for ov in OverrideEntrega.query.filter(
                OverrideEntrega.pedido_code.in_(codes)).all():
            overrides[ov.pedido_code] = ov.data_entrega

    clientes = []
    nao_achados = []
    for code in codes:
        order = vnda.buscar_pedido_completo(code)
        if not order:
            nao_achados.append(code)
            continue
        shipping = vnda.buscar_shipping_address(code)
        client = None
        cid = order.get('client_id')
        if cid:
            try:
                client = vnda.buscar_cliente(cid)
            except Exception:  # noqa: BLE001
                client = None
        p = vnda._normalizar_pedido(order, client_data=client,
                                     shipping_data=shipping)
        data_fmt = p.get('data_entrega_fmt') or ''
        ov = overrides.get(code)
        data_alterada = False
        data_original = None
        if ov:
            data_alterada = True
            data_original = data_fmt
            data_fmt = ov.strftime('%d/%m/%Y')
        clientes.append({
            'code': p.get('code'),
            'destinatario': p.get('destinatario') or p.get('comprador') or '',
            'telefone': p.get('telefone') or '',
            'endereco': p.get('endereco') or '',
            'data_entrega': data_fmt,
            'data_alterada': data_alterada,
            'data_original': data_original,
            'periodo': p.get('periodo') or '',
            'itens': [{'nome': it.get('nome'),
                       'qtd': it.get('quantidade')}
                      for it in (p.get('itens') or [])],
        })
    payload = {
        'ok': True,
        'total': len(codes),
        'achados': len(clientes),
        'nao_achados': nao_achados,
        'clientes': clientes,
    }
    if formato == 'json':
        return jsonify(payload), 200
    # HTML default: tela imprimivel, telefone clicavel, 1 cliente por bloco
    return render_template('main/vnda_contatos.html', dados=payload), 200


@main_bp.route('/admin/zapi/grupos')
@owner_required
def zapi_grupos():
    """Lista os grupos de WhatsApp que o numero do bot participa, com o
    ID pronto pra colar no destino de alertas (owner-only).

    Fluxo (12/06/2026, pedido do dono): criar grupo no WhatsApp →
    adicionar o numero do bot ao grupo → abrir esta rota → copiar o
    `id` (termina em '-group') → colar no Railway em
    CHATBOT_VIGIA_NUMERO (vigia do bot) e CHATWOOT_VIGIA_INFRA_NUMERO
    (vigia de infra) → Apply. O envio pra grupo tem whitelist propria
    que inclui automaticamente esses destinos.

    ?testar=<id-do-grupo>: manda uma mensagem de teste pro grupo na
    hora — fecha o loop da configuracao sem esperar um incidente real.
    Se o grupo nao estiver em nenhuma env de destino, a whitelist
    recusa e o erro aparece no JSON (tambem e diagnostico util)."""
    from app.services import zapi
    out = zapi.listar_grupos()
    testar = (request.args.get('testar') or '').strip()
    if testar:
        out['teste_envio'] = zapi.enviar_texto(
            testar,
            '✅ Teste de alerta — este grupo está configurado pra '
            'receber os avisos do vigia da O Pão.')
    return jsonify(out), 200


@main_bp.route('/admin/debug-bot')
@owner_required
def debug_bot():
    """O que o bot ENXERGA sobre um produto (owner-only).

    Caso real (12/06/2026): vigia alertou 'bot disse esgotado mas tem 872
    un em estoque'. O bot consulta o VNDA (canal de venda do site); o
    vigia compara contra EstoqueLoja (estoque fisico). Fontes diferentes
    explicam o desencontro sem o bot delirar. Esta rota mostra a verdade
    de cada fonte lado a lado pra qualquer produto.

    ?busca=Pain au Chocolat → {vnda: [...], estoque_loja: [{loja, qtd}]}
    """
    from app.services import bot_tools
    busca = (request.args.get('busca') or '').strip()
    if not busca:
        return jsonify({'erro': 'use ?busca=<termo>'}), 400
    out = {'busca': busca}
    try:
        r = bot_tools.consultar_produtos(busca)
        out['vnda'] = r
    except Exception as exc:  # noqa: BLE001
        out['vnda'] = {'erro': f'{type(exc).__name__}: {str(exc)[:200]}'}

    # Estoque interno (mesma fonte que o vigia usa pra comparar)
    from collections import defaultdict

    from app.models import EstoqueLoja
    saldos = defaultdict(lambda: {'qtd_total': 0, 'por_loja': {}})
    try:
        from app.utils import normalizar_busca
        termos = [t for t in normalizar_busca(busca).split() if len(t) > 2]
        for e in EstoqueLoja.query.filter(EstoqueLoja.quantidade > 0).all():
            nome = None
            if e.receita and e.receita.nome:
                nome = e.receita.nome.strip()
            elif e.produto and e.produto.nome:
                nome = e.produto.nome.strip()
            elif (e.nome_pendente or '').strip():
                nome = e.nome_pendente.strip()
            if not nome:
                continue
            nome_norm = normalizar_busca(nome)
            if termos and not all(t in nome_norm for t in termos):
                continue
            loja_nome = (e.loja.nome if e.loja else f'loja_{e.loja_id}')
            saldos[nome]['qtd_total'] += int(e.quantidade or 0)
            saldos[nome]['por_loja'][loja_nome] = (
                saldos[nome]['por_loja'].get(loja_nome, 0)
                + int(e.quantidade or 0))
        out['estoque_loja'] = [
            {'nome': k, **v}
            for k, v in sorted(saldos.items(),
                               key=lambda kv: -kv[1]['qtd_total'])]
    except Exception as exc:  # noqa: BLE001
        out['estoque_loja'] = {'erro': f'{type(exc).__name__}: '
                                       f'{str(exc)[:200]}'}
    return jsonify(out), 200


@main_bp.route('/admin/vigia/diag')
@owner_required
def vigia_diag():
    """Diagnostico do vigia do chatbot: mostra config + ultimos veredictos.

    Owner-only. Pra confirmar que o vigia esta avaliando conversas e que o
    pipeline (Haiku -> Z-API -> WhatsApp do dono) esta funcionando."""
    import os as _os

    from flask import current_app, jsonify

    from app.services import chatbot_vigia
    cfg = current_app.config
    return jsonify({
        'ligado': bool(cfg.get('CHATBOT_VIGIA')),
        'anthropic_api_key_configurada': bool(cfg.get('ANTHROPIC_API_KEY')
                                              or _os.environ.get('ANTHROPIC_API_KEY')),
        'numero_destino': chatbot_vigia._numero_destino(),
        'modelo': chatbot_vigia.MODELO,
        'ultimos_veredictos': chatbot_vigia.ultimos(),
        'tip': ('Pra disparar alerta de teste no seu WhatsApp: '
                'POST /admin/vigia/teste?cenario=estoque '
                '(ou cenario=irritado, ou cenario=silencio)'),
    })


# Rotulos amigaveis pras funcoes registradas em UsoIA.
_USO_IA_LABELS = {
    'bot_atendimento': 'Bot atendimento (Chatwoot)',
    'vigia': 'Vigia do bot',
    'auditor': 'Auditor diario',
    'followup': 'Follow-up pos-handoff',
    'copilot_slack': 'Copilot (Slack)',
    'copilot_whatsapp': 'Copilot (WhatsApp do dono)',
    'ocr_nf': 'OCR Contas a Pagar (NF/boleto)',
    'ocr_cupom': 'OCR cupom',
    'seo': 'Descrições SEO',
}


@main_bp.route('/admin/uso-ia')
@owner_required
def uso_ia_relatorio():
    """Custo das chamadas de IA (Anthropic) por funcao, nos ultimos N dias.

    Owner-only. Fonte: tabela UsoIA (app/services/uso_ia.py registra cada
    chamada). ATENCAO: nao havia registro antes de 25/06/2026 — os numeros so
    existem a partir do deploy desta instrumentacao. Os primeiros 7 dias ficam
    parciais ate acumular a janela inteira."""
    from decimal import Decimal

    from app.services import uso_ia
    dias = request.args.get('dias', 7, type=int)
    if dias < 1:
        dias = 7
    linhas = uso_ia.resumo(dias)
    for ln in linhas:
        ln['label'] = _USO_IA_LABELS.get(ln['funcao'], ln['funcao'])
    total = sum((ln['custo_usd'] for ln in linhas), Decimal(0))
    return render_template('admin/uso_ia.html', linhas=linhas, total=total,
                           dias=dias)


@main_bp.route('/admin/briefing')
@owner_required
def briefing_dono_view():
    """Briefing diário do dono: preview na tela + envio manual (?enviar=1).

    O cron manda o mesmo texto às 07:00 BRT (seru_cron, job briefing-dono).
    Owner-only — é o cockpit pessoal do dono."""
    from app.services import briefing_dono
    dados = briefing_dono.montar()
    texto = briefing_dono.montar_texto(dados)
    if request.args.get('enviar') == '1':
        # Reusa o texto já montado — montar() pode bater na API Seru
        # (garantir_capturado) e remontar dobraria a espera do clique.
        r = briefing_dono.enviar_briefing(texto)
        if r.get('ok'):
            flash('Briefing enviado pro seu WhatsApp.', 'success')
        else:
            flash('Envio falhou: %s' % r.get('erro', r), 'danger')
        return redirect(url_for('main.briefing_dono_view'))
    return render_template('admin/briefing.html', dados=dados, texto=texto)


@main_bp.route('/admin/vendas/cancelados-descontos')
@owner_required
def cancelados_descontos_detalhe():
    """Drill-down do cockpit da home ("abrir" cancelamentos/descontos): lista
    AO VIVO (bate na API Seru) os pedidos cancelados e os com desconto do dia.
    Owner-only (mesmo gate do painel de vendas da home); JSON pro modal.

    A home em si NUNCA bate na API — este endpoint só roda no CLIQUE explícito.
    Seru fora do ar → 502 com aviso amigável (o modal mostra, nada quebra)."""
    from datetime import date as _date

    from app.services import briefing_dono
    hoje_d = hoje_brt()                              # 1x (sem TOCTOU na virada)
    dia_str = (request.args.get('dia') or '').strip()
    if not dia_str:
        dia = hoje_d
    else:
        try:
            dia = _date.fromisoformat(dia_str)
        except ValueError:
            return jsonify(ok=False, erro='Data inválida.'), 400
    # Só hoje ou ontem (o cockpit mostra esses dois; range aberto bateria na
    # API sem limite).
    if dia not in (hoje_d, hoje_d - timedelta(days=1)):
        return jsonify(ok=False, erro='Só hoje ou ontem.'), 400
    try:
        dados = briefing_dono.cancelados_descontos_detalhe(dia)
        return jsonify(ok=True, dia=dia.isoformat(), **dados)
    except Exception as e:  # noqa: BLE001 — API externa; nunca quebrar o modal
        current_app.logger.exception('detalhe cancelados/descontos')
        return jsonify(
            ok=False,
            erro='Não consegui consultar o Seru agora (%s). Tente de novo.'
                 % type(e).__name__), 502


@main_bp.route('/admin/manual')
@login_required
@admin_required
def manual_operacao():
    """Manual de operação vivo (16/07/2026): o que roda sozinho, o que é
    diário/semanal/mensal e de quem é cada gesto — numa página só, com link
    direto em cada tela. Toda feature nova DEVE se registrar aqui (regra de
    processo combinada com o dono)."""
    return render_template('admin/manual.html')


@main_bp.route('/admin/auditor/run', methods=['POST'])
@owner_required
def auditor_run():
    """Roda o auditor proativo do bot AGORA (varre o dia ate este momento) e
    envia o relatorio pro WhatsApp do dono. Owner-only."""
    from flask import flash

    from app.services import chatbot_auditor
    r = chatbot_auditor.auditar_hoje(enviar=True)
    if r.get('enviado'):
        flash('Auditor rodou e enviou o relatorio pro seu WhatsApp.', 'success')
    elif r.get('pulou'):
        flash(f'Auditor pulou: {r["pulou"]}', 'warning')
    elif r.get('erro'):
        flash(f'Auditor falhou: {r["erro"]}', 'danger')
    elif r.get('ok') and not r.get('rel', {}).get('problemas'):
        flash('Auditor rodou: nenhum problema relevante encontrado no periodo.',
              'info')
    else:
        flash(f'Auditor rodou mas nao enviou (sem destino?): {r}', 'warning')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/vigia/teste', methods=['POST'])
@owner_required
def vigia_teste():
    """Dispara o vigia com conversa SINTETICA pra confirmar que tudo funciona
    de ponta a ponta. Owner-only.

    Cenarios:
      estoque  - bot afirma esgotado pra item que tem nas lojas (ALERTA ALTA)
      irritado - cliente irritado com o atendimento (ALERTA ALTA)
      silencio - conversa normal (NAO deve disparar — controle)
    """
    from flask import flash, jsonify, request

    from app.services import chatbot_vigia
    cenario = (request.args.get('cenario') or request.form.get('cenario')
               or 'estoque').strip().lower()
    if cenario not in ('estoque', 'irritado', 'silencio'):
        cenario = 'estoque'
    resultado = chatbot_vigia.disparar_teste(cenario)
    if request.headers.get('Accept', '').startswith('application/json'):
        return jsonify({'cenario': cenario, 'resultado': resultado})
    if resultado.get('enviado'):
        flash(f'Vigia OK: alerta de TESTE ({cenario}) enviado pro seu WhatsApp.',
              'success')
    elif resultado.get('silencio'):
        flash(f'Vigia avaliou ({cenario}) e decidiu NAO alertar — confere se '
              'o cenario era pra disparar. Veredicto: '
              f'{resultado.get("veredicto")}', 'warning')
    elif resultado.get('pulou'):
        flash(f'Vigia pulou: {resultado["pulou"]} (cheque CHATBOT_VIGIA e '
              'ANTHROPIC_API_KEY)', 'warning')
    else:
        flash(f'Vigia teste falhou: {resultado.get("erro") or resultado}',
              'danger')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/loja-online/vendas')
@login_required
@owner_required
def loja_online_vendas_painel():
    """Painel de VENDAS da loja própria (PedidoOnline): faturamento, ticket
    médio, top produtos, novos vs recorrentes — por período. Owner-only
    (dinheiro). Complementa o funil do GA4 (visita→carrinho→checkout→compra),
    que vive no painel do Google."""
    from datetime import timedelta

    from app.services import loja_online_vendas as lov
    dias = max(1, min(request.args.get('dias', 30, type=int), 365))
    fim = hoje_brt()
    ini = fim - timedelta(days=dias - 1)
    fat = lov.faturamento_por_dia(ini, fim)
    clientes = lov.resumo_clientes(ini, fim)
    prods = lov.produtos_vendidos(ini, fim)
    ticket = (fat['total'] / fat['n_pedidos']) if fat['n_pedidos'] else 0.0
    return render_template('admin/loja_online_vendas.html',
                           dias=dias, ini=ini, fim=fim, fat=fat,
                           ticket=ticket, clientes=clientes,
                           produtos=prods['produtos'][:15])


@main_bp.route('/admin/loja-online/auditoria-catalogo')
@login_required
def loja_online_auditoria_catalogo():
    """Fase 0 da Loja Online (16/06/2026): auditoria de pre-requisitos do
    catalogo. Quantos produtos ja estao 'prontos pra vitrine' (preco_site +
    imagem) e quantos VNDA-orfaos restam mapear. Read-only — so observa o
    estado, nao muda nada.

    Plano completo: /root/.claude/plans/modular-tinkering-owl.md (Loja
    propria substituindo VNDA). docs/loja-online/fase-0-checklist.md
    lista os passos manuais (Pagar.me sandbox, contador, etc)."""
    from sqlalchemy import or_

    from app.models import Produto, Receita

    # Receitas
    rec_total = Receita.query.count()
    rec_ativas = Receita.query.filter(Receita.arquivada_em.is_(None)).count()
    rec_preco_site = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        Receita.preco_site.isnot(None),
        Receita.preco_site > 0).count()
    rec_img = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        or_(Receita.imagem_dropbox_url.isnot(None),
            Receita.imagem_url.isnot(None))).count()
    rec_prontas = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        Receita.preco_site.isnot(None), Receita.preco_site > 0,
        or_(Receita.imagem_dropbox_url.isnot(None),
            Receita.imagem_url.isnot(None))).count()
    rec_faltando = (Receita.query
                    .filter(Receita.arquivada_em.is_(None))
                    .filter(or_(Receita.preco_site.is_(None),
                                Receita.preco_site == 0,
                                Receita.imagem_dropbox_url.is_(None),
                                Receita.imagem_url.is_(None)))
                    .order_by(Receita.nome).limit(40).all())

    # Produtos (cestas/kits)
    prod_total = Produto.query.count()
    prod_ativos = Produto.query.filter_by(ativo=True).count()
    prod_preco_site = Produto.query.filter(
        Produto.ativo.is_(True),
        Produto.preco_site.isnot(None),
        Produto.preco_site > 0).count()
    prod_img = Produto.query.filter(
        Produto.ativo.is_(True),
        or_(Produto.imagem_dropbox_url.isnot(None),
            Produto.imagem_url.isnot(None))).count()
    prod_prontos = Produto.query.filter(
        Produto.ativo.is_(True),
        Produto.preco_site.isnot(None), Produto.preco_site > 0,
        or_(Produto.imagem_dropbox_url.isnot(None),
            Produto.imagem_url.isnot(None))).count()
    prod_faltando = (Produto.query
                     .filter_by(ativo=True)
                     .filter(or_(Produto.preco_site.is_(None),
                                 Produto.preco_site == 0,
                                 Produto.imagem_dropbox_url.is_(None),
                                 Produto.imagem_url.is_(None)))
                     .order_by(Produto.nome).limit(40).all())

    return render_template(
        'admin/loja_online_auditoria_catalogo.html',
        rec_total=rec_total, rec_ativas=rec_ativas,
        rec_preco_site=rec_preco_site, rec_img=rec_img,
        rec_prontas=rec_prontas, rec_faltando=rec_faltando,
        prod_total=prod_total, prod_ativos=prod_ativos,
        prod_preco_site=prod_preco_site, prod_img=prod_img,
        prod_prontos=prod_prontos, prod_faltando=prod_faltando,
    )


# ── Loja Online — Fase 1: curadoria de catálogo (16/06/2026) ──────────
#
# Decisao do dono: "todo item com preco_site sobe no site". Esta tela e o
# "comando central" do catalogo: lista compacta com preço inline + upload de
# foto, sem sair da pagina. Edita rapido o que ainda esta faltando antes da
# Fase 2 (vitrine) entrar.
#
# Reusa: `dropbox_storage.upload_publico` + `app.utils.comprimir_imagem` +
# colunas `preco_site` / `imagem_dropbox_url` ja existentes. Sem schema novo.

@main_bp.route('/admin/loja-online/catalogo')
@login_required
def loja_online_catalogo():
    """Lista combinada de Receitas + Produtos com edicao rapida de preco e
    upload de foto. Filtros via query string: ?filtro=no-site|sem-preco|
    sem-foto|todos (default: todos)."""
    from app.models import Produto, Receita
    from app.services import loja_catalogo
    # Default 'todos' pro dono (curadoria); 'no-site' pros demais (so veem o
    # que ja esta vendendo no site — nao tem o que fazer com sem-preco/sem-foto
    # pois nao podem editar). Decisao do dono 22/06/2026.
    default_filtro = 'todos' if getattr(current_user, 'is_owner', False) else 'no-site'
    filtro = (request.args.get('filtro') or default_filtro).strip().lower()

    # Estoque atual na loja do site (a mesma de /pedidos/estoque-loja). None =
    # loja do site não configurada → não dá pra editar estoque aqui.
    estoque_map = loja_catalogo._estoque_site_map()

    # Receitas ativas
    rec_q = Receita.query.filter(Receita.arquivada_em.is_(None))
    # Produtos ativos
    prod_q = Produto.query.filter_by(ativo=True)

    receitas = rec_q.order_by(Receita.categoria, Receita.nome).all()
    produtos = prod_q.order_by(Produto.nome).all()

    # Unifica em uma lista com 'tipo' pra o template
    itens = []
    for r in receitas:
        tem_foto = bool(r.imagem_dropbox_url or r.imagem_url)
        tem_preco = r.preco_site is not None and r.preco_site > 0
        item = {
            'tipo': 'receita', 'id': r.id, 'nome': r.nome,
            'categoria': r.categoria or '',
            'ordem_site': r.ordem_site,
            'preco_site': r.preco_site,
            'imagem': r.imagem_dropbox_url or r.imagem_url,
            'no_site': tem_foto and tem_preco,
            'falta_foto': not tem_foto,
            'falta_preco': not tem_preco,
            'estoque': (None if estoque_map is None
                        else estoque_map.get(('receita', r.id), 0)),
        }
        itens.append(item)
    for p in produtos:
        tem_foto = bool(p.imagem_dropbox_url or p.imagem_url)
        tem_preco = p.preco_site is not None and p.preco_site > 0
        item = {
            'tipo': 'produto', 'id': p.id, 'nome': p.nome,
            'categoria': p.categoria or '(cesta/kit)',
            'ordem_site': p.ordem_site,
            'preco_site': p.preco_site,
            'imagem': p.imagem_dropbox_url or p.imagem_url,
            'no_site': tem_foto and tem_preco,
            'falta_foto': not tem_foto,
            'falta_preco': not tem_preco,
            'estoque': (None if estoque_map is None
                        else estoque_map.get(('produto', p.id), 0)),
        }
        itens.append(item)

    if filtro == 'no-site':
        itens = [i for i in itens if i['no_site']]
    elif filtro == 'sem-preco':
        itens = [i for i in itens if i['falta_preco']]
    elif filtro == 'sem-foto':
        itens = [i for i in itens if i['falta_foto']]

    contagens = {
        'todos': len(receitas) + len(produtos),
        'no_site': sum(1 for r in receitas if (r.preco_site or 0) > 0 and (r.imagem_dropbox_url or r.imagem_url))
                  + sum(1 for p in produtos if (p.preco_site or 0) > 0 and (p.imagem_dropbox_url or p.imagem_url)),
        'sem_preco': sum(1 for r in receitas if not r.preco_site or r.preco_site <= 0)
                    + sum(1 for p in produtos if not p.preco_site or p.preco_site <= 0),
        'sem_foto': sum(1 for r in receitas if not (r.imagem_dropbox_url or r.imagem_url))
                   + sum(1 for p in produtos if not (p.imagem_dropbox_url or p.imagem_url)),
    }
    # Lista de categorias já cadastradas (Produtos + Receitas) — alimenta
    # o autocomplete (datalist) na edição inline.
    cats = set()
    for r in receitas:
        if r.categoria:
            cats.add(r.categoria.strip())
    for p in produtos:
        if p.categoria:
            cats.add(p.categoria.strip())
    categorias_existentes = sorted(c for c in cats if c)
    return render_template('admin/loja_online_catalogo.html',
                            itens=itens, filtro=filtro, contagens=contagens,
                            categorias_existentes=categorias_existentes)


@main_bp.route('/admin/loja-online/catalogo/preco/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_preco(tipo, id):
    """Atualiza preco_site via AJAX. JSON: {preco: float|null}. Aceita
    null/0 pra TIRAR do site. Owner-only — dinheiro."""
    from decimal import Decimal, InvalidOperation

    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('preco')
    if raw is None or raw == '' or raw == 0:
        obj.preco_site = None
    else:
        try:
            val = Decimal(str(raw).replace(',', '.'))
        except (InvalidOperation, ValueError, TypeError):
            return jsonify(ok=False, erro='preço inválido'), 400
        if val < 0 or val > 9999:
            return jsonify(ok=False, erro='preço fora da faixa (0 a 9999)'), 400
        obj.preco_site = float(val)
    _db.session.commit()
    return jsonify(ok=True,
                   preco_site=(float(obj.preco_site)
                               if obj.preco_site is not None else None))


@main_bp.route('/admin/loja-online/catalogo/estoque/<tipo>/<int:id>',
                methods=['POST'])
@login_required
def loja_online_catalogo_estoque(tipo, id):
    """Define o estoque ATUAL do item na loja do site — a MESMA EstoqueLoja
    que /pedidos/estoque-loja usa. JSON: {estoque: int}. SET absoluto: grava
    a diferença como MovEstoqueLoja pra manter o histórico consistente.
    Aberto a QUALQUER usuário logado (só `@login_required`) — decisão do dono:
    problema de estoque do site precisa ser resolvido com urgência por quem
    estiver na mão. O MovEstoqueLoja registra quem mexeu (trilha de auditoria)."""
    from app.extensions import db as _db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.loja_pagamento import loja_origem_site
    if tipo not in ('receita', 'produto'):
        return jsonify(ok=False, erro='tipo inválido'), 400
    loja = loja_origem_site()
    if not loja:
        return jsonify(ok=False, erro='loja do site não configurada'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('estoque')
    if raw is None or str(raw).strip() == '':
        return jsonify(ok=False, erro='quantidade obrigatória'), 400
    try:
        novo = int(str(raw).strip())
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='quantidade inválida'), 400
    if novo < 0 or novo > 100000:
        return jsonify(ok=False, erro='quantidade fora da faixa (0 a 100000)'), 400
    from app.services.estoque_helpers import serializar_loja
    # SET absoluto le `atual` e grava `novo` — sem o lock, uma baixa concorrente
    # (Seru/site) entre o read e o write seria sobrescrita. Lock por loja.
    serializar_loja(loja.id)
    filtro = {'loja_id': loja.id,
              ('receita_id' if tipo == 'receita' else 'produto_id'): id}
    el = EstoqueLoja.query.filter_by(**filtro).first()
    atual = (el.quantidade or 0) if el else 0
    if not el:
        el = EstoqueLoja(quantidade=0, **filtro)
        _db.session.add(el)
        _db.session.flush()
    delta = novo - atual
    el.quantidade = novo
    if delta != 0:
        _db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='entrada_manual' if delta > 0 else 'ajuste_negativo',
            quantidade=abs(delta),
            referencia='ajuste catálogo do site',
            usuario_id=current_user.id))
    _db.session.commit()
    return jsonify(ok=True, estoque=el.quantidade)


@main_bp.route('/admin/loja-online/catalogo/ordem/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_ordem(tipo, id):
    """Atualiza a `ordem_site` do item (edição inline). JSON:
    {ordem: int|null}. Vazio/null = item vai pro fim alfabético."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('ordem')
    if raw is None or raw == '':
        obj.ordem_site = None
    else:
        try:
            obj.ordem_site = int(raw)
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='ordem precisa ser inteiro'), 400
    _db.session.commit()
    return jsonify(ok=True, ordem=obj.ordem_site)


@main_bp.route('/admin/loja-online/categorias/ordem', methods=['POST'])
@owner_required
def loja_online_categorias_ordem():
    """Salva a nova ordem das categorias em lote.
    Body JSON: {ordem: ['Pães', 'Bebidas', 'Conservas']}.
    Faz upsert pra cada (ordem = índice).
    """
    from app.models import CategoriaSite
    dados = request.get_json(silent=True) or {}
    nomes = dados.get('ordem') or []
    if not isinstance(nomes, list):
        return jsonify(ok=False, erro='ordem precisa ser lista'), 400
    existentes = {c.nome: c for c in CategoriaSite.query.all()}
    for i, nome in enumerate(nomes):
        nome = (nome or '').strip()[:50]
        if not nome:
            continue
        if nome in existentes:
            existentes[nome].ordem = i
        else:
            db.session.add(CategoriaSite(nome=nome, ordem=i))
    db.session.commit()
    return jsonify(ok=True, salvas=len([n for n in nomes if n]))


@main_bp.route('/admin/loja-online/produtos/ordem', methods=['POST'])
@owner_required
def loja_online_produtos_ordem():
    """Salva a nova ordem dos PRODUTOS dentro de uma categoria em lote.
    Body JSON: {itens: [{tipo: 'produto'|'receita', id: int}, ...]}.
    O índice na lista vira o `ordem_site` (do menor pro maior).
    """
    from app.models import Produto, Receita
    dados = request.get_json(silent=True) or {}
    itens = dados.get('itens') or []
    if not isinstance(itens, list):
        return jsonify(ok=False, erro='itens precisa ser lista'), 400
    salvas = 0
    for i, it in enumerate(itens):
        tipo = (it.get('tipo') or '').strip()
        try:
            iid = int(it.get('id'))
        except (TypeError, ValueError):
            continue
        if tipo == 'produto':
            obj = Produto.query.get(iid)
        elif tipo == 'receita':
            obj = Receita.query.get(iid)
        else:
            continue
        if not obj:
            continue
        obj.ordem_site = i
        salvas += 1
    db.session.commit()
    return jsonify(ok=True, salvas=salvas)


@main_bp.route('/admin/loja-online/ordem-produtos')
@owner_required
def loja_online_ordem_produtos():
    """Tela pra reordenar PRODUTOS por categoria via drag-and-drop.
    Agrupa publicados pela categoria; cada grupo é uma lista sortable."""
    from app.services import loja_catalogo
    itens = loja_catalogo.produtos_publicados()
    grupos = loja_catalogo.por_categorias(itens)
    return render_template('admin/loja_online_ordem_produtos.html',
                            grupos=grupos)


@main_bp.route('/admin/loja-online/categorias', methods=['GET', 'POST'])
@owner_required
def loja_online_categorias():
    """Gestão da ordem das categorias na vitrine. GET mostra; POST salva."""
    from app.models import CategoriaSite, Produto, Receita
    # Coleta TODAS as categorias usadas no catálogo (Produto + Receita).
    cats_uso = set()
    for r in (Receita.ativas()
              .with_entities(Receita.categoria).distinct()):
        if r[0]:
            cats_uso.add(r[0].strip())
    for p in (Produto.query.filter(Produto.ativo.is_(True))
              .with_entities(Produto.categoria).distinct()):
        if p[0]:
            cats_uso.add(p[0].strip())

    if request.method == 'POST':
        # Recebe pares (nome, ordem) e upserta. Categoria removida do form
        # vira sem peso (vai pro fim alfabético).
        nomes = request.form.getlist('nome')
        ordens = request.form.getlist('ordem')
        existentes = {c.nome: c for c in CategoriaSite.query.all()}
        for nome, ord_str in zip(nomes, ordens):
            nome = (nome or '').strip()[:50]
            if not nome:
                continue
            try:
                ordem = int(ord_str)
            except (TypeError, ValueError):
                ordem = 0
            if nome in existentes:
                existentes[nome].ordem = ordem
            else:
                db.session.add(CategoriaSite(nome=nome, ordem=ordem))
        db.session.commit()
        from flask import flash
        flash('Ordem das categorias atualizada.', 'success')
        return redirect(url_for('main.loja_online_categorias'))

    existentes = {c.nome: c.ordem for c in CategoriaSite.query.all()}
    # Combina: começa com as que TÊM ordem (em ordem), depois as outras
    # (alfabética).
    com_ordem = sorted(
        ((n, o) for n, o in existentes.items() if n in cats_uso),
        key=lambda x: (x[1], x[0].lower()))
    sem_ordem = sorted(
        ((n, 0) for n in cats_uso if n not in existentes),
        key=lambda x: x[0].lower())
    linhas = com_ordem + sem_ordem
    return render_template('admin/loja_online_categorias.html',
                            linhas=linhas)


@main_bp.route('/admin/loja-online/catalogo/categoria/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_categoria(tipo, id):
    """Atualiza a categoria do item (edição inline). JSON: {categoria: str}.
    Vazio limpa (item cai em 'Outros' na vitrine)."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    cat = (dados.get('categoria') or '').strip()[:50] or None
    obj.categoria = cat
    _db.session.commit()
    return jsonify(ok=True, categoria=cat or '')


@main_bp.route('/admin/loja-online/catalogo/foto/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_foto(tipo, id):
    """Upload de foto via AJAX. JSON de resposta: {ok, imagem_url}. Reusa
    `comprimir_imagem` + `dropbox_storage.upload_publico` (padrão de
    `cardapio_img_upload`)."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400

    f = request.files.get('imagem_arquivo') or request.files.get('foto')
    if not f or not f.filename:
        return jsonify(ok=False, erro='nenhum arquivo enviado'), 400
    if not (f.mimetype or '').startswith('image/'):
        return jsonify(ok=False, erro='arquivo não é imagem'), 400
    data = f.read()
    if not data:
        return jsonify(ok=False, erro='arquivo vazio'), 400
    if len(data) > 25 * 1024 * 1024:
        return jsonify(ok=False, erro='imagem maior que 25MB'), 400
    try:
        final = comprimir_imagem(data)
        if dropbox_storage.disponivel():
            path = f'/cardapio/{tipo}/{obj.id}.jpg'
            info = dropbox_storage.upload_publico(
                final, path, mode='overwrite', autorename=False)
            obj.imagem_dropbox_url = info['url']
            obj.imagem_storage_path = info['storage_path']
            obj.imagem_blob = None
        else:
            obj.imagem_blob = final
        obj.imagem_mimetype = 'image/jpeg'
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, erro=f'erro ao processar: {exc}'), 500
    _db.session.commit()
    return jsonify(ok=True,
                   imagem_url=(obj.imagem_dropbox_url or ''))


@main_bp.route('/admin/loja-online/logo', methods=['POST'])
@owner_required
def loja_online_logo():
    """Upload do logotipo da loja → Dropbox → URL guardada em AppConfig
    (`loja_logo_url`). O header da vitrine renderiza o logo se setado, senão
    cai no wordmark de texto. Preserva transparência (PNG/SVG) pra não ficar
    caixa branca sobre o fundo creme."""
    from flask import flash

    from app.models import AppConfig
    from app.services import dropbox_storage
    from app.utils import comprimir_logo
    f = request.files.get('logo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if not (f.mimetype or '').startswith('image/'):
        flash('O arquivo precisa ser uma imagem (PNG, SVG ou JPG).', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    data = f.read()
    if not data:
        flash('Arquivo vazio.', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if len(data) > 10 * 1024 * 1024:
        flash('Logo grande demais (máx 10MB).', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if not dropbox_storage.disponivel():
        flash('Dropbox não configurado — não dá pra subir o logo agora.',
              'danger')
        return redirect(url_for('main.loja_online_dashboard'))
    try:
        proc, _mime, ext = comprimir_logo(data)
        info = dropbox_storage.upload_publico(
            proc, f'/loja/logo.{ext}', mode='overwrite', autorename=False)
        AppConfig.set('loja_logo_url', info['url'])
        db.session.commit()
        flash('Logo atualizado!', 'success')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(f'Erro ao subir o logo: {exc}', 'danger')
    return redirect(url_for('main.loja_online_dashboard'))


@main_bp.route('/admin/loja-online/logo/remover', methods=['POST'])
@owner_required
def loja_online_logo_remover():
    """Volta o header pro wordmark de texto (limpa `loja_logo_url`)."""
    from flask import flash

    from app.models import AppConfig
    AppConfig.set('loja_logo_url', None)
    db.session.commit()
    flash('Logo removido — header volta ao texto.', 'success')
    return redirect(url_for('main.loja_online_dashboard'))


@main_bp.route('/admin/debug-redirect-dominio')
@owner_required
def debug_redirect_dominio():
    """Confirma como está o redirect do domínio antigo. Mostra os hosts
    armados e o destino — sem segredos. Use pra checar que o
    SITE_REDIRECT_HOSTS no Railway ficou certo ANTES de mexer no DNS."""
    cfg = current_app.config
    hosts = [h.strip().lower() for h in (cfg.get('SITE_REDIRECT_HOSTS') or '')
             .split(',') if h.strip()]
    destino = (cfg.get('SITE_REDIRECT_DESTINO')
               or 'https://opao.online').rstrip('/') + '/'
    return jsonify(
        ativo=bool(hosts),
        hosts=hosts,
        destino=destino,
        instrucao=('Hosts armados — vai responder 302 pro destino quando o '
                   'DNS apontar pra cá.' if hosts else
                   'Inerte — defina SITE_REDIRECT_HOSTS no Railway.'),
    )


@main_bp.route('/admin/loja-online/prontidao')
@login_required
def loja_online_prontidao():
    """Pré-flight do CUTOVER: o que precisa estar pronto ANTES de apontar o
    domínio antigo pro site novo. GO/NO-GO + pendências. O bloqueio nº 1 é
    LOJA_VISIVEL — sem ela, o cliente anônimo redirecionado vê 404."""
    import os

    from app.blueprints.loja.routes import _loja_visivel_publico
    from app.services import loja_catalogo, pagarme
    cfg = current_app.config

    loja_visivel = _loja_visivel_publico()
    pg_ambiente = pagarme.ambiente()
    pg_ok = pagarme.disponivel() and pg_ambiente == 'producao'
    redirect_hosts = [h.strip() for h in (cfg.get('SITE_REDIRECT_HOSTS') or '')
                      .split(',') if h.strip()]
    loja_hosts = sorted(h.strip().lower()
                        for h in (cfg.get('LOJA_HOSTS') or '').split(',')
                        if h.strip())
    host_atual = (request.host or '').split(':')[0].lower()
    host_atual_eh_loja_publica = host_atual in loja_hosts if loja_hosts else False
    postmark_ok = bool((cfg.get('POSTMARK_SERVER_TOKEN') or '').strip())
    sentry_ok = bool(os.environ.get('SENTRY_DSN', '').strip())
    ga4_set = bool((cfg.get('GA4_ID') or '').strip())
    pixel_set = bool((cfg.get('META_PIXEL_ID') or '').strip())

    # Produtos no site sem estoque (aparecem como "Esgotado") — aviso, não bloqueio.
    mapa = loja_catalogo._estoque_site_map() or {}
    esgotados = sum(1 for it in loja_catalogo.produtos_publicados()
                    if not (mapa.get((it['kind'], it['id'])) or 0) > 0)

    pendencias = []
    if not loja_visivel:
        pendencias.append('BLOQUEIO: LOJA_VISIVEL não é 1 — cliente anônimo vê '
                          '404. Defina LOJA_VISIVEL=1 no Railway ANTES de trocar '
                          'o DNS.')
    if not pg_ok:
        pendencias.append(f'BLOQUEIO: Pagar.me não está produção/ok '
                          f'(ambiente={pg_ambiente}).')
    if not loja_hosts:
        pendencias.append('BLOQUEIO: LOJA_HOSTS vazio — qualquer domínio cai '
                          'no fallback fail-open (loja serve em todo host, '
                          'incluindo gestao.*). Defina LOJA_HOSTS com os '
                          'domínios públicos.')
    if not postmark_ok:
        pendencias.append('BLOQUEIO: POSTMARK_SERVER_TOKEN ausente — cliente '
                          'não recebe confirmação de pedido nem NF.')
    avisos = []
    if not sentry_ok:
        avisos.append('AVISO: SENTRY_DSN ausente — erros 500 passam em '
                      'silêncio. Não bloqueia, mas atrasa diagnóstico.')
    if not (ga4_set or pixel_set):
        avisos.append('AVISO: nem GA4_ID nem META_PIXEL_ID configurados — '
                      'site sobe sem analytics/remarketing. Opcional.')

    return jsonify(
        pronto=(loja_visivel and pg_ok and bool(loja_hosts) and postmark_ok),
        loja_visivel=loja_visivel,
        loja_hosts=loja_hosts,
        host_atual=host_atual,
        host_atual_eh_loja_publica=host_atual_eh_loja_publica,
        pagarme_ambiente=pg_ambiente,
        pagarme_ok=pg_ok,
        postmark_ok=postmark_ok,
        sentry_ok=sentry_ok,
        ga4_configurado=ga4_set,
        meta_pixel_configurado=pixel_set,
        redirect_hosts_armados=redirect_hosts,
        produtos_esgotados=esgotados,
        pendencias=pendencias,
        avisos=avisos,
        nota=('produtos_esgotados é AVISO (eles aparecem como "Esgotado" no '
              'site até você preencher o estoque), não bloqueia o cutover.'),
    )


# ── Debug Pagar.me: valida a chave sem expor o segredo (Fase 4) ───────────
@main_bp.route('/admin/debug-pagarme')
@owner_required
def debug_pagarme():
    """Diagnóstico do Pagar.me (owner-only). Confirma se a chave cadastrada
    no Railway é válida e em qual ambiente (sandbox/produção), SEM expor o
    segredo. Útil pra saber se as chaves são reais ou placeholders."""
    from app.services import pagarme
    cfg = current_app.config
    seg = (cfg.get('PAGARME_WEBHOOK_SECRET') or '').strip()
    # Máscara do secret VIVO neste container (owner-only): len + 4 primeiros +
    # 4 últimos. Serve pra confirmar que o redeploy do Railway já aplicou o
    # valor novo (inicio muda) ANTES de reenviar o webhook no Pagar.me.
    secret_mascara = ({'len': len(seg), 'inicio': seg[:4], 'fim': seg[-4:]}
                      if len(seg) > 8 else {'len': len(seg)})
    return jsonify(
        configurado=pagarme.disponivel(),
        ambiente=pagarme.ambiente(),
        api_key_len=len((cfg.get('PAGARME_API_KEY') or '')),
        api_key_prefixo=pagarme.prefixo_chave(),
        public_key_len=len((cfg.get('PAGARME_PUBLIC_KEY') or '')),
        public_key_prefixo=pagarme.prefixo_public(),
        webhook_secret_set=bool(seg),
        webhook_secret_mascara=secret_mascara,
        resultado=pagarme.validar_chave(),
    )


@main_bp.route('/admin/debug-pagarme/ultimo-webhook')
@owner_required
def debug_pagarme_ultimo_webhook():
    """Mostra metadados MASCARADOS do último hit do webhook (esperado vs
    fornecido). Útil pra entender por que o Pagar.me marca "Falha":
    `bate: false` + `status: 401` → secret divergente. Os campos
    `inicio`/`fim` mostram só 4 chars de cada lado pra COMPARAÇÃO visual,
    sem expor o valor."""
    from app.blueprints.loja.routes import ler_ultimo_hit_pagarme
    hit = ler_ultimo_hit_pagarme()
    if not hit:
        return jsonify(erro='nenhum hit registrado neste container ainda; '
                       'reenvie um webhook pelo painel do Pagar.me e tente '
                       'de novo')
    return jsonify(hit)


@main_bp.route('/admin/debug-pagarme/conciliar/<codigo>')
@owner_required
def debug_pagarme_conciliar(codigo):
    """Conciliação manual (owner) — rede de segurança pra webhook perdido.
    Consulta o Pagar.me pelo order_id salvo; com ?aplicar=1 marca o pedido
    pago se o gateway confirmar (baixa estoque + e-mail). Sem ?aplicar=1 =
    dry-run. Idempotente: ignora a idempotência do webhook e _marcar_pago é
    no-op se já pago."""
    from app.services import loja_pagamento
    aplicar = request.args.get('aplicar') == '1'
    res = loja_pagamento.conciliar_pedido(codigo, aplicar=aplicar)
    return jsonify(res), (200 if res.get('ok') else 400)


@main_bp.route('/admin/debug-pagarme/eventos')
@owner_required
def debug_pagarme_eventos():
    """Lista os últimos eventos do webhook do Pagar.me recebidos pelo
    servidor. Diagnóstico: webhook NÃO chegou = nada aqui (URL/secret/
    seleção de eventos errados no painel do Pagar.me)."""
    from app.models import PagarmeEvento
    n = max(1, min(int(request.args.get('n', 20)), 200))
    eventos = (PagarmeEvento.query
               .order_by(PagarmeEvento.recebido_em.desc()).limit(n).all())
    return jsonify(total=len(eventos), eventos=[
        {'evento_id': e.evento_id, 'tipo': e.tipo,
         'recebido_em': e.recebido_em.isoformat(sep=' ', timespec='seconds')
                       if e.recebido_em else None}
        for e in eventos])


@main_bp.route('/admin/debug-pagarme/pedido/<codigo>')
@owner_required
def debug_pagarme_pedido(codigo):
    """Raio-X de UM pedido pra entender por que o status não mudou:
    status atual, pagamentos com order_id/charge_id do Pagar.me, e os
    eventos do webhook por id (precisa bater pelo `data.id`/`data.code`)."""
    from app.models import PagamentoOnline, PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not p:
        return jsonify(erro='pedido nao encontrado', codigo=codigo), 404
    pagamentos = PagamentoOnline.query.filter_by(pedido_id=p.id).all()
    return jsonify(
        codigo=p.codigo, status=p.status,
        valor_total=str(p.valor_total),
        criado_em=p.criado_em.isoformat(sep=' ', timespec='seconds')
                 if p.criado_em else None,
        pagamentos=[{
            'id': pg.id, 'metodo': pg.metodo, 'status': pg.status,
            'pagarme_order_id': pg.pagarme_order_id,
            'pagarme_charge_id': pg.pagarme_charge_id,
            'criado_em': pg.criado_em.isoformat(sep=' ', timespec='seconds')
                        if pg.criado_em else None,
            'erro': pg.erro,
        } for pg in pagamentos],
    )


# ── Pedidos do site (Fase 3): acompanhamento dos PedidoOnline ─────────────
# Tela pra o dono acompanhar os pedidos que entram pelo checkout nativo. Read
# + cancelar. Em Fase 3 o pedido nasce 'aguardando_pagamento' e NAO baixa
# estoque; cancelar aqui e so mudanca de status (sem estorno/refund — isso
# entra na Fase 4 com o Pagar.me).

_STATUS_PEDIDO_ONLINE_LABEL = {
    'aguardando_pagamento': 'Aguardando pagamento',
    'pago': 'Pago',
    'em_preparo': 'Em preparo',
    'a_caminho': 'A caminho',
    'entregue': 'Entregue',
    'cancelado': 'Cancelado',
    'divulgacao': '⭐ Divulgação',
}


def _catalogo_divulgacao():
    """Itens selecionaveis na tela de divulgacao: receitas ATIVAS + produtos
    ATIVOS com nome (preco so referencia). Helpers canonicos — nunca arquivado.
    Ordenado por nome; kind='receita'|'produto' pra o value do select."""
    itens = []
    for r in (Receita.ativas().order_by(Receita.nome).all()):
        itens.append({'kind': 'receita', 'id': r.id, 'nome': r.nome,
                      'preco': float(r.preco_site or 0)})
    for p in (Produto.query.filter_by(ativo=True)
              .order_by(Produto.nome).all()):
        itens.append({'kind': 'produto', 'id': p.id, 'nome': p.nome,
                      'preco': float(p.preco_site or 0)})
    itens.sort(key=lambda i: i['nome'].lower())
    return itens


def _menus_divulgacao():
    """Regras + slots dos MENUS configuraveis (Caixa de Mini etc.) pro
    montador da tela de divulgacao (20/08/2026, caso 24FB0FFB — dono quer
    escolher os minis "como no site"). {'produto:<id>': {'total', 'teto',
    'slots': [{'pi_id','nome','preco','padrao'}]}}. So produtos ativos que
    `loja_menu.eh_menu` reconhece."""
    from app.services import loja_menu
    out = {}
    for p in Produto.query.filter_by(ativo=True).all():
        if not loja_menu.eh_menu(p):
            continue
        total, teto = loja_menu.regras(p)
        out['produto:%d' % p.id] = {
            'total': total, 'teto': teto,
            'slots': [{'pi_id': s['pi_id'], 'nome': s['nome'],
                       'preco': s['preco'], 'padrao': s['padrao']}
                      for s in loja_menu.slots(p)],
        }
    return out


@main_bp.route('/admin/loja-online/divulgacao', methods=['GET', 'POST'])
@login_required
@divulgacao_required
def loja_online_divulgacao():
    """Lanca uma DIVULGACAO (brinde/PR): pedido "como do site" SEM pagamento,
    que aparece no painel de entregas com estrela ⭐. Baixa estoque de verdade
    (marcado, fora de faturamento/previsao). SO o dono e o papel 'marketing'
    (nunca admin comum) — e um gesto de dar produto de graca."""
    from datetime import date as _date

    from flask import flash, redirect, url_for

    from app.models import Loja
    from app.services import divulgacao as div_svc
    # Lojas operacionais pra retirada (ativas, fora a Industria de RH).
    lojas = [lo for lo in Loja.query.filter_by(ativa=True)
             .order_by(Loja.nome).all() if lo.nome != 'Indústria']

    if request.method == 'POST':
        modo = (request.form.get('modo_entrega') or 'agendada').strip()
        # Itens: pares kind:id[] + qtd[] (linhas dinamicas no form).
        alvos = request.form.getlist('item_alvo[]')     # "receita:12"
        qtds = request.form.getlist('item_qtd[]')
        # Composicao do MENU (montador dos minis, 20/08/2026): um JSON
        # {produto_item_id: qtd} por linha, '' nas linhas comuns. O service
        # re-valida tudo (normalizar/validar/preco) — aqui so desserializa.
        comps = request.form.getlist('item_comp[]')
        itens = []
        for i, (alvo, q) in enumerate(zip(alvos, qtds)):
            alvo = (alvo or '').strip()
            if not alvo or ':' not in alvo:
                continue
            kind, _, sid = alvo.partition(':')
            comp = None
            if i < len(comps) and (comps[i] or '').strip():
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
    """Regras semanais e excecoes da disponibilidade da loja online."""
    from datetime import date as _date

    from app.services import loja_catalogo, loja_plano_dia
    from app.utils import hoje
    modo = (request.args.get('view') or 'semanal').strip().lower()
    if modo not in ('semanal', 'excecoes'):
        modo = 'semanal'
    data_str = (request.args.get('data') or '').strip()
    try:
        alvo = _date.fromisoformat(data_str) if data_str else hoje()
    except ValueError:
        alvo = hoje()

    itens_publicados = loja_catalogo.produtos_publicados()
    from app.models import EstoqueSiteExcecao, EstoqueSiteRegraSemanal
    regras = {(r.kind, r.item_id): r
              for r in EstoqueSiteRegraSemanal.query.all()}
    excecoes = {(r.kind, r.item_id): r
                for r in EstoqueSiteExcecao.query.filter_by(data=alvo).all()}

    itens = []
    for it in itens_publicados:
        chave = (it['kind'], it['id'])
        regra = regras.get(chave)
        excecao = excecoes.get(chave)
        configuracao = loja_plano_dia.configuracao_dia(
            it['kind'], it['id'], alvo)
        itens.append({
            'kind': it['kind'], 'id': it['id'],
            'nome': it['nome'], 'categoria': it['categoria'],
            'regra': regra,
            'dias_ativos': ([d for d in range(7)
                             if regra and regra.dias_mask & (1 << d)]),
            'excecao': excecao,
            'configuracao': configuracao,
        })

    # Regras ativas primeiro; dentro de cada grupo, nome alfabetico. Assim a
    # tela normal mostra logo o que realmente foi restringido.
    itens.sort(key=lambda it: (0 if it['regra'] else 1,
                               (it['nome'] or '').casefold()))

    return render_template('admin/loja_online_plano_dia.html',
                           itens=itens, data=alvo, modo=modo,
                           data_str=alvo.isoformat(),
                           total_regras=len(regras),
                           total_excecoes=len(excecoes))


def _item_publicado_no_site(kind, item_id):
    """Valida o alvo dos formularios sem confiar nos campos do navegador."""
    from app.services import loja_catalogo
    return any(it['kind'] == kind and it['id'] == item_id
               for it in loja_catalogo.produtos_publicados())


@main_bp.route('/admin/loja-online/plano-do-dia/regra-semanal',
               methods=['POST'])
@owner_required
def loja_online_regra_semanal_salvar():
    """Salva a regra recorrente ou devolve o item ao modo sempre livre."""
    from app.services import loja_plano_dia
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        abort(400)
    if (kind not in ('receita', 'produto')
            or not _item_publicado_no_site(kind, item_id)):
        abort(400)

    if request.form.get('regra') == 'sempre':
        loja_plano_dia.remover_regra_semanal(kind, item_id)
        flash('Produto liberado para todos os dias.', 'success')
    else:
        dias = request.form.getlist('dias')
        tipo_limite = request.form.get('tipo_limite')
        try:
            limite = (None if tipo_limite == 'sem_limite'
                      else int(request.form.get('qtd_limite')))
            loja_plano_dia.salvar_regra_semanal(
                kind, item_id, dias, limite)
        except (TypeError, ValueError) as exc:
            flash(str(exc) or 'Confira os dias e a quantidade.', 'danger')
        else:
            flash('Regra semanal salva.', 'success')
    return redirect(url_for('main.loja_online_plano_dia', view='semanal')
                    + f'#item-{kind}-{item_id}')


@main_bp.route('/admin/loja-online/plano-do-dia/excecao', methods=['POST'])
@owner_required
def loja_online_excecao_salvar():
    """Salva ou remove uma excecao pontual da regra semanal."""
    from datetime import date as _date

    from app.services import loja_plano_dia
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
        data = _date.fromisoformat(request.form.get('data'))
    except (TypeError, ValueError):
        abort(400)
    if (kind not in ('receita', 'produto')
            or not _item_publicado_no_site(kind, item_id)):
        abort(400)

    tipo = (request.form.get('tipo_excecao') or 'herdar').strip()
    try:
        if tipo == 'herdar':
            loja_plano_dia.remover_excecao(kind, item_id, data)
        elif tipo == 'bloqueado':
            loja_plano_dia.salvar_excecao(kind, item_id, data, 0)
        elif tipo == 'sem_limite':
            loja_plano_dia.salvar_excecao(kind, item_id, data, None)
        elif tipo == 'limite':
            limite = int(request.form.get('qtd_limite'))
            if limite <= 0:
                raise ValueError('o limite precisa ser maior que zero')
            loja_plano_dia.salvar_excecao(kind, item_id, data, limite)
        else:
            raise ValueError('tipo de excecao invalido')
    except (TypeError, ValueError) as exc:
        flash(str(exc) or 'Confira a quantidade.', 'danger')
    else:
        flash('Excecao da data salva.', 'success')
    return redirect(url_for('main.loja_online_plano_dia', view='excecoes',
                            data=data.isoformat())
                    + f'#item-{kind}-{item_id}')


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
