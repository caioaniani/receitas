"""Universidade Padaria Artesanal — seed dos módulos e aulas (12/08/2026).

O dono mandou a estrutura completa ("📚 UNIVERSIDADE PADARIA ARTESANAL")
com 9 módulos e 140 aulas e pediu pra cadastrar, pra ele só abrir cada
aula e subir o vídeo (o fluxo canônico do admin do treino já é esse:
`admin_video_novo` cria a aula SEM `video_externo_id` e manda subir o
arquivo depois — ver app/blueprints/treino/routes.py).

Mapeamento: módulo -> `TreinoTrilha`; aula -> `TreinoVideo` (ordem = posição
na lista do dono, sem vídeo ainda). Decisões desta importação:

- **Trilhas e aulas nascem DESLIGADAS (`ativa=False`)**: o dono publica cada
  aula somente depois de subir e conferir o vídeo. Ligar um módulo libera
  apenas as aulas que ele publicou; os demais títulos continuam invisíveis.
- **Módulo 9 (Liderança) não é vinculado a cargo aqui**: os cargos são
  cadastro dinâmico do dono (/rh/cargos) — o vínculo trilha↔cargo se faz
  na tela do admin do treino, não em código.

O seed roda UMA vez (guard em AppConfig) e NUNCA ressuscita: a partir da
importação, o cadastro do dono manda — apagar/renomear trilha ou aula aqui
não volta no próximo deploy. `forcar=True` ignora só o guard, nunca a
deduplicação (trilha por nome normalizado; aula por título normalizado
dentro da trilha), então re-rodar não duplica nada.
"""
import logging
import unicodedata

from app.extensions import db

logger = logging.getLogger(__name__)

# Chave do guard — bump no sufixo só se um dia houver um segundo lote.
CFG_SEED = 'treino_seed_universidade_v1'

# (nome, descricao, [aulas na ordem do dono]). Trilha NOVA: `ordem` das
# aulas = índice na lista; trilha reusada (top-up): continua do max dela.
# A `ordem` das trilhas continua do max global existente.
MODULOS = [
    ('Módulo 1 — Cultura',
     'Quem somos, como nos comportamos e o padrão do qual não abrimos '
     'mão — a base de tudo.',
     ['Nossa história',
      'Missão e visão',
      'O que significa excelência aqui',
      'Princípio: “Como você atenderia sua mãe?”',
      'Postura profissional no salão',
      'Comunicação entre equipe',
      'Trabalho em equipe na prática',
      'Pontualidade e responsabilidade',
      'Uniforme e apresentação pessoal',
      'Mentalidade de dono',
      'Respeito ao cliente e colegas',
      'Como agir sob pressão',
      'Orgulho do produto que vendemos',
      'Cultura de melhoria contínua',
      'O que não toleramos (comportamentos proibidos)']),
    ('Módulo 2 — Atendimento',
     'Da abordagem nos primeiros 5 segundos à despedida: como atender, '
     'vender e encantar.',
     ['Abordagem inicial (primeiros 5 segundos)',
      'Linguagem corporal',
      'Tom de voz ideal',
      'Como identificar a necessidade do cliente',
      'Venda consultiva',
      'Como explicar produtos',
      'Como sugerir complementos (upsell)',
      'Como lidar com cliente indeciso',
      'Atendimento em fila cheia',
      'Atendimento rápido vs atendimento de qualidade',
      'Fechamento de venda',
      'Como encantar o cliente',
      'Erros comuns no atendimento',
      'Como lidar com cliente difícil',
      'Como resolver reclamações',
      'Recuperação de cliente insatisfeito',
      'Despedida correta',
      'Criando clientes recorrentes']),
    ('Módulo 3 — Produtos',
     'Sourdough, croissants, cafés e o que vender junto — conhecer o '
     'produto para recomendar bem.',
     ['O que é sourdough',
      'Diferencial dos nossos pães',
      'Linha de pães principais',
      'Croissants — padrão e qualidade',
      'Viennoiseries',
      'Bolos artesanais',
      'Cafés — básicos',
      'Bebidas frias',
      'Combinações (o que vender junto)',
      'Como descrever sabor',
      'Produtos mais vendidos',
      'Produtos de maior margem',
      'Como recomendar produtos',
      'Alérgenos e restrições',
      'Validade e conservação',
      'Como apresentar o produto ao cliente',
      'Storytelling dos produtos',
      'Degustação (quando e como usar)',
      'Erros comuns ao explicar produtos',
      'Segurança alimentar básica']),
    ('Módulo 4 — Operação',
     'Abertura, vitrine, reposição, limpeza e fechamento — a loja rodando '
     'redonda o dia inteiro.',
     ['Abertura da loja',
      'Organização do salão',
      'Organização da vitrine',
      'Reposição de produtos',
      'Padrão de limpeza',
      'Limpeza contínua',
      'Checklist de turno',
      'Passagem de turno',
      'Organização de estoque',
      'FIFO (primeiro que entra, primeiro que sai)',
      'Controle de desperdício',
      'Uso correto de equipamentos',
      'Cuidados com utensílios',
      'Organização da retaguarda',
      'Prioridades no horário de pico',
      'Comunicação com a produção',
      'Fluxo de pedidos',
      'Fechamento da loja']),
    ('Módulo 5 — Caixa e Sistemas',
     'Caixa aberto, pedido registrado, pagamento certo e fechamento '
     'sem sustos.',
     ['Abertura de caixa',
      'Registro de pedidos',
      'Uso do sistema',
      'Formas de pagamento',
      'Pix',
      'Cartão',
      'Dinheiro',
      'Troco correto',
      'Cancelamentos',
      'Estornos',
      'Sangria',
      'Conferência de caixa',
      'Fechamento de caixa',
      'Erros comuns de caixa',
      'Segurança no caixa']),
    ('Módulo 6 — Experiência do Cliente',
     'Os detalhes que transformam um bom atendimento numa experiência que '
     'traz o cliente de volta.',
     ['O que é experiência do cliente',
      'Detalhes que encantam',
      'Apresentação dos produtos',
      'Como servir café',
      'Como servir alimentos',
      'Embalagem correta',
      'Tempo ideal de atendimento',
      'Antecipar a necessidade do cliente',
      'Personalização do atendimento',
      'Como surpreender positivamente',
      'Ambiente e clima da loja',
      'Consistência de experiência',
      'Experiência no horário de pico',
      'Erros que quebram a experiência',
      'Padrão de excelência diário']),
    ('Módulo 7 — Segurança e Boas Práticas',
     'Higiene, manipulação de alimentos e segurança no trabalho — o '
     'básico inegociável.',
     ['Higiene pessoal',
      'Lavagem correta das mãos',
      'Uso de EPIs',
      'Manipulação de alimentos',
      'Contaminação cruzada',
      'Controle de temperatura',
      'Armazenamento correto',
      'Validade dos produtos',
      'Limpeza sanitária',
      'Segurança no trabalho',
      'Prevenção de acidentes',
      'O que fazer em emergência',
      'Descarte correto',
      'Normas básicas sanitárias']),
    ('Módulo 8 — Desenvolvimento Profissional',
     'Postura, comunicação, feedback e disciplina — crescer junto com '
     'a empresa.',
     ['Postura profissional',
      'Comunicação clara',
      'Inteligência emocional',
      'Como receber feedback',
      'Como dar feedback',
      'Resolução de conflitos',
      'Gestão do tempo',
      'Produtividade no turno',
      'Responsabilidade individual',
      'Crescimento na empresa',
      'Mentalidade de aprendizado',
      'Disciplina e consistência']),
    ('Módulo 9 — Liderança',
     'Para líderes: treinar o time, cobrar padrão e formar novos líderes.',
     ['Papel do líder',
      'Como treinar o time',
      'Como dar feedback',
      'Como cobrar padrão',
      'Como usar checklist',
      'Como observar comportamento',
      'Como corrigir erros',
      'Como motivar a equipe',
      'Como conduzir reuniões',
      'Como acompanhar indicadores',
      'Como resolver conflitos',
      'Formação de novos líderes',
      'Cultura através da liderança']),
]


