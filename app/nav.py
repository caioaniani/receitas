"""Metadados das ÁREAS do hub (tela inicial do admin).

Fonte única do título/ícone/cor/descrição de cada área + o predicado de
permissão que decide quem vê o card e quem pode abrir a página da área
(`/area/<slug>`). Os LINKS das funções de cada área NÃO ficam aqui — vivem no
macro `templates/_area_nav.html`, reaproveitado pela sidebar e pela página da
área (fonte única, sem duplicação).

`pode` recebe o `current_user` e espelha exatamente o guarda que o card já
tinha em `home.html`."""

AREAS = [
    {'slug': 'lojas', 'titulo': 'Lojas', 'icone': '🏪', 'cor': '#ffc107',
     'desc': 'Pedidos, estoque e lista de compras das lojas',
     'pode': lambda u: u.pode_lojas()},
    {'slug': 'producao', 'titulo': 'Produção', 'icone': '🥖', 'cor': '#198754',
     'desc': 'Painel, cronograma, fluxograma e massas base',
     'pode': lambda u: u.pode_producao()},
    {'slug': 'catalogo', 'titulo': 'Catálogo', 'icone': '📚', 'cor': '#0d6efd',
     'desc': 'Matérias-primas, fornecedores, produtos e preços',
     'pode': lambda u: u.pode_catalogo()},
    {'slug': 'vendas', 'titulo': 'Vendas & Entregas', 'icone': '🛒',
     'cor': '#6f42c1', 'desc': 'Caixa (PDV), B2B e entregas do site',
     'pode': lambda u: u.pode_pdv() or u.is_admin() or u.pode_lojas()},
    {'slug': 'financeiro', 'titulo': 'Financeiro', 'icone': '💰',
     'cor': '#20c997', 'desc': 'Cobranças, contas a pagar, caixa diário e rentabilidade',
     'pode': lambda u: u.is_admin()},
    {'slug': 'rh', 'titulo': 'RH', 'icone': '👥', 'cor': '#d63384',
     'desc': 'Funcionários, folha, escala, ponto e férias',
     'pode': lambda u: u.is_dono()},
    {'slug': 'relatorios', 'titulo': 'Relatórios', 'icone': '📈',
     'cor': '#fd7e14', 'desc': 'Dashboards, custos e previsão de demanda',
     'pode': lambda u: u.is_admin()},
    {'slug': 'administracao', 'titulo': 'Administração', 'icone': '⚙️',
     'cor': '#6c757d', 'desc': 'Usuários, permissões e integrações',
     'pode': lambda u: u.is_admin()},
    {'slug': 'fichas', 'titulo': 'Fichas Técnicas', 'icone': '📖',
     'cor': '#0dcaf0', 'desc': 'Receitas e modo de preparo',
     'pode': lambda u: u.is_authenticated},
]

_POR_SLUG = {a['slug']: a for a in AREAS}


def areas_visiveis(user):
    """Áreas que `user` pode ver (na ordem de AREAS)."""
    return [a for a in AREAS if a['pode'](user)]


def area_por_slug(slug):
    """Metadados da área ou None se o slug não existir."""
    return _POR_SLUG.get(slug)
