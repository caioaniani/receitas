"""Destinos da busca por tarefa, resolvidos no servidor por usuário.

A busca tem vocabulário próprio ("senha", "tirar do site", "minis"), por
isso mantém seus rótulos e sinônimos separados dos menus de áreas. Os URLs
vêm dos endpoints Flask e os acessos usam as capacidades efetivas das rotas,
inclusive os bloqueios por conta. Não modifica permissões nem executa ações.
"""
from flask import url_for

from app.services import permissoes


def itens_para_usuario(usuario, categorias_receitas):
    """Recebe as receitas já filtradas por atribuição no contexto da sidebar."""
    if not usuario.is_authenticated:
        return []

    itens = []

    def adicionar(titulo, endpoint, categoria='Navegar', icon='arrow-right',
                  aliases=(), principal=False, **params):
        itens.append(dict(titulo=titulo, url=url_for(endpoint, **params),
                          categoria=categoria, icon=icon,
                          aliases=list(aliases), principal=principal))

    def capacidade(nome):
        return usuario.is_admin() or permissoes.pode(usuario.papel or '', nome)

    adicionar('Trocar minha senha', 'auth.minha_senha', 'Minha conta', 'key',
              aliases=['minha senha', 'meu acesso'])
    adicionar('Sair', 'auth.logout', 'Minha conta', 'box-arrow-right')
    if usuario.senha_provisoria:
        return itens
    if usuario.is_observador():
        adicionar('Sala de controle', 'pedidos.painel_observador',
                  principal=True)
        adicionar('Pedidos de todos os canais', 'pedidos.consulta',
                  principal=True)
        return itens

    adicionar('Meu treinamento', 'treino.home', 'Equipe', 'mortarboard')
    if usuario.pode_checklist():
        adicionar('Preencher checklist', 'checklist.index', 'Lojas',
                  'clipboard-check', aliases=['abertura', 'fechamento',
                                               'troca de turno'],
                  principal=not usuario.is_admin())
    if usuario.pode_organizar_equipe():
        adicionar('Organizar equipe', 'rh.lideranca_preenchimento', 'Equipe',
                  'diagram-3', aliases=['líder', 'liderança', 'loja e turno',
                                        'gerentes', 'atendentes chefes'])
    # O RH está restrito ao dono; o cadastro básico tem uma exceção estreita.
    if usuario.pode_cadastrar_funcionarios() and capacidade('web_rh'):
        adicionar('Funcionários', 'rh.funcionarios', 'Equipe', 'people',
                  aliases=['cadastro de pessoas'])
    if usuario.somente_treino:
        return itens

    adicionar('Hoje', 'main.index', icon='house', aliases=['início', 'home'],
              principal=True)
    if capacidade('web_pedidos'):
        adicionar('Pedidos das lojas', 'pedidos.lista', 'Lojas', 'cart',
                  aliases=['reposição das lojas'], principal=True)
        adicionar('Novo pedido para a loja', 'pedidos.novo', 'Lojas',
                  'plus-circle')
    if capacidade('web_estoque_loja'):
        adicionar('Pedidos do site', 'main.loja_online_pedidos', 'Site', 'bag',
                  aliases=['pedido online', 'compras do site'], principal=True)
        adicionar('Estoque das lojas', 'pedidos.estoque_loja', 'Lojas', 'box',
                  aliases=['estoque da loja'])
        adicionar('Conferência de estoque', 'pedidos.conferencia', 'Lojas',
                  'clipboard-check', aliases=['contagem da loja'])
        adicionar('Histórico de estoque', 'pedidos.estoque_loja_historico',
                  'Lojas', 'clock-history')
        adicionar('Desperdício', 'pedidos.desperdicio', 'Lojas', 'trash')
        adicionar('Relatório de pedidos', 'pedidos.relatorio', 'Lojas',
                  'file-text')
    if capacidade('web_producao'):
        adicionar('Listas de compra da produção', 'producao.lista', 'Produção')
        adicionar('Estoque de congelados', 'pedidos.congelados', 'Produção',
                  'snow')
        adicionar('Separar pedidos das lojas', 'pedidos.separacao', 'Produção',
                  'list-check', aliases=['pedidos a separar'])
    if capacidade('web_padeiro'):
        adicionar('Tela do padeiro', 'padeiro.index', 'Produção',
                  'check2-square', aliases=['confirmar produção'])
    if capacidade('web_catalogo'):
        adicionar('Matérias-primas', 'materias_primas.banco', 'Catálogo',
                  'flower3', aliases=['ingredientes'])
        adicionar('Estoque de matérias-primas', 'materias_primas.estoque',
                  'Catálogo', 'boxes', aliases=['estoque MP'])
        adicionar('Fornecedores', 'fornecedores.lista', 'Catálogo', 'truck')
        adicionar('Produtos e cestas', 'produtos.lista', 'Catálogo', 'gift')
        adicionar('Modo padeiro das fichas', 'receitas.padeiro_lista',
                  'Catálogo', 'eyeglasses', aliases=['modo padeiro'])
        adicionar('Cardápio de atacado', 'main.cardapio', 'Atacado', 'file-pdf',
                  aliases=['cardápio PDF', 'minis', 'mini pães', 'minipães',
                           'cardápio de minis'], tipo='atacado')

    if usuario.is_admin():
        adicionar('Plano de produção', 'industria_teste.index', 'Produção',
                  'calendar-week', aliases=['cronograma', 'motor de previsão'],
                  principal=True)
        adicionar('Auditoria de produção', 'industria_teste.auditoria',
                  'Produção', 'clipboard-data', principal=True,
                  aliases=['histórico de produção', 'produção confirmada',
                           'pendências do padeiro', 'produção não feita'])
        adicionar('Pedidos semanais das lojas', 'producao.pedidos_semana_media',
                  'Produção', 'calendar3', aliases=['pedidos da semana'])
        adicionar('Conferir checklists', 'checklist.conferencia', 'Lojas',
                  'clipboard-check', principal=True,
                  aliases=['checklists feitos', 'auditoria de checklist'])
        adicionar('Responsáveis pelo checklist', 'checklist.responsaveis',
                  'Equipe', 'person-check',
                  aliases=['responsável', 'liberar checklist',
                           'adicionar pessoa no checklist'])
        adicionar('Configurar itens do checklist', 'checklist.config', 'Lojas',
                  'list-check')
        # A curadoria é consultável pelo admin, mas ativar/desativar é do dono.
        adicionar('Produtos do site', 'main.loja_online_catalogo', 'Site',
                  'shop-window', aliases=['catálogo do site'] + (
                      ['tirar do site', 'retirar de venda', 'desativar produto',
                       'ativar produto', 'ocultar produto']
                      if usuario.is_dono() else []), filtro='no-site',
                  principal=True)
        adicionar('Vendas no atacado', 'b2b.dashboard', 'Atacado', 'building',
                  aliases=['B2B', 'venda para empresas'], principal=True)
        adicionar('Nova venda no atacado', 'b2b.venda_nova', 'Atacado',
                  'plus-circle', aliases=['novo pedido atacado', 'vender minis'])
        adicionar('Clientes do atacado', 'b2b.clientes', 'Atacado', 'people')
        adicionar('Orçamentos do atacado', 'b2b.orcamentos', 'Atacado',
                  'file-text')
        adicionar('Vendas do caixa', 'pdv.index', 'Vendas', 'cash-stack',
                  aliases=['PDV'])
        adicionar('Itens vendidos', 'pdv.itens_vendidos', 'Vendas', 'graph-up')
        adicionar('Vincular produtos do caixa', 'pdv.mapeamentos', 'Vendas',
                  'link-45deg', aliases=['mapeamentos Seru'])
        adicionar('Entregas do site', 'entregas.index', 'Site', 'geo-alt')
        adicionar('Dashboards', 'relatorios.dashboards', 'Relatórios',
                  'bar-chart-line')
        adicionar('Caixa diário', 'main.caixa', 'Relatórios', 'piggy-bank')
        adicionar('Rentabilidade', 'main.rentabilidade', 'Relatórios',
                  'currency-dollar')
        adicionar('Relatórios de custos', 'relatorios.custos', 'Relatórios',
                  'bar-chart')
        adicionar('Previsão de demanda', 'relatorios.previsao', 'Relatórios',
                  'graph-up')
        adicionar('Pendências do catálogo', 'main.todo', 'Catálogo',
                  'check2-square', aliases=['TO-DO'])
        adicionar('Fichas atribuídas', 'auth.painel', 'Equipe', 'diagram-2',
                  aliases=['atribuições'])
        adicionar('Usuários', 'auth.usuarios', 'Sistema', 'person-badge')
        adicionar('Histórico de alterações', 'main.audit', 'Sistema',
                  'shield-check', aliases=['audit log', 'quem alterou'])
        adicionar('Manual de operação', 'main.manual_operacao', 'Sistema',
                  'book', aliases=['ajuda', 'como funciona'])
        adicionar('Exportar JSON', 'main.exportar', 'Sistema', 'download')
        adicionar('Slack bot', 'slack.install', 'Sistema', 'slack')

    if usuario.is_dono():
        adicionar('Estoque do site', 'main.loja_online_plano_dia', 'Site',
                  'calendar-check', principal=True,
                  aliases=['plano do dia', 'quantidade à venda',
                           'disponibilidade do site'])
        adicionar('Acessos dos funcionários', 'rh.funcionarios', 'Equipe',
                  'person-lock', view='acessos', acesso='todos',
                  aliases=['senha', 'enviar nova senha', 'e-mail de acesso',
                           'liberar acesso', 'login da equipe'])
        adicionar('Cargos e salários', 'rh.plano_carreira', 'Equipe',
                  'signpost-split', aliases=['plano de carreira',
                                            'planilha de cargos'])
        adicionar('Escala operacional', 'rh.escala', 'Equipe', 'calendar3',
                  aliases=['escala de trabalho', 'turnos'])
        adicionar('Painel RH', 'rh.dashboard', 'Equipe', 'people')
        adicionar('Folha de pagamento', 'rh.folha', 'Equipe', 'cash-coin')
        adicionar('Cadastro de lojas', 'rh.lojas', 'Equipe', 'shop')
        adicionar('Ponto', 'rh.ponto', 'Equipe', 'fingerprint',
                  aliases=['lançar ponto'])
        adicionar('Férias e folgas', 'rh.ferias', 'Equipe', 'umbrella')

    for receitas in categorias_receitas.values():
        for receita in receitas:
            adicionar(receita.nome, 'receitas.ficha', 'Receitas',
                      'journal-text', id=receita.id)
    return itens
