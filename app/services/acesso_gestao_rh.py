"""Escopo fixo da chefia de RH, sem privilégios de administrador do ERP.

Novos endpoints precisam ser revisados antes de entrar nesta lista. Cadastro
fiscal/PIN de lojas, credenciais e importação de contatos ficam com o dono.
"""

ENDPOINTS_RH = frozenset('rh.' + nome for nome in (
    'dashboard', 'administrativo', 'funcionarios', 'novo_funcionario',
    'detalhe_funcionario', 'salvar_funcionario', 'add_feedback', 'excluir_feedback',
    'lideranca', 'lideranca_vinculos', 'lideranca_preenchimento',
    'lideranca_preenchimento_salvar', 'lideranca_compartilhar',
    'lideranca_compartilhada_remover', 'lideranca_organograma',
    'lideranca_organograma_pdf', 'lideranca_checklist',
    'equipe', 'equipe_lojas', 'equipe_lojas_adicionar', 'equipe_lojas_importar',
    'equipe_lojas_remover', 'carreira_funcionario', 'promover_funcionario',
    'plano_carreira', 'plano_carreira_importar', 'plano_carreira_importar_aplicar',
    'plano_carreira_decisao', 'plano_carreira_aprovar_lote',
    'cargos', 'salvar_cargos', 'excluir_cargo', 'unificar_atendentes',
    'pre_cadastros', 'pre_cadastro_promover', 'pre_cadastro_descartar',
    'folha', 'gerar_folha', 'salvar_folha_item', 'holerite_pdf',
    'excluir_folha_item', 'excluir_folha_mes', 'folha_importar', 'folha_aplicar',
    'escala', 'atribuir_posicao', 'nova_posicao', 'excluir_posicao',
    'novo_atestado', 'ver_atestado', 'excluir_atestado', 'novo_feedback_dashboard',
    'mapa_index', 'mapa', 'upload_planta', 'excluir_planta', 'ver_planta',
    'api_slots', 'api_criar_slot', 'api_excluir_slot', 'api_alocar',
    'ferias', 'ferias_nova', 'ferias_excluir', 'ponto', 'ponto_registrar', 'ponto_resumo',
))

ACESSOS_TREINO = frozenset({
    'treino.admin_gerar_acesso', 'treino.admin_gerar_acessos_todos',
    'treino.admin_vincular_acesso',
})


def endpoint_permitido(endpoint):
    return (endpoint in ENDPOINTS_RH
            or (endpoint.startswith('treino.') and endpoint not in ACESSOS_TREINO))