def _norm(s):
    """Nome sem acento/caixa/espaços duplicados — chave de deduplicação."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.casefold().split())


def importar_universidade(forcar=False):
    """Cria os módulos (trilhas) e aulas (vídeos sem arquivo) da
    Universidade. Devolve {'trilhas': N, 'aulas': N}.

    Roda UMA vez (guard em AppConfig): a partir daí o cadastro do dono
    manda — apagar/renomear trilha ou aula NÃO ressuscita no próximo
    deploy. `forcar` ignora só o guard, nunca a deduplicação, então
    re-rodar não duplica. Trilha homônima já existente é REUSADA (ganha só
    as aulas que faltam, com a `ativa` do dono intocada)."""
    from app.models import AppConfig, TreinoTrilha, TreinoVideo

    if not forcar and AppConfig.get(CFG_SEED):
        return {'trilhas': 0, 'aulas': 0}

    trilhas_por_nome = {_norm(t.nome): t for t in TreinoTrilha.query.all()}
    prox_ordem = (db.session.query(
        db.func.coalesce(db.func.max(TreinoTrilha.ordem), 0)).scalar() or 0)
    criadas = aulas_criadas = 0
    for nome, descricao, aulas in MODULOS:
        # Dedup pela MESMA forma que é gravada (truncada): com os dados de
        # hoje o truncamento nunca dispara (nome máx 39 chars), mas num
        # segundo lote uma string longa deduplicada pela forma inteira e
        # gravada truncada duplicaria no re-run (achado de revisão).
        nome = nome[:150]
        trilha = trilhas_por_nome.get(_norm(nome))
        if trilha is None:
            prox_ordem += 1
            # DESLIGADA de propósito: o dono liga o módulo no /treino/admin
            # quando os vídeos dele estiverem no ar (ver docstring do topo).
            trilha = TreinoTrilha(nome=nome, descricao=descricao,
                                  ordem=prox_ordem, ativa=False)
            db.session.add(trilha)
            db.session.flush()
            trilhas_por_nome[_norm(nome)] = trilha
            criadas += 1
        titulos_ja = {_norm(v.titulo) for v in trilha.videos}
        ordem_video = max((v.ordem for v in trilha.videos), default=-1)
        for titulo in aulas:
            titulo = titulo[:200]          # dedup pela forma gravada
            if _norm(titulo) in titulos_ja:
                continue
            ordem_video += 1
            db.session.add(TreinoVideo(
                trilha_id=trilha.id, titulo=titulo, ordem=ordem_video,
                provedor='cloudflare', ativo=False))
            titulos_ja.add(_norm(titulo))
            aulas_criadas += 1
    AppConfig.set(CFG_SEED, '1')
    db.session.commit()
    logger.info('treino: seed da Universidade criou %d trilha(s) e %d '
                'aula(s)', criadas, aulas_criadas)
    return {'trilhas': criadas, 'aulas': aulas_criadas}
