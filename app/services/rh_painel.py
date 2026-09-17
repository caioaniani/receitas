"""Visão de pessoas: somente leitura, reaproveitando os critérios existentes."""
from app.services import rh_equipe, rh_equipe_lojas, treino_painel


def carregar():
    equipe = rh_equipe.carregar_visao({})
    estrutura = rh_equipe_lojas.carregar_lojas()
    pessoas = [linha['pessoa'] for linha in equipe['linhas']]
    # Sem ciclo: precisamos dos estados, não do cálculo pessoa × módulo.
    treinamento = treino_painel.painel_equipe(pessoas, None)
    grupos = []

    def grupo(titulo, descricao, itens, aba, acao):
        if itens:
            grupos.append(dict(titulo=titulo, descricao=descricao,
                               pessoas=itens, aba=aba, acao=acao))

    grupo('Sem conta vinculada', 'Conferir conta existente antes de criar outra.',
          [p for p in pessoas if not p.usuario_id], 'acesso', 'Conferir acesso')
    grupo('Senha provisória pendente', 'A troca da senha ainda não foi concluída; isso não confirma recebimento do e-mail.',
          [p for p in pessoas if p.usuario and p.usuario.senha_provisoria
           and not p.usuario.is_owner], 'acesso', 'Conferir acesso')
    grupo('Cargo ou nível a conferir', 'Considera o cargo atual, nunca uma proposta de promoção.',
          [l['pessoa'] for l in equipe['linhas']
           if l['nivel'] is None and not l['direcao']], 'resumo', 'Ver pessoa')
    for status, titulo, descricao in (
        ('nao_iniciou', 'Treinamento não iniciado', 'Pessoas com módulos obrigatórios e sem atividade registrada nas aulas.'),
        ('parado', 'Retomar acompanhamento', 'Sem atividade nas aulas há 7 dias ou mais. Converse com a pessoa; não é uma avaliação de desempenho.'),
    ):
        grupo(titulo, descricao, [l['funcionario'] for l in treinamento['linhas']
                                if l['status'] == status], 'treinamento', 'Ver treinamento')
    # A lotação é canônica: principal, sem duplicar vínculos adicionais/direção.
    estrutura['lojas'].sort(key=lambda l: (not bool(l['total']), l['nome'].casefold()))
    return dict(equipe=equipe, estrutura=estrutura, pendencias=grupos,
                total=len(pessoas),
                pessoas_pendentes=len({p.id for g in grupos for p in g['pessoas']}))
