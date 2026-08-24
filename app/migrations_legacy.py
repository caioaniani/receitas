Warning: truncated output (original token count: 48808)
Total output lines: 3890

"""Migrations legacy — sistema anterior ao Alembic.

Cada deploy chama `_migrate(app)` apos `db.create_all()`. As funcoes
`_migrate_postgres` e `_migrate_sqlite` aplicam `ALTER TABLE IF NOT EXISTS`
e `CREATE INDEX IF NOT EXISTS` de forma idempotente — mesma operacao em
2 sintaxes diferentes pra cobrir prod (Postgres) e dev local (SQLite).

Status: a partir de 2026-05-21 o sistema usa Alembic (ver migrations/).
Estas funcoes continuam aqui por compatibilidade e pra cobrir mudancas
de schema que ainda nao foram migradas pra Alembic.

ADICIONAR COLUNA NOVA: prefira gerar uma migration Alembic
(`flask db migrate`). So adicione aqui se for hot-fix urgente que
nao pode esperar revisao de migration.
"""
import logging
import os

from app.extensions import db

logger = logging.getLogger(__name__)

# Cobrança de sobra POR ITEM (01/08/2026, dono: "o pessoal não tem lançado
# sobra do croissant tradicional, precisamos atacar isso"). Backfill ÚNICO na
# criação da coluna `receita.cobra_sobra_diaria`: os itens que o dono AJUSTOU
# na conferência de estoque de 29-31/07 (lista comprovada — é o que ele
# controla nas lojas); depois disso o checkbox da ficha manda.
COBRA_SOBRA_SEED = [
    'Pão Francês Fermentado',
    'Croissant Tradicional',
    'Pain au Chocolat',
    'Sourdough 7 Grãos',
    'Sourdough Integral',
    'Sourdough Tradicional',
    'Sourdough Nozes e Azeitonas',
    'Cinnamon Roll',
    'Cinnamon Roll Doce de leite',
    'Danish de queijo branco',
    'Danish de Calabresa',
    'Danish de alho poró',
    'Cookie Calebaut',
    'Brioche',
    'Pão de Forma Integral com Grãos',
    'Croissant Almond',
]

# Backfill UNICO de receita.descricao_atacado (20/07/2026, ditado do dono:
# "descricao sincera de cada produto b2b, quanto menos e mais... fala dos
# ingredientes"). Textos escritos a partir dos ingredientes REAIS das fichas
# em prod (FarinhaT65/T45, Baton Callebaut etc.) + metodos ditados. Aplicado
# SO quando a coluna e criada; depois disso a ficha da receita manda.
DESCRICOES_ATACADO_SEED = [
    ('Sourdough Tradicional',
     'Farinha francesa T65, água, sal e levain. '
     'Vendido congelado; rende 14 fatias.'),
    ('Sourdough Integral',
     'Farinha francesa T65 e farinha integral, água, sal e levain. '
     'Vendido congelado; rende 14 fatias.'),
    ('Sourdough 7 Grãos',
     'Farinha francesa T65, mix de 7 grãos, água, sal e levain. '
     'Vendido congelado; rende 14 fatias.'),
    ('Sourdough Nozes e Azeitonas',
     'Farinha francesa T65, nozes e azeitonas, água, sal e levain. '
     'Vendido congelado; rende 14 fatias.'),
    ('Brioche',
     'Farinha francesa T45, manteiga, ovos e açúcar. '
     'Entregue fresco; validade de 3 dias.'),
    ('Croissant Tradicional',
     'Farinha francesa e manteiga francesa. '
     'Backup (congelado cru) ou assado e congelado.'),
    ('Pain au Chocolat',
     'Farinha francesa, manteiga francesa e chocolate belga Callebaut. '
     'Backup (congelado cru) ou assado e congelado.'),
    ('Cinnamon Roll',
     'Massa folhada de farinha e manteiga francesas, '
     'canela e creme de confeiteiro.'),
    ('Croissant Almond',
     'Croissant de farinha e manteiga francesas com creme de amêndoas '
     'e amêndoas laminadas.'),
]


def _migrate(app):
    """Adiciona colunas novas sem perder dados existentes."""
    uri = app.config['SQLALCHEMY_DATABASE_URI']

    if uri.startswith('sqlite'):
        _migrate_sqlite(app)
    elif 'postgresql' in uri:
        _migrate_postgres(app)

    # Em testes o conftest faz create_all (schema final) e os testes de
    # consolidacao precisam inserir duplicatas — a trava rodaria por teste sem
    # necessidade. O teste dedicado chama `_migrate_estoque_trava` diretamente.
    if not os.environ.get('PYTEST_RUNNING'):
        _migrate_estoque_trava(app)
        _seed_horario_dia_dos_pais(app)
        _seed_checklist_padrao(app)
        _seed_curadoria_dia_pais(app)
        _seed_curadoria_dia_pais_v2(app)
        _seed_drivers_entrega(app)
        _seed_cores_drivers(app)
        _seed_treino_universidade(app)
        _backfill_treino_aulas_sem_video_rascunho(app)
        _backfill_cargos_funcionarios(app)
        _seed_minimo_danish(app)
        _seed_minimo_danish_v2(app)
        _seed_minimo_danish_v3(app)
        _seed_minimo_cinnamon(app)
        _seed_antecedencia_brioche(app)
        _seed_minis_sanduiche(app)
        _seed_acerto_granola_iogurte(app)
        _backfill_totais_orcamento(app)


def _backfill_cargos_funcionarios(app):
    """Liga fichas antigas ao Cargo pelo nome da função, de forma idempotente.

    Não usa aproximação: caixa, acentos e espaços são ignorados, mas nomes
    realmente diferentes ou ambíguos continuam sem vínculo para revisão.
    Rodar em todo deploy também cobre funcionários importados depois da
    criação original de `cargo_id`.
    """
    try:
        from app.services import rh_cargos

        resultado = rh_cargos.associar_pendentes(commit=True)
        quantidade = len(resultado['associados'])
        pendentes = len(resultado['sem_correspondencia'])
        if quantidade or pendentes:
            logger.info('cargos de funcionarios: %d associado(s), %d para '
                        'revisao', quantidade, pendentes)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (cargos de funcionarios): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _backfill_totais_orcamento(app):
    """Reconserta UMA VEZ subtotal/valor_total dos orçamentos gravados pelo
    bug do editar (fix 18/08/2026, caso orc-2026-0003: editar 200x5 pra
    80x5 gravava R$ 1.400 — o recalcular_total somava itens deletados +
    novos). A venda da aprovação nunca herdou o erro (recalcula dos
    itens); aqui é só o registro/PDF do orçamento. Idempotente: recomputa
    da fonte (itens + desconto + frete). Marker grava a CONTAGEM corrigida
    (regra dos seeds com filtro). Best-effort: nunca derruba o startup."""
    try:
        from decimal import Decimal

        from app.extensions import db
        from app.models import AppConfig, Orcamento

        marker = 'backfill_orc_totais_2026_08_18'
        if AppConfig.get(marker):
            return
        corrigidos = []
        for orc in Orcamento.query.all():
            sub = sum((Decimal(str(i.subtotal or 0)) for i in orc.itens),
                      Decimal('0'))
            desc = Decimal(str(orc.desconto_valor or 0))
            frete = Decimal(str(orc.frete_valor or 0))
            total = max(Decimal('0'), sub - desc) + frete
            if (Decimal(str(orc.subtotal or 0)) != sub
                    or Decimal(str(orc.valor_total or 0)) != total):
                orc.subtotal = sub
                orc.valor_total = total
                corrigidos.append(orc.codigo)
        AppConfig.set(marker, ('corrigidos=%d %s'
                               % (len(corrigidos),
                                  ','.join(corrigidos)))[:500])
        db.session.commit()
        if corrigidos:
            logger.info('backfill totais orcamento: %d corrigido(s): %s',
                        len(corrigidos), ', '.join(corrigidos))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (backfill totais orcamento): %s', e)


def _seed_treino_universidade(app):
    """Cadastra UMA VEZ os 9 módulos e 140 aulas da Universidade Padaria
    Artesanal (estrutura mandada pelo dono em 12/08/2026) — as aulas nascem
    sem vídeo e as trilhas DESLIGADAS; o dono sobe os vídeos e liga cada
    módulo no /treino/admin. Guard em AppConfig: apagar/renomear depois NÃO
    ressuscita (cadastro do dono manda sobre seed). Best-effort: falhar
    aqui nunca derruba o startup. Pulado sob PYTEST_RUNNING (o teste do
    seed chama o serviço direto)."""
    try:
        from app.services import treino_seed
        treino_seed.importar_universidade()
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed treino universidade): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _backfill_treino_aulas_sem_video_rascunho(app):
    """Corrige UMA VEZ a publicação em massa da Universidade (21/08/2026).

    O seed original deixou as 140 aulas ativas e só o módulo desligado. Ao
    publicar o Módulo 1, todos os títulos apareceram mesmo sem arquivo. Aulas
    sem vídeo passam a rascunho; a 1.1 e qualquer outra aula que já tenha um
    UID do Cloudflare são preservadas. Marker impede que uma decisão editorial
    futura do dono seja refeita em outro deploy.
    """
    try:
        from app.models import AppConfig, TreinoVideo

        marker = 'treino_aulas_sem_video_rascunho_2026_08_21'
        if AppConfig.get(marker):
            return
        candidatas = TreinoVideo.query.filter(
            TreinoVideo.ativo.is_(True),
            db.or_(TreinoVideo.video_externo_id.is_(None),
                   TreinoVideo.video_externo_id == '')).all()
        for video in candidatas:
            video.ativo = False
        AppConfig.set(marker, f'desativadas={len(candidatas)}')
        db.session.commit()
        logger.info('treino: %d aula(s) sem video movidas para rascunho',
                    len(candidatas))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (rascunhos treino): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_checklist_padrao(app):
    """Importa UMA VEZ o checklist em papel da Opão (11 setores, 169 pontos).

    O dono mandou o PDF "CHECKLISTS OPERACIONAIS POR SETOR" em 03/08/2026 e
    pediu pra importar — então os itens já têm que estar lá quando o deploy
    subir, sem depender de alguém digitar 169 linhas.

    Guard em AppConfig: se ele apagar/editar itens depois, o próximo deploy
    NÃO ressuscita — cadastro do dono manda sobre seed (mesma regra do
    horário do Dia dos Pais). Best-effort: falhar aqui nunca derruba o
    startup. Pulado sob PYTEST_RUNNING (169 itens em toda fixture quebraria
    os testes que contam itens); o teste do seed chama o serviço direto."""
    try:
        from app.services import checklist_seed
        checklist_seed.importar_padrao()
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed checklist padrao): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# Data do pedido original do dono (27/07/2026): "no dia 09/08 tenha somente
# uma janela de horario para entrega: das 06:00 as 10:00. E dia dos pais".
_SEED_DIA_DOS_PAIS = {
    'chave': 'seed_horario_dia_dos_pais_2026',
    'data': '2026-08-09',
    'rotulo': 'Dia dos Pais',
    'janelas': '06:00–10:00',
}


def _seed_horario_dia_dos_pais(app):
    """Cadastra UMA VEZ o horario especial do Dia dos Pais 2026.

    A tabela `loja_data_especial` nasce vazia (`db.create_all`) e a tela e do
    dono — mas o pedido dele foi pra ESTA data, entao ela ja tem que estar la
    quando o deploy subir, sem depender de alguem lembrar de digitar.

    Roda UMA vez, marcada por AppConfig: se o dono APAGAR a data depois (ou
    mudar o horario), o proximo deploy NAO ressuscita o que ele decidiu —
    cadastro do dono manda sobre seed. Best-effort: falhar aqui nunca pode
    derrubar o startup do app."""
    from datetime import date as _date
    try:
        from app.models import AppConfig, LojaDataEspecial
        if AppConfig.get(_SEED_DIA_DOS_PAIS['chave']):
            return
        data = _date.fromisoformat(_SEED_DIA_DOS_PAIS['data'])
        if not LojaDataEspecial.query.filter_by(data=data).first():
            db.session.add(LojaDataEspecial(
                data=data,
                rotulo=_SEED_DIA_DOS_PAIS['rotulo'],
                janelas=_SEED_DIA_DOS_PAIS['janelas'],
                express_bloqueado=True))
        AppConfig.set(_SEED_DIA_DOS_PAIS['chave'], '1')
        db.session.commit()
        logger.info('seed: horario especial do Dia dos Pais cadastrado')
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed dia dos pais): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_curadoria_dia_pais(app, _hoje=None):
    """Fecha o catálogo do site pro Dia dos Pais 2026 (pedido do dono
    07/08/2026, "Faz isso pra mim": dos 9 somente as cestas que ele deixou
    com quantidade no plano-do-dia).

    O que faz, UMA vez (marker em AppConfig; edição do dono manda depois):
    1. Plano-do-dia de 09/08: cria linha `qtd_planejada=0` pra todo item
       PUBLICADO sem linha na data (o fail-open do plano deixaria vender
       livre) e ZERA linha auto-criada com o default 99999 (rastro de venda,
       não é curadoria; `qtd_reservada` preservada). Linha com
       0 < qtd < 99999 é CURADORIA DO DONO — intocada (as cestas dele).
    2. GUARD DE SEGURANÇA: só age se já existe pelo menos UMA linha de
       curadoria (0 < qtd < 99999) na data. Sem ela, zerar tudo deixaria o
       dia 9 SEM NADA à venda — inclusive as cestas.
    3. `LojaDataEspecial` de 09/08: preenche `bloquear_itens='Mini Pães'`
       quando vazio (cinto e suspensório da mesma decisão — barra a
       categoria dos minis mesmo pra item publicado depois da curadoria).
    Depois de 09/08/2026 só marca e sai (seed velho não mexe em nada)."""
    from datetime import date as _date
    try:
        from app.models import AppConfig, EstoqueSitePlano, LojaDataEspecial
        from app.services import loja_catalogo
        from app.services.loja_plano_dia import DEFAULT_QTD_PLANEJADA
        from app.utils import hoje as _hoje_fn
        chave = 'seed_curadoria_dia_pais_2026'
        if AppConfig.get(chave):
            return
        alvo = _date(2026, 8, 9)
        hoje_d = _hoje if _hoje is not None else _hoje_fn()
        if hoje_d > alvo:
            AppConfig.set(chave, 'expirado')
            db.session.commit()
            return
        linhas = EstoqueSitePlano.query.filter_by(data=alvo).all()
        curadas = [ln for ln in linhas
                   if 0 < (ln.qtd_planejada or 0) < DEFAULT_QTD_PLANEJADA]
        if not curadas:
            logger.warning(
                'seed curadoria dia dos pais: NENHUMA linha com quantidade '
                'no plano de %s — não vou zerar o dia inteiro; nada feito '
                '(o dono cura na tela e o seed fica de fora)', alvo)
            AppConfig.set(chave, 'sem_curadoria')
            db.session.commit()
            return
        por_chave = {(ln.kind, ln.item_id): ln for ln in linhas}
        criadas = zeradas = 0
        for it in loja_catalogo.produtos_publicados():
            ln = por_chave.get((it['kind'], it['id']))
            if ln is None:
                db.session.add(EstoqueSitePlano(
                    kind=it['kind'], item_id=it['id'], data=alvo,
                    qtd_planejada=0, qtd_reservada=0))
                criadas += 1
            elif (ln.qtd_planejada or 0) >= DEFAULT_QTD_PLANEJADA:
                ln.qtd_planejada = 0
                zeradas += 1
        regra = LojaDataEspecial.query.filter_by(data=alvo).first()
        if regra is not None and not (regra.bloquear_itens or '').strip():
            regra.bloquear_itens = 'Mini Pães'
        AppConfig.set(chave, '1')
        db.session.commit()
        logger.info(
            'seed curadoria dia dos pais: %d linha(s) zerada(s) criada(s), '
            '%d default-99999 zerada(s); curadoria do dono preservada '
            '(%d item(ns) com quantidade)', criadas, zeradas, len(curadas))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed curadoria dia dos pais): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_curadoria_dia_pais_v2(app, _hoje=None):
    """Correção do seed v1 (07/08/2026, mesmo dia): a 'Cesta dia dos pais'
    (39 vendas pagas pro dia 9) e a 'Caixa Especial' (10) estavam com a
    linha AUTO-criada de 99999 — o dono nunca digitou quantidade nelas — e
    o v1 as zerou junto com o resto, fechando as duas cestas MAIS vendidas
    do Dia dos Pais. Reabre as duas com 10000, o mesmo 'sem limite prático'
    que o dono digitou na Family Box e na Bandeja. Guard: só mexe em linha
    que está EM ZERO (ajuste do dono entre deploys manda); marker; expira
    depois de 09/08."""
    from datetime import date as _date
    try:
        from app.models import AppConfig, EstoqueSitePlano
        from app.services import loja_catalogo
        chave = 'seed_curadoria_dia_pais_2026_v2'
        if AppConfig.get(chave):
            return
        alvo = _date(2026, 8, 9)
        if _hoje is None:
            from app.utils import hoje as _hoje_fn
            _hoje = _hoje_fn()
        if _hoje > alvo:
            AppConfig.set(chave, 'expirado')
            db.session.commit()
            return
        reabrir = {'cesta dia dos pais', 'caixa especial'}
        alvos = [(it['kind'], it['id']) for it in
                 loja_catalogo.produtos_publicados()
                 if (it['nome'] or '').strip().lower() in reabrir]
        n = 0
        for kind, item_id in alvos:
            ln = EstoqueSitePlano.query.filter_by(
                kind=kind, item_id=item_id, data=alvo).first()
            if ln is not None and (ln.qtd_planejada or 0) == 0:
                ln.qtd_planejada = 10000
                n += 1
        AppConfig.set(chave, '1')
        db.session.commit()
        logger.info('seed curadoria dia dos pais v2: %d cesta(s) '
                    'reaberta(s) com 10000', n)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed curadoria pais v2): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# Motoristas contratados que o dono mandou por WhatsApp (07/08/2026,
# "cadastrar esses motoristas" — semana do Dia dos Pais). Telefone ja em
# digitos com DDI 55: e o formato que a Z-API usa no envio do magic link
# (`normalizar_telefone` so tira mascara, nao adiciona o 55).
_SEED_DRIVERS_2026_08 = [
    ('Andreia', '5511998909264'),
    ('Alinne', '5511984652398'),
    ('Rodrigo', '5511983578852'),
    ('Roberta', '5511966152906'),
    ('Anderson', '5511998513825'),
    ('Carolina', '5511920488991'),
    ('Luís', '5511911874548'),
    ('Márcia', '5511998137354'),
    ('Alessandra', '5511953950106'),
    ('Sibele', '5511988976979'),
    ('Hélio', '5511983811876'),
]


def _seed_drivers_entrega(app):
    """Cadastra UMA VEZ os motoristas da lista do dono (07/08/2026).

    Marker em AppConfig: depois do primeiro boot a tela de drivers do
    /entregas/painel manda — o seed nunca re-cria quem o dono apagar nem
    sobrescreve o que ele editar. Regras de colisao (nunca duplicar pessoa,
    nunca sobrescrever dado do dono):
    - Match contra os drivers existentes por NOME (sem acento/caixa —
      'Marcia' de prod casa 'Márcia' da lista) OU por TELEFONE
      (`telefone_chave` colapsa +55/9o digito).
    - Existente SEM telefone ganha o telefone da lista; existente COM
      telefone (mesmo divergente) fica intocado — inclusive `ativo`.
    - So driver realmente novo e criado (ativo, token proprio, capacidade
      default do modelo), igual ao POST /entregas/api/drivers.
    Best-effort: falhar aqui nunca derruba o startup.
    """
    import secrets as _secrets
    import unicodedata as _ud
    try:
        from app.models import AppConfig, Driver
        from app.utils import telefone_chave
        chave = 'seed_drivers_entrega_2026_08'
        if AppConfig.get(chave):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        existentes = Driver.query.all()
        por_nome = {_norm(d.nome): d for d in existentes}
        por_fone = {telefone_chave(d.telefone): d for d in existentes
                    if telefone_chave(d.telefone)}
        criados, preenchidos, intocados = [], [], []
        for nome, fone in _SEED_DRIVERS_2026_08:
            d = por_nome.get(_norm(nome)) or por_fone.get(telefone_chave(fone))
            if d is not None:
                if not (d.telefone or '').strip():
                    d.telefone = fone
                    preenchidos.append(d.nome)
                else:
                    intocados.append(d.nome)
                continue
            novo = Driver(nome=nome, telefone=fone, ativo=True,
                          token=_secrets.token_urlsafe(16))
            db.session.add(novo)
            # Indexa o recem-criado: colisao DENTRO da propria lista (ou
            # numa segunda execucao sem marker) tambem nao duplica.
            por_nome[_norm(nome)] = novo
            k = telefone_chave(fone)
            if k:
                por_fone[k] = novo
            criados.append(nome)
        AppConfig.set(chave, f'criados={len(criados)}')
        db.session.commit()
        logger.info('seed drivers: %d criado(s) %s | telefone preenchido: '
                    '%s | ja existiam: %s', len(criados), criados,
                    preenchidos, intocados)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed drivers): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_cores_drivers(app):
    """Dá cor ÚNICA a cada motorista (dono 09/08/2026, manhã do Dia dos
    Pais: "Preciso que as cores nao sejam repetidas"). UMA vez, marcado em
    AppConfig: cor já distinta fica como está (primeiro dono da cor mantém);
    NULL/vazia/repetida ganha a próxima cor livre da paleta
    (`entregas.routes.PALETA_DRIVERS` — a mesma que o cadastro novo usa).
    Depois disso a tela de Drivers manda. Best-effort."""
    try:
        from app.blueprints.entregas.routes import cor_driver_livre
        from app.models import AppConfig, Driver
        chave = 'seed_cores_drivers_2026_08'
        if AppConfig.get(chave):
            return
        drivers = Driver.query.order_by(Driver.id).all()
        # 1º passe: registra TODAS as cores distintas existentes (primeiro
        # dono por id mantém) — sem isso, um driver SEM cor pegaria da
        # paleta a cor que um dono legítimo já usa e expulsaria o dono
        # (pego por teste).
        usadas = set()
        donos = set()
        for d in drivers:
            c = (d.cor or '').strip().lower()
            if c and c not in usadas:
                usadas.add(c)
                donos.add(d.id)
        # 2º passe: NULL/vazia/repetida ganha a próxima cor livre.
        trocados = 0
        for d in drivers:
            if d.id in donos:
                continue
            nova = cor_driver_livre(usadas)
            d.cor = nova
            usadas.add(nova.lower())
            trocados += 1
        AppConfig.set(chave, f'trocados={trocados}')
        db.session.commit()
        logger.info('seed cores drivers: %d cor(es) atribuida(s)', trocados)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed cores drivers): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# Danishes ASSADAS que toda loja recebe todo dia (dono 17/08/2026: "coloque
# automaticamente em cada loja por dia para elas receberem assado 2 danishes
# ASSADOS de cada (calabresa, queijo branco, mucarela de bufala, alho poro e
# maça). Dependendo se vendeu 2 ou mais por dia... deveria pedir a mais").
# O gesto vira ESTOQUE MINIMO por (loja, receita): o motor venda+estoque usa
# alvo = max(media de venda, minimo) - estoque — repoe o colchao de 2 todo
# dia E pede a mais sozinho quando a venda passa do piso. As 5 receitas ja
# tem estado_padrao='assado' (conferido em prod 17/08) — a linha do pedido
# sai "(assado)" sem precisar de nada.
SEED_MINIMO_DANISH = {
    'chave': 'seed_minimo_danish_2026_08',
    # v2 (mesmo dia): "as lojas devem receber 2 danishes desses por dia
    # IMPRETERIVELMENTE" — o colchao de estoque (v1) virou o piso
    # INCONDICIONAL `pedido_minimo_diario` (nao desconta o estoque que
    # sobrou). O v2 converte: seta o diario=2 e LIMPA o estoque_minimo
    # que o v1 deixou em exatamente 2 (valor diferente = do dono, fica).
    'chave_v2': 'seed_minimo_danish_2026_08_v2',
    # v3: v1/v2 commitaram `setados=0` em prod (marker visto pela sonda
    # ?seeds=1) — as lojas diarias tem `dias_funcionamento` PREENCHIDO com
    # a semana inteira (a tela de lojas grava '0123456'; so vazio contava
    # como "abre todo dia" no filtro). Restrita agora = subconjunto PROPRIO
    # da semana. O v3 NAO mexe em estoque_minimo (os =2 esparsos de prod
    # sao ajuste manual do dono) e o marker guarda lojas=/receitas= pra
    # zero nunca mais passar batido.
    'chave_v3': 'seed_minimo_danish_2026_08_v3',
    'minimo': 2,
    'nomes': ('danish de calabresa', 'danish de queijo branco',
              'danish de mucarela de bufala', 'danish de alho poro',
              'danish de maca'),
}


def _seed_minimo_danish(app):
    """UMA VEZ: `estoque_loja.estoque_minimo = 2` nas 5 danishes, em cada
    loja ATIVA que abre todo dia (fora a "Industria" — Loja so pro RH — e
    fora de loja com `dias_funcionamento` restrito, ex. Cantina sab/dom:
    colchao DIARIO nao se aplica; o dono seta na mao se quiser). NUNCA
    sobrescreve minimo ja definido (> 0) — depois do seed a tela
    /pedidos/estoque-loja manda. Best-effort; marker em AppConfig."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, EstoqueLoja, Loja, Receita
        cfg = SEED_MINIMO_DANISH
        if AppConfig.get(cfg['chave']):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        alvos = set(cfg['nomes'])
        receitas = [r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) in alvos]
        lojas = [
            loja for loja in Loja.query.filter_by(ativa=True).all()
            if 'industria' not in _norm(loja.nome)
            and not (getattr(loja, 'dias_funcionamento', None) or '').strip()
        ]
        setados, mantidos = 0, 0
        for loja in lojas:
            for r in receitas:
                el = EstoqueLoja.query.filter_by(
                    loja_id=loja.id, receita_id=r.id).first()
                if el is None:
                    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                     quantidade=0)
                    db.session.add(el)
                if int(el.estoque_minimo or 0) > 0:
                    mantidos += 1          # valor do dono manda
                    continue
                el.estoque_minimo = int(cfg['minimo'])
                setados += 1
        AppConfig.set(cfg['chave'],
                      f'setados={setados} mantidos={mantidos}')
        db.session.commit()
        logger.info('seed minimo danish: %d minimo(s) setado(s) em %d '
                    'loja(s) x %d receita(s); %d ja definidos mantidos',
                    setados, len(lojas), len(receitas), mantidos)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed minimo danish): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_minimo_danish_v2(app):
    """UMA VEZ (v2, mesma tarde de 17/08/2026): converte o colchao do v1 no
    piso INCONDICIONAL — `pedido_minimo_diario = 2` nas 5 danishes das
    lojas diarias ("receber 2 por dia impreterivelmente", sem descontar o
    estoque que sobrou) e LIMPA o `estoque_minimo` que o v1 deixou em
    exatamente 2 (valor diferente = ajuste do dono, fica). Depois do seed a
    coluna "Diario" de /pedidos/estoque-loja manda. Best-effort."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, EstoqueLoja, Loja, Receita
        cfg = SEED_MINIMO_DANISH
        if AppConfig.get(cfg['chave_v2']):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        alvos = set(cfg['nomes'])
        receitas = [r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) in alvos]
        lojas = [
            loja for loja in Loja.query.filter_by(ativa=True).all()
            if 'industria' not in _norm(loja.nome)
            and not (getattr(loja, 'dias_funcionamento', None) or '').strip()
        ]
        setados, mantidos = 0, 0
        for loja in lojas:
            for r in receitas:
                el = EstoqueLoja.query.filter_by(
                    loja_id=loja.id, receita_id=r.id).first()
                if el is None:
                    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                     quantidade=0)
                    db.session.add(el)
                if int(el.estoque_minimo or 0) == int(cfg['minimo']):
                    el.estoque_minimo = None       # colchao v1 vira o diario
                if int(el.pedido_minimo_diario or 0) > 0:
                    mantidos += 1                  # valor do dono manda
                    continue
                el.pedido_minimo_diario = int(cfg['minimo'])
                setados += 1
        AppConfig.set(cfg['chave_v2'],
                      f'setados={setados} mantidos={mantidos}')
        db.session.commit()
        logger.info('seed minimo danish v2: %d piso(s) diario(s) em %d '
                    'loja(s) x %d receita(s); %d mantidos', setados,
                    len(lojas), len(receitas), mantidos)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed minimo danish v2): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _loja_abre_todo_dia(loja):
    """True se a loja abre TODO dia: `dias_funcionamento` vazio (default
    historico) OU com a semana inteira ('0123456' — e o que a tela de lojas
    grava quando os 7 checkboxes estao marcados). Restrita = subconjunto
    PROPRIO da semana (ex: Cantina '56')."""
    dias = (getattr(loja, 'dias_funcionamento', None) or '').strip()
    return not dias or set('0123456') <= set(dias)


def _seed_minimo_danish_v3(app):
    """Terceira rodada (mesma noite de 17/08/2026): v1/v2 commitaram
    `setados=0` em prod porque o filtro de loja so aceitava
    `dias_funcionamento` VAZIO — e as lojas diarias de prod tem '0123456'
    gravado pela tela. Seta `pedido_minimo_diario = 2` nas 5 danishes das
    lojas que ABREM TODO DIA (`_loja_abre_todo_dia`; Industria fora),
    nunca sobrescrevendo piso ja definido e SEM tocar em estoque_minimo
    (os =2 esparsos de prod sao ajuste manual do dono). O marker guarda
    lojas=/receitas= — `setados=0` nunca mais passa batido."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, EstoqueLoja, Loja, Receita
        cfg = SEED_MINIMO_DANISH
        if AppConfig.get(cfg['chave_v3']):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        alvos = set(cfg['nomes'])
        receitas = [r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) in alvos]
        lojas = [
            loja for loja in Loja.query.filter_by(ativa=True).all()
            if 'industria' not in _norm(loja.nome)
            and _loja_abre_todo_dia(loja)
        ]
        setados, mantidos = 0, 0
        for loja in lojas:
            for r in receitas:
                el = EstoqueLoja.query.filter_by(
                    loja_id=loja.id, receita_id=r.id).first()
                if el is None:
                    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                     quantidade=0)
                    db.session.add(el)
                if int(el.pedido_minimo_diario or 0) > 0:
                    mantidos += 1                  # valor do dono manda
                    continue
                el.pedido_minimo_diario = int(cfg['minimo'])
                setados += 1
        AppConfig.set(cfg['chave_v3'],
                      f'setados={setados} mantidos={mantidos} '
                      f'lojas={len(lojas)} receitas={len(receitas)}')
        db.session.commit()
        logger.info('seed minimo danish v3: setados=%d mantidos=%d em %d '
                    'loja(s) x %d receita(s)', setados, mantidos,
                    len(lojas), len(receitas))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed minimo danish v3): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# Minis sanduíches do cardápio em PDF (dono 19/08/2026: "Cadastra esses 6
# minis sanduiches no sistema e coloca eles para serem vendidos como minis
# em .../caixa-de-mini-p148 junto com os outros minis. Importar fotos e
# precos do PDF" + "Usar o mesmo codigo SKU do tiny que usamos para o mini
# croissant"). Fotos extraídas do PDF e conferidas UMA A UMA contra os
# nomes (ficam em app/seeds_data/minis_2026_08/, viram imagem_blob — o
# card de migração BLOB do /admin/debug-schema pode levá-las ao Dropbox
# depois, backfill idempotente).
MINIS_SANDUICHE_SEED = {
    'chave': 'seed_minis_sanduiche_2026_08',
    'menu_produto_id': 148,          # .../loja/caixa-de-mini-p148
    'referencia': 'mini croissant tradicional',
    'itens': (
        ('Posta de Lagarto',
         'Mini pão sourdough tradicional com posta de lagarto ao molho',
         '15.00', 'posta-de-lagarto.jpg'),
        ('Sourdough Avocado',
         'Mini sourdough tradicional com recheio de avocado com tomates '
         'picados', '22.00', 'sourdough-avocado.jpg'),
        ('Posta de Beringela',
         'Mini sourdough 7 grãos com posta de beringela', '13.00',
         'posta-de-beringela.jpg'),
        ('Lanche de Lagarto',
         'Pão francês tradicional com lagarto desfiado', '25.00',
         'lanche-de-lagarto.jpg'),
        ('Brioche Caprese',
         'Mini brioche bolinha com caprese — mussarela de búfala, tomate '
         'cereja e manjericão', '19.00', 'brioche-caprese.jpg'),
        ('Mini Croissant',
         'Mini croissant com uma fatia de peito de peru e uma fatia de '
         'queijo branco', '25.00', 'mini-croissant.jpg'),
    ),
}


def _seed_minis_sanduiche(app):
    """UMA VEZ: cadastra os 6 minis sanduíches do PDF como Receitas, cria
    o slot de cada um no menu configurável Caixa de Mini (produto 148,
    `preco_menu` do PDF, pré-seleção 0 — não muda o default de quem já
    compra) e herda o SKU do Tiny do Mini Croissant Tradicional (canal
    site, confirmado — ordem explícita do dono, NF-e automática).

    Regras da casa: só marca no SUCESSO (menu ausente = tenta no próximo
    boot); homônima ativa não duplica (mantidas); flags/medidas copiadas
    da receita de REFERÊNCIA (os minis existentes); marker com contagens
    (setados=0 nunca mais passa batido); edição futura do dono manda."""
    import os as _os
    import unicodedata as _ud
    from decimal import Decimal as _D
    try:
        from app.models import AppConfig, Produto, ProdutoItem, Receita, TinyProdutoMap
        from app.utils import agora as _agora
        cfg = MINIS_SANDUICHE_SEED
        if AppConfig.get(cfg['chave']):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        menu = db.session.get(Produto, cfg['menu_produto_id'])
        if menu is None or not getattr(menu, 'menu_configuravel', False):
            logger.warning('seed minis sanduiche: produto %s ausente ou nao '
                           'e menu configuravel — re-tenta no proximo boot',
                           cfg['menu_produto_id'])
            return

        ref = next((r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) == cfg['referencia']), None)
        sku_ref = None
        if ref is not None:
            m = TinyProdutoMap.query.filter_by(
                canal='site', kind='receita', item_id=ref.id).first()
            if m and (m.tiny_sku or '').strip():
                sku_ref = m.tiny_sku.strip()

        ativas = {_norm(r.nome): r for r in Receita.query
                  .filter(Receita.arquivada_em.is_(None)).all()}
        slots = {}
        for pi in ProdutoItem.query.filter_by(produto_id=menu.id).all():
            if pi.receita_id:
                slots[pi.receita_id] = pi

        base_dir = _os.path.join(_os.path.dirname(__file__),
                                 'seeds_data', 'minis_2026_08')
        criadas = mantidas = novos_slots = skus = fotos = 0
        for nome, desc, preco, arquivo in cfg['itens']:
            r = ativas.get(_norm(nome))
            if r is None:
                r = Receita(
                    nome=nome, observacao=desc,
                    categoria=(ref.categoria if ref else 'Minis'),
                    rendimento_qtd=(ref.rendimento_qtd if ref else 1),
                    rendimento_unidade=(ref.rendimento_unidade
                                        if ref else 'un'),
                    peso_base=(ref.peso_base if ref else 100.0),
                    estado_padrao=(ref.estado_padrao if ref else 'assado'),
                    sob_encomenda=(ref.sob_encomenda if ref else True),
                )
                caminho = _os.path.join(base_dir, arquivo)
                try:
                    with open(caminho, 'rb') as f:
                        r.imagem_blob = f.read()
                    r.imagem_mimetype = 'image/jpeg'
                    fotos += 1
                except OSError:
                    logger.warning('seed minis: foto %s ausente', arquivo)
                db.session.add(r)
                db.session.flush()
                criadas += 1
            else:
                mantidas += 1
            if r.id not in slots:
                db.session.add(ProdutoItem(
                    produto_id=menu.id, tipo='receita', receita_id=r.id,
                    item_nome=r.nome, quantidade=0,
                    preco_menu=_D(preco)))
                novos_slots += 1
            if sku_ref:
                m = TinyProdutoMap.query.filter_by(
                    canal='site', kind='receita', item_id=r.id).first()
                if m is None:
                    db.session.add(TinyProdutoMap(
                        canal='site', kind='receita', item_id=r.id,
                        tiny_sku=sku_ref,
                        tiny_nome='(herdado do Mini Croissant Tradicional)',
                        confirmado_em=_agora()))
                    skus += 1
        AppConfig.set(cfg['chave'],
                      f'criadas={criadas} mantidas={mantidas} '
                      f'slots={novos_slots} skus={skus} fotos={fotos}')
        db.session.commit()
        logger.info('seed minis sanduiche: criadas=%d mantidas=%d slots=%d '
                    'skus=%d fotos=%d', criadas, mantidas, novos_slots,
                    skus, fotos)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed minis sanduiche): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_antecedencia_brioche(app):
    """UMA VEZ (dono 18/08/2026, "quero o maximo de brioche fresco nas
    lojas"): `antecedencia_max_dias = 0` no Brioche CLASSICO — assa so
    na madrugada anterior a entrega. Seed proprio com marker porque o
    backfill original (guard "coluna acabou de nascer") FALHOU em prod:
    o hook pusha cada edit e o deploy vencedor bootou com o ALTER sem o
    backfill no codigo — quando o backfill chegou, a coluna ja existia e
    o guard nunca mais abriu (mesma classe do danish v1/v2). So aplica
    onde ainda esta NULL — edicao do dono manda. Marker com contagens."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, Receita
        chave = 'seed_antecedencia_brioche_2026_08'
        if AppConfig.get(chave):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        receitas = [r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) == 'brioche']
        setados, mantidos = 0, 0
        for r in receitas:
            if getattr(r, 'antecedencia_max_dias', None) is not None:
                mantidos += 1                      # valor do dono manda
                continue
            r.antecedencia_max_dias = 0
            setados += 1
        AppConfig.set(chave, f'setados={setados} mantidos={mantidos} '
                             f'receitas={len(receitas)}')
        db.session.commit()
        logger.info('seed antecedencia brioche: setados=%d mantidos=%d '
                    'em %d receita(s)', setados, mantidos, len(receitas))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed antecedencia brioche): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _seed_minimo_cinnamon(app):
    """UMA VEZ (dono 17/08/2026: "Esqueci de falar sobre o cinnamon Roll,
    entra na mesma regra dos 2 danishes"): `pedido_minimo_diario = 2` do
    Cinnamon Roll nas lojas ativas que abrem todo dia (mesma regua do
    seed v3 das danishes — Industria fora, `_loja_abre_todo_dia`), nunca
    sobrescrevendo piso ja definido. So o 'Cinnamon Roll' CLASSICO — o
    'Cinnamon Roll Doce de leite' fica fora (o dono citou um; o match e
    por nome normalizado EXATO). Como a regra e "receber ASSADO" e o
    cadastro esta com `estado_padrao` vazio em prod (sonda 17/08), o seed
    tambem seta 'assado' — SO quando vazio (valor do dono manda). Marker
    com contagens (regra do v3: setados=0 nunca mais passa batido)."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, EstoqueLoja, Loja, Receita
        chave = 'seed_minimo_cinnamon_2026_08'
        if AppConfig.get(chave):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        receitas = [r for r in Receita.query
                    .filter(Receita.arquivada_em.is_(None)).all()
                    if _norm(r.nome) == 'cinnamon roll']
        lojas = [
            loja for loja in Loja.query.filter_by(ativa=True).all()
            if 'industria' not in _norm(loja.nome)
            and _loja_abre_todo_dia(loja)
        ]
        setados, mantidos, assado = 0, 0, 0
        for r in receitas:
            if not (getattr(r, 'estado_padrao', None) or '').strip():
                r.estado_padrao = 'assado'
                assado += 1
            for loja in lojas:
                el = EstoqueLoja.query.filter_by(
                    loja_id=loja.id, receita_id=r.id).first()
                if el is None:
                    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                     quantidade=0)
                    db.session.add(el)
                if int(el.pedido_minimo_diario or 0) > 0:
                    mantidos += 1                  # valor do dono manda
                    continue
                el.pedido_minimo_diario = 2
                setados += 1
        AppConfig.set(chave,
                      f'setados={setados} mantidos={mantidos} '
                      f'assado={assado} lojas={len(lojas)} '
                      f'receitas={len(receitas)}')
        db.session.commit()
        logger.info('seed minimo cinnamon: setados=%d mantidos=%d '
                    'assado=%d em %d loja(s) x %d receita(s)', setados,
                    mantidos, assado, len(lojas), len(receitas))
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (seed minimo cinnamon): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# Acerto UMA VEZ (dono 18/08/2026, auditoria "granola/iogurte em POTES"):
# pedidos historicos desses itens foram lancados em POTES/LITROS (qtd 1-15)
# quando o item e medido em g/ml — o relatorio de pedidos saia ~1000x
# distorcido. Regra dada pelo dono: item "granola 1000" e em GRAMAS; as
# porcoes 100g/200g sao produtos proprios (nao entram aqui). Mapa por
# (pedido_id): (qtd_lancada, qtd_certa) — a correcao SO aplica se a qtd
# atual ainda for a lancada (dono editou no meio = valor dele manda).
# Fonte: sonda /api/claude/pedidos-itens?dias=90 em 18/08/2026; casos com
# observacao explicita ("18 litros 2cx", "9360", "3 litros") seguem a
# observacao; o resto e x1000 (pote 1kg / litro).
ACERTO_GRANOLA_PEDIDOS = {
    106: (7, 7000), 162: (15, 15000), 210: (5, 5000), 220: (1, 1000),
    239: (15, 15000), 309: (3, 3000), 376: (2, 2000), 380: (15, 15000),
    435: (15, 15000), 478: (15, 15000), 487: (4, 4000),
}
ACERTO_IOGURTE_PEDIDOS = {
    57: (1, 1000), 84: (1, 1000), 85: (1, 1000), 105: (1, 1000),
    118: (9, 9000), 126: (2, 2000), 128: (1, 1000), 130: (1, 1000),
    152: (1, 1000), 154: (1, 1000), 156: (1, 1000), 160: (1, 1000),
    162: (1, 1000), 166: (1, 1000), 172: (1, 1000), 181: (2, 2000),
    195: (1, 1000),
    203: (1, 3000),   # obs "Caixa P, 3.100 litros" — caixa de 3L
    204: (1, 1000), 206: (1, 1000), 213: (1, 1000),
    222: (1, 1000),   # obs "1 LITRO SOMENTE"
    229: (1, 3000),   # obs "pote pequeno 3L"
    230: (1, 1000), 235: (1, 1000),
    238: (1, 3000),   # obs "3l somente"
    243: (1, 1000), 244: (2, 2000), 288: (1, 1000), 309: (1, 1000),
    311: (1, 3000),   # obs "3 litros"
    319: (1, 1000), 322: (3, 3000), 335: (9, 9000), 376: (1, 1000),
    379: (3, 3000), 399: (3, 3000), 404: (9, 9000), 408: (9, 9000),
    428: (3, 3000), 435: (9, 9000), 442: (3, 3000), 460: (3, 3000),
    462: (3, 3000), 463: (3, 3000),
    489: (2, 9360),   # obs "9360"
    496: (2, 18000),  # obs "18 litros 2cx"
    500: (6, 6000),
    530: (4, 4000),   # obs "3 litros" mas qtd E recebimento confirmam 4
}
# Cestas com o componente de granola 1000x menor (mesma auditoria): a
# venda baixava quase nada do estoque a granel.
ACERTO_CESTAS_GRANOLA = [
    ('Granola 50g', 0.05, 50.0),
    ('Cesta dia das mães 2026', 0.1, 100.0),
]
IOGURTE_LOTE_PADRAO = 3000  # dono 18/08/2026: "o padrao e 3000 para iogurte"


def _seed_acerto_granola_iogurte(app):
    """UMA VEZ (dono 18/08/2026: "resolve o 2 ... 4 voce pode resolver
    tambem"): corrige as quantidades dos pedidos historicos lancados em
    potes/litros (mapas acima), conserta o fator das duas cestas e seta o
    lote/minimo do iogurte pro padrao 3000 (granola ja estava 5000/5000 —
    config do dono, intocada). Marker com contagens (regra do seed v3)."""
    import unicodedata as _ud
    try:
        from app.models import AppConfig, PedidoItem, Produto, ProdutoItem, Receita
        chave = 'acerto_granola_iogurte_2026_08'
        if AppConfig.get(chave):
            return

        def _norm(s):
            s = _ud.normalize('NFKD', s or '')
            s = ''.join(c for c in s if not _ud.combining(c))
            return ' '.join(s.casefold().split())

        def _achar_receita(nome_norm):
            return next((r for r in Receita.query.all()
                         if _norm(r.nome) == nome_norm), None)

        granola = _achar_receita('producao - granola artesanal 1000g')
        iogurte = _achar_receita('producao - iogurte caseiro 1000ml')

        corrigidos, mantidos = 0, 0
        for rec, mapa in ((granola, ACERTO_GRANOLA_PEDIDOS),
                          (iogurte, ACERTO_IOGURTE_PEDIDOS)):
            if rec is None:
                continue
            itens = (PedidoItem.query
                     .filter(PedidoItem.receita_id == rec.id,
                             PedidoItem.pedido_id.in_(list(mapa))).all())
            for it in itens:
                old, new = mapa[it.pedido_id]
                if int(it.quantidade or 0) != old:
                    mantidos += 1        # dono/loja mexeu depois — manda
                    continue
                it.quantidade = new
                if it.quantidade_recebida is not None and \
                        int(it.quantidade_recebida) == old:
                    it.quantidade_recebida = new
                corrigidos += 1

        cestas = 0
        if granola is not None:
            for nome_prod, old, new in ACERTO_CESTAS_GRANOLA:
                alvo = _norm(nome_prod)
                # join explicito: ProdutoItem tem DUAS FKs pra produto
                # (produto_id = cesta dona; produto_componente_id)
                linhas = (ProdutoItem.query
                          .join(Produto, ProdutoItem.produto_id == Produto.id)
                          .filter(ProdutoItem.receita_id == granola.id).all())
                for pi in linhas:
                    if _norm(pi.produto.nome) != alvo:
                        continue
                    if abs(float(pi.quantidade or 0) - old) > 1e-9:
                        continue         # valor mudou — dono manda
                    pi.quantidade = new
                    cestas += 1

        lote = 0
        if iogurte is not None and not iogurte.lote_pedido:
            iogurte.lote_pedido = IOGURTE_LOTE_PADRAO
            iogurte.minimo_pedido = IOGURTE_LOTE_PADRAO
            lote = 1

        AppConfig.set(chave,
                      f'corrigidos={corrigidos} mantidos={mantidos} '
                      f'cestas={cestas} lote_iogurte={lote}')
        db.session.commit()
        logger.info('seed acerto granola/iogurte: corrigidos=%d mantidos=%d '
                    'cestas=%d lote_iogurte=%d', corrigidos, mantidos,
                    cestas, lote)
    except Exception as e:  # noqa: BLE001
        logger.warning('migrate skip (acerto granola/iogurte): %s', e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _migrate_estoque_trava(app):
    """Estoque por produto: consolida duplicatas legadas e cria a trava de
    unicidade. Estado vive so no PEDIDO; o estoque (loja e industria) eh 1 linha
    por produto. Roda em qualquer dialeto (db.engine), idempotente.

    Sequencia (ordem importa): consolida ANTES de criar o indice unico — senao a
    criacao falha com duplicatas existentes. Guardada por advisory lock no
    Postgres (exec unica entre workers) e por checagem de existencia do indice.
    """
    from sqlalchemy import text
    is_pg = 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']

    def _trava_existe():
        try:
            with db.engine.connect() as c:
                q = ("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_estoque_loja_receita'"
                     if is_pg else
                     "SELECT 1 FROM sqlite_master WHERE type='index' "
                     "AND name='uq_estoque_loja_receita'")
                return c.execute(text(q)).first() is not None
        except Exception:
            return False

    if _trava_existe():
        return

    ddls = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_receita "
        "ON estoque_loja(loja_id, receita_id) WHERE receita_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_produto "
        "ON estoque_loja(loja_id, produto_id) WHERE produto_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_mp "
        "ON estoque_loja(loja_id, materia_prima_id) WHERE materia_prima_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_producao_receita "
        "ON estoque_producao(receita_id) WHERE receita_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_producao_produto "
        "ON estoque_producao(produto_id) WHERE produto_id IS NOT NULL",
    )

    lock_conn = None
    try:
        if is_pg:
            lock_conn = db.engine.connect()
            # 7740 RESERVADO pra este lock de migracao de schema — nao reusar em
            # job/cron (colisao faz o deploy PULAR a migracao em silencio).
            if not lock_conn.execute(text('SELECT pg_try_advisory_lock(7740)')).scalar():
                lock_conn.close()
                return  # outro worker esta aplicando — sai sem erro
            if _trava_existe():  # outro worker terminou enquanto esperavamos
                lock_conn.execute(text('SELECT pg_advisory_unlock(7740)'))
                lock_conn.close()
                return

        from app.services.estoque_helpers import consolidar_estoque_duplicado
        n_loja, n_prod = consolidar_estoque_duplicado()
        db.session.commit()
        logger.warning('Estoque consolidado por produto: %s item(ns) de loja, '
                       '%s de producao.', n_loja, n_prod)
        for ddl in ddls:
            with db.engine.connect() as c:
                c.execute(text(ddl))
                c.commit()
        logger.warning('Trava de unicidade de estoque criada.')
    except Exception as e:
        db.session.rollback()
        logger.exception('consolidacao+trava de estoque falhou: %s', e)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text('SELECT pg_advisory_unlock(7740)'))
            except Exception:
                pass
            lock_conn.close()


def _migrate_postgres(app):
    """Adiciona colunas novas no PostgreSQL. Cada ALTER em commit isolado
    para que falhas pontuais não abortem migrations seguintes."""
    import logging

    from sqlalchemy import text
    log = logging.getLogger(__name__)

    def _try(stmt):
        """Executa um DDL em sub-conexão isolada com commit imediato."""
        try:
            with db.engine.connect() as c:
                c.execute(text(stmt))
                c.commit()
        except Exception as e:
            log.warning('migrate skip (%s): %s', stmt[:60], e)

    def _cols(table):
        try:
            with db.engine.connect() as c:
                r = c.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"), {'t': table})
                return {row[0] for row in r}
        except Exception:
            return set()

    with db.engine.connect() as conn:
        # Verificar e adicionar colunas faltantes em receita
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita'"
        ))
        colunas = {row[0] for row in result}

        migrações_receita = {
            'perda_percentual': 'ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0',
            'preco_loja': 'ALTER TABLE receita ADD COLUMN preco_loja REAL',
            'preco_site': 'ALTER TABLE receita ADD COLUMN preco_site REAL',
            'preco_interno': 'ALTER TABLE receita ADD COLUMN preco_interno REAL',
            'custo_embalagem': 'ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0',
            'modo_preparo': 'ALTER TABLE receita ADD COLUMN modo_preparo TEXT',
            'observacao': 'ALTER TABLE receita ADD COLUMN observacao TEXT',
            'reaproveitavel': 'ALTER TABLE receita ADD COLUMN reaproveitavel BOOLEAN NOT NULL DEFAULT FALSE',
            'familia': 'ALTER TABLE receita ADD COLUMN familia VARCHAR(30)',
            'estado_padrao': 'ALTER TABLE receita ADD COLUMN estado_padrao VARCHAR(20)',
            'dias_producao': 'ALTER TABLE receita ADD COLUMN dias_producao INTEGER NOT NULL DEFAULT 0',
            'capacidade_amassadeira_g': 'ALTER TABLE receita ADD COLUMN capacidade_amassadeira_g INTEGER NOT NULL DEFAULT 50000',
            'sugerir_pedido_loja': 'ALTER TABLE receita ADD COLUMN sugerir_pedido_loja BOOLEAN NOT NULL DEFAULT TRUE',
            'lote_pedido': 'ALTER TABLE receita ADD COLUMN lote_pedido INTEGER',
            'minimo_pedido': 'ALTER TABLE receita ADD COLUMN minimo_pedido INTEGER',
            # Estoque minimo da INDUSTRIA (freezer) por receita: piso da
            # previsao de producao — o alvo do dia nunca cai abaixo dele
            # (previsao_producao.balanco_industria). Vazio = sem piso.
            'estoque_minimo_industria': ('ALTER TABLE receita ADD COLUMN '
                                         'estoque_minimo_industria INTEGER'),
            # Lote SO da producao (placa de focaccia = 8 pedacos): cronograma
            # produz em multiplos; pedido de loja segue livre. Vazio herda
            # lote_pedido.
            'lote_producao': 'ALTER TABLE receita ADD COLUMN lote_producao INTEGER',
            'fornada_especial': 'ALTER TABLE receita ADD COLUMN fornada_especial BOOLEAN NOT NULL DEFAULT FALSE',
            # Devolucao loja->industria (croissant almond): sobras devolvidas
            # desta receita CREDITAM a receita apontada (ex: Croissant
            # Tradicional -> "Croissant Tradicional — Retorno"). NULL = credita
            # a propria. Retorno vira receita SEPARADA porque o estoque da
            # industria e 1 linha por receita (uq_estoque_producao_receita) e o
            # retornado (assado, de vespera) nao pode se misturar com o
            # congelado cru que atende pedidos das lojas.
            'retorno_receita_id': 'ALTER TABLE receita ADD COLUMN retorno_receita_id INTEGER REFERENCES receita(id)',
            # Sub-receita que ENTRA NA AMASSADEIRA quando consumida por outra
            # ficha (ex.: Levain (pé) nos sourdoughs) — a cascata da massa
            # base conta/mostra em gramas, ao contrário das subs de montagem
            # (Massa para folhar nos Danish). 15/07/2026.
            'sub_na_amassadeira': ('ALTER TABLE receita ADD COLUMN '
                                   'sub_na_amassadeira BOOLEAN NOT NULL '
                                   'DEFAULT FALSE'),
            # Estoque físico desta receita NÃO abate a produção sugerida
            # (balanço + MRP do cronograma). Decisão do dono 19/07/2026,
            # caso Massa para folhar: o ledger dizia 2 bolas que não
            # existiam na geladeira e a sugestão de massa pros 300 pains
            # saía menor. Produção JÁ MANDADA (plano de hoje) segue
            # contando — só o estoque em EstoqueProducao é ignorado.
            'estoque_nao_abate': ('ALTER TABLE receita ADD COLUMN '
                                  'estoque_nao_abate BOOLEAN NOT NULL '
                                  'DEFAULT FALSE'),
            # Sob encomenda D+2 (dono 21/07/2026): venda no site so pra data
            # >= D+2; o pedido vira demanda de producao pro padeiro (estilo
            # B2B) e NAO abate prateleira (produzido pro pedido).
            'sob_encomenda': ('ALTER TABLE receita ADD COLUMN '
                              'sob_encomenda BOOLEAN NOT NULL '
                            …18808 tokens truncated…cela(fatura_id)")
    _try("ALTER TABLE cobranca ADD COLUMN IF NOT EXISTS "
         "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS uq_cobranca_fatura "
         "ON cobranca(fatura_id) WHERE fatura_id IS NOT NULL")

    # Baixa do B2B na SEPARACAO (07/07/2026, decisao do dono): o estoque
    # da industria so baixa quando o padeiro separa o pedido no /padeiro.
    # `estoque_baixado_em` marca o regime da venda: NULL = aguardando
    # separacao; preenchido = ja baixou (na separacao, ou na criacao para
    # venda imediata sem data_entrega). BACKFILL one-shot: toda venda ATIVA
    # existente baixou na criacao (regime antigo) — sem o backfill, a
    # separacao pos-deploy baixaria EM DOBRO. Cancelada fica NULL (estorno
    # ja devolveu tudo; reabrir cai no regime novo).
    _cols_vb2b_antes = _cols('venda_b2b')
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "estoque_baixado_em TIMESTAMP")
    if _cols_vb2b_antes and 'estoque_baixado_em' not in _cols_vb2b_antes:
        _try("UPDATE venda_b2b SET estoque_baixado_em = "
             "COALESCE(criado_em, NOW()) "
             "WHERE estoque_baixado_em IS NULL AND status = 'ativa'")

    # Aprovar orcamento vira venda (07/07/2026): vinculo persistido evita
    # converter o mesmo orcamento duas vezes (duplicaria fila do padeiro
    # e, na separacao, a baixa).
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS "
         "venda_id INTEGER REFERENCES venda_b2b(id)")

    # Rascunho arquivavel (08/07/2026, pedido do dono): rascunho que nao
    # foi pra frente sai de Pendentes sem virar 'recusado' (que significa
    # cliente disse nao). Mesmo idioma do Receita.arquivada_em.
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS "
         "arquivado_em TIMESTAMP")

    # Mapeamento de SKU do Tiny POR CANAL (06/07/2026): no Tiny o B2B eh
    # outro cadastro/lista de preco — o mesmo item nosso pode ter SKU
    # diferente por canal ('site' | 'b2b'). Linhas existentes viram 'site'
    # (era o unico canal). A unique (kind, item_id) vira
    # (canal, kind, item_id).
    _try("ALTER TABLE tiny_produto_map ADD COLUMN IF NOT EXISTS "
         "canal VARCHAR(10) NOT NULL DEFAULT 'site'")
    _try("ALTER TABLE tiny_produto_map DROP CONSTRAINT IF EXISTS "
         "uq_tiny_map_item")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS uq_tiny_map_canal_item "
         "ON tiny_produto_map(canal, kind, item_id)")

    # ── Acuracia por ANTECEDENCIA de verdade (11/07/2026, aprovado dono) ──
    # A unique (data_alvo, loja, receita, motor) guardava so a PRIMEIRA
    # previsao vista pra cada data — a tabela "por lead" da acuracia
    # comparava leads diferentes vindos de datas diferentes. A unique passa
    # a incluir lead_dias: o cron congela 1 snapshot POR ANTECEDENCIA
    # (D-6..D-0) da mesma data. Procedimento de 2 commits: este ALTER
    # deploya ANTES do codigo que insere por lead — a unique velha
    # rejeitaria os inserts novos.
    _try("ALTER TABLE previsao_snapshot DROP CONSTRAINT IF EXISTS "
         "uq_previsao_snapshot_alvo_motor")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS "
         "uq_previsao_snapshot_alvo_motor_lead ON "
         "previsao_snapshot(data_alvo, loja_id, receita_id, motor, "
         "lead_dias)")

    # ── Restauracao do incidente "M6 Commit D" (12/07/2026) ──
    # Outra sessao dropou as colunas BLOB (receita.imagem_blob,
    # produto.imagem_blob, foto_recebimento.imagem, pedido_item_foto.imagem)
    # num container que BOOTOU (migrations rodam no startup) mas NUNCA foi
    # promovido (deploy falhou) — o codigo no ar continuou sendo o antigo,
    # que seleciona essas colunas: todo SELECT nesses modelos virou 500.
    # Re-criamos as colunas VAZIAS: a guarda do drop so dropava com 0
    # linhas com BLOB, entao nao houve perda de dados. Se o Commit D for
    # refeito, seguir o procedimento canonico de 2 commits confirmando o
    # deploy do commit 1 pela sonda /api/claude/deploy.
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE foto_recebimento ADD COLUMN IF NOT EXISTS "
         "imagem BYTEA")
    _try("ALTER TABLE pedido_item_foto ADD COLUMN IF NOT EXISTS "
         "imagem BYTEA")

    # Aniversário do cliente do site (11/07/2026, portal Wi-Fi da Ribeiro
    # do Vale): dia/mês pra campanha de aniversário, ano OPCIONAL (LGPD —
    # minimização; o form só obriga dia/mês). Nullable: contas antigas não
    # têm. Commit 1 do procedimento de 2 commits — o modelo Cliente só
    # ganha as colunas depois deste ALTER estar aplicado em prod.
    _try("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS "
         "aniversario_dia INTEGER")
    _try("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS "
         "aniversario_mes INTEGER")
    _try("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS "
         "nascimento_ano INTEGER")

    # Portal do funcionário (24/07/2026): liga o cadastro de RH (Funcionario)
    # à conta de login (Usuario) por e-mail — hoje são universos separados.
    # Essa conta serve o módulo de TREINAMENTO agora e o holerite/avisos
    # depois. ALTER vai NA FRENTE do modelo (procedimento de 2 commits —
    # CLAUDE.md "Schema migrations"): commit 1 = este ALTER + deploy
    # confirmado; commit 2 = coluna no modelo Funcionario.
    _try("ALTER TABLE funcionario ADD COLUMN IF NOT EXISTS "
         "usuario_id INTEGER REFERENCES usuario(id)")
    _try("CREATE INDEX IF NOT EXISTS ix_funcionario_usuario "
         "ON funcionario(usuario_id)")

    # Backfill de tokens em drivers existentes (sem token)
    try:
        import secrets

        from app.models import Driver
        sem_token = Driver.query.filter(
            (Driver.token == None) | (Driver.token == '')  # noqa: E711
        ).all()
        for drv in sem_token:
            drv.token = secrets.token_urlsafe(16)
        if sem_token:
            db.session.commit()
    except Exception as e:
        app.logger.warning('backfill token driver falhou: %s', e)
        db.session.rollback()

    # Módulo antigo "Vídeos simples" (blueprint treinamento) REMOVIDO em
    # 24/07/2026 e substituído pelo treinamento gamificado (/treino). O dono
    # autorizou explicitamente o DROP das 6 tabelas antigas ("Pode destruir").
    # CASCADE resolve as FKs internas (pergunta→treinamento, opcao→pergunta,
    # tentativa/conclusao/progresso→treinamento). Idempotente (IF EXISTS).
    for _t in ('treinamento_progresso', 'treinamento_conclusao',
               'treinamento_tentativa', 'treinamento_opcao',
               'treinamento_pergunta', 'treinamento'):
        _try(f"DROP TABLE IF EXISTS {_t} CASCADE")

    # ── Menu degustação CONFIGURÁVEL no site (26/07/2026, pedido do dono) ──
    # Cesta cujo cliente escolhe as quantidades de cada componente, com
    # total FIXO ("30 minis, quais você quiser") e teto por componente. O
    # preço é a SOMA do preço por unidade de cada mini escolhido (decisão do
    # dono: "cadastrar preço por mini") — por isso `preco_menu` mora no
    # ProdutoItem (preço DENTRO deste menu), não na Receita: os minis não
    # são vendidos avulsos na vitrine e um `preco_site` neles os publicaria.
    # `ProdutoItem.quantidade` (já existente) continua sendo a PRÉ-SELEÇÃO.
    # Commit 1 do procedimento de 2 commits (CLAUDE.md "Schema migrations"):
    # este ALTER deploya ANTES do modelo, confirmado por /api/claude/deploy.
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS "
         "menu_configuravel BOOLEAN NOT NULL DEFAULT FALSE")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS "
         "menu_total_unidades INTEGER")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS "
         "menu_max_por_item INTEGER")
    _try("ALTER TABLE produto_item ADD COLUMN IF NOT EXISTS "
         "preco_menu NUMERIC(10, 2)")

    # ── Setor no item de checklist de loja (03/08/2026) ──
    # O checklist em papel do dono é organizado por SETOR (Café/Barista,
    # Chapa, Cozinha, Câmara Fria, Caixa, Salão, Limpeza, Área Externa,
    # Escritório e Forno, Supervisão da Loja) — 11 folhas, ~169 pontos.
    # Ele escolheu preencher TUDO NUMA TELA, agrupado por setor: o setor é
    # SUBTÍTULO, não navegação. Sem esta coluna o agrupamento viraria
    # prefixo no texto ("CAFÉ — Ligar máquina…"), que sujaria também o
    # snapshot histórico das respostas. NULL = item sem setor (o que o
    # cadastro manual cria por padrão) — cai no grupo "Geral".
    # Commit 1 do procedimento de 2 commits: este ALTER deploya ANTES do
    # modelo, confirmado por /api/claude/deploy. A TABELA já existe em prod
    # (criada por db.create_all no deploy de 03/08); `IF NOT EXISTS` no
    # ALTER e o _try cobrem o caso de ela ainda não existir.
    _try("ALTER TABLE checklist_item_modelo ADD COLUMN IF NOT EXISTS "
         "setor VARCHAR(60)")

    # ── Descadastro de marketing no Cliente (05/08/2026) ──
    # E-mail marketing (Listmonk no VPS): decisão do dono é OPT-OUT — a base
    # inteira (quem comprou no site E quem usou o Wi-Fi das lojas) recebe
    # campanha, e quem clicar em "cancelar inscrição" para de receber.
    # NULL = recebe; preenchido = cancelou (e a data fica registrada, que é o
    # que prova o respeito ao pedido caso alguém questione).
    # Refletir o descadastro DE VOLTA aqui é obrigatório: sem isso a próxima
    # sincronização re-inscreveria quem acabou de cancelar — a forma mais
    # rápida de virar spam e queimar o domínio.
    _try("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS "
         "marketing_descadastro_em TIMESTAMP")

    # ── Origem do cadastro do cliente (05/08/2026) ──
    # Bug real: a lista "Wi-Fi das lojas" mostrava UMA pessoa. O caminho vivo
    # do portal (modo RADIUS, 13/07/2026) é `wifi_portal.criar_conta_direta`,
    # que cria SÓ o `Cliente` — quem deixa `WifiPortalSessao` é o fluxo ANTIGO
    # (validação por WhatsApp). Derivar "veio do Wi-Fi" da sessão enxergava,
    # então, uma fração mínima da base.
    # A marca passa a ser explícita ('site' | 'wifi' | 'balcao' | NULL).
    if 'origem' not in _cols('cliente'):
        _try("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS "
             "origem VARCHAR(20)")
        # Backfill ÚNICO (só na criação da coluna): quem tem sessão do portal
        # OU aniversário preenchido veio do Wi-Fi — os dois formulários do
        # portal são os ÚNICOS lugares do sistema que perguntam aniversário
        # (o cadastro do site não pergunta). Depois disso quem manda é o
        # código que grava a origem na hora do cadastro.
        _try("UPDATE cliente SET origem = 'wifi' WHERE origem IS NULL AND ("
             "aniversario_dia IS NOT NULL OR EXISTS ("
             "SELECT 1 FROM wifi_portal_sessao s "
             "WHERE LOWER(s.email) = LOWER(cliente.email)))")

    # Bloqueio de ITENS por data especial (07/08/2026, caso Caixa de Mini
    # vendida pro Dia dos Pais — dono: "os clientes nao poderiam comprar os
    # minis para o dia 9"). Uma linha por regra (nome de categoria ou de
    # item); NULL/vazio = sem restricao. Commit 1 do procedimento de 2
    # commits — o modelo/logica so entram depois deste ALTER estar no ar.
    if 'bloquear_itens' not in _cols('loja_data_especial'):
        _try("ALTER TABLE loja_data_especial ADD COLUMN IF NOT EXISTS "
             "bloquear_itens TEXT")

    # "Pular endereço" do motorista (08/08/2026, dono, véspera do Dia dos
    # Pais: portaria recusou → foto provando que esteve lá + o pedido vai
    # pro FIM da rota, segue pendente pra voltar depois; NÃO é nao_entregue,
    # que é desfecho final). Commit 1 do procedimento de 2 commits.
    if 'pulado_em' not in _cols('atribuicao_entrega'):
        _try("ALTER TABLE atribuicao_entrega ADD COLUMN IF NOT EXISTS "
             "pulado_em TIMESTAMP")

    # Responsável pela perda de produção (13/08/2026, dono: "escolher o
    # responsável — lista de funcionários padeiro, ajudante etc"). FK pro
    # quadro do RH (Funcionario), não pra conta logada (a TV é conta
    # compartilhada). Commit 1 do procedimento de 2 commits.
    if 'funcionario_id' not in _cols('perda_producao'):
        _try("ALTER TABLE perda_producao ADD COLUMN IF NOT EXISTS "
             "funcionario_id INTEGER")

    # Roteiro de gravação da aula do treinamento (13/08/2026): o dono mandou
    # o plano de conteúdo da "Universidade" (9 módulos, 140 aulas) em
    # planilha — cada aula vira um TreinoVideo RASCUNHO com o roteiro
    # anexado, e quem grava abre o roteiro na própria tela de admin. Só o
    # admin vê (o funcionário vê o vídeo, nunca o roteiro). Commit 1 do
    # procedimento de 2 commits.
    if 'roteiro' not in _cols('treino_video'):
        _try("ALTER TABLE treino_video ADD COLUMN IF NOT EXISTS "
             "roteiro TEXT")

    # Liderança direta e edição segura do checklist de aplicação (24/08/2026).
    # A hierarquia vive no RH; o item inativo preserva observações antigas
    # quando o proprietário atualiza o checklist de um módulo.
    if 'lider_id' not in _cols('funcionario'):
        _try("ALTER TABLE funcionario ADD COLUMN IF NOT EXISTS "
             "lider_id INTEGER REFERENCES funcionario(id) ON DELETE SET NULL")
    _try("CREATE INDEX IF NOT EXISTS ix_funcionario_lider "
         "ON funcionario(lider_id)")
    if 'ativo' not in _cols('treino_item_checklist'):
        _try("ALTER TABLE treino_item_checklist ADD COLUMN IF NOT EXISTS "
             "ativo BOOLEAN NOT NULL DEFAULT TRUE")


def _migrate_sqlite(app):
    """Adiciona colunas novas no SQLite."""
    import sqlite3
    uri = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    conn = sqlite3.connect(uri)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(receita)")
    colunas = [row[1] for row in cursor.fetchall()]
    if 'descricao_seo' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN descricao_seo TEXT")
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod = [row[1] for row in cursor.fetchall()]
    if 'descricao_seo' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN descricao_seo TEXT")
    # receita_etapa.descricao — passo-a-passo do padeiro por etapa (14/07/2026).
    cursor.execute("PRAGMA table_info(receita_etapa)")
    cols_re = [row[1] for row in cursor.fetchall()]
    if cols_re and 'descricao' not in cols_re:
        cursor.execute("ALTER TABLE receita_etapa ADD COLUMN descricao TEXT")
    # receita.sub_na_amassadeira — sub-receita que entra na amassadeira
    # (Levain (pé)); backfill único junto com a criação (15/07/2026).
    if 'sub_na_amassadeira' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN sub_na_amassadeira "
                       "BOOLEAN NOT NULL DEFAULT 0")
        cursor.execute("UPDATE receita SET sub_na_amassadeira = 1 "
                       "WHERE nome = 'Levain (pé)'")
    # receita.estoque_nao_abate — estoque físico não abate a produção
    # sugerida (dono 19/07/2026, caso Massa para folhar); backfill único da
    # flag + correção da ficha do croissant (86 g = 1,2011 bola/batida de
    # 50, guard em 1.0) junto com a criação, espelho do bloco Postgres.
    if 'estoque_nao_abate' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN estoque_nao_abate "
                       "BOOLEAN NOT NULL DEFAULT 0")
        cursor.execute("UPDATE receita SET estoque_nao_abate = 1 "
                       "WHERE nome = 'Massa para folhar'")
        cursor.execute(
            "UPDATE receita_ingrediente SET porcentagem = 1.2011 "
            "WHERE receita_id = (SELECT id FROM receita "
            "                    WHERE nome = 'Croissant Tradicional' "
            "                    ORDER BY id LIMIT 1) "
            "  AND sub_receita_id = (SELECT id FROM receita "
            "                        WHERE nome = 'Massa para folhar' "
            "                        ORDER BY id LIMIT 1) "
            "  AND porcentagem = 1.0")
    # receita.sob_encomenda — venda sob encomenda D+2 (dono 21/07/2026);
    # espelho do bloco Postgres.
    if 'sob_encomenda' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN sob_encomenda "
                       "BOOLEAN NOT NULL DEFAULT 0")
    # receita.antecedencia_max_dias — antecedencia do nivelador POR
    # receita (dono 18/08/2026, brioche fresco); espelho do Postgres,
    # backfill unico do Brioche classico em 0.
    if 'antecedencia_max_dias' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "antecedencia_max_dias INTEGER")
        cursor.execute("UPDATE receita SET antecedencia_max_dias = 0 "
                       "WHERE nome = 'Brioche'")
    # receita.cobra_sobra_diaria — cobrança de sobra POR ITEM (01/08/2026,
    # caso croissant tradicional); backfill único na criação, espelho do
    # bloco Postgres (COBRA_SOBRA_SEED).
    if 'cobra_sobra_diaria' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN cobra_sobra_diaria "
                       "BOOLEAN NOT NULL DEFAULT 0")
        for _nome in COBRA_SOBRA_SEED:
            cursor.execute("UPDATE receita SET cobra_sobra_diaria = 1 "
                           "WHERE nome = ?", (_nome,))
    # receita.descricao_atacado — descrição sincera do cardápio atacado
    # (dono 20/07/2026); backfill único junto com a criação, espelho do
    # bloco Postgres (DESCRICOES_ATACADO_SEED).
    if 'descricao_atacado' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN descricao_atacado TEXT")
        for _nome, _desc in DESCRICOES_ATACADO_SEED:
            cursor.execute(
                "UPDATE receita SET descricao_atacado = ? "
                "WHERE nome = ? AND descricao_atacado IS NULL",
                (_desc, _nome))
    if 'perda_percentual' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0")
    if 'preco_loja' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_loja REAL")
    if 'preco_site' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_site REAL")
    if 'preco_interno' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_interno REAL")
    if 'custo_embalagem' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0")
    if 'imagem_url' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_url VARCHAR(400)")
    if 'imagem_blob' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_blob BLOB")
    if 'imagem_mimetype' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_mimetype VARCHAR(50)")
    if 'arquivada_em' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN arquivada_em TIMESTAMP")
    if 'arquivada_por_id' not in colunas:
        cursor.execute(
            "ALTER TABLE receita ADD COLUMN arquivada_por_id INTEGER REFERENCES usuario(id)")
    if 'dias_producao' not in colunas:
        cursor.execute(
            "ALTER TABLE receita ADD COLUMN dias_producao INTEGER NOT NULL DEFAULT 0")
    if 'capacidade_amassadeira_g' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "capacidade_amassadeira_g INTEGER NOT NULL DEFAULT 50000")
    if 'sugerir_pedido_loja' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "sugerir_pedido_loja BOOLEAN NOT NULL DEFAULT 1")
    if 'lote_pedido' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN lote_pedido INTEGER")
    if 'minimo_pedido' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN minimo_pedido INTEGER")
    if 'estoque_minimo_industria' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "estoque_minimo_industria INTEGER")
    if 'lote_producao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN lote_producao INTEGER")
    if 'fornada_especial' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "fornada_especial BOOLEAN NOT NULL DEFAULT 0")
    if 'retorno_receita_id' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "retorno_receita_id INTEGER REFERENCES receita(id)")

    cursor.execute("PRAGMA table_info(conta_pagar_item_map)")
    cols_cpim = [row[1] for row in cursor.fetchall()]
    if cols_cpim and 'produto_id' not in cols_cpim:
        cursor.execute("ALTER TABLE conta_pagar_item_map ADD COLUMN "
                       "produto_id INTEGER REFERENCES produto(id)")

    # funcionario.usuario_id — vínculo RH <-> conta de login (Portal do
    # funcionário, 24/07/2026). Espelho do bloco Postgres.
    cursor.execute("PRAGMA table_info(funcionario)")
    cols_func = [row[1] for row in cursor.fetchall()]
    if cols_func and 'usuario_id' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN "
                       "usuario_id INTEGER REFERENCES usuario(id)")

    # mov_estoque_loja.desperdicio_id — mesma migracao do _migrate_postgres:
    # liga a baixa ao Desperdicio pra exclusao com estorno exato.
    cursor.execute("PRAGMA table_info(mov_estoque_loja)")
    cols_mel = [row[1] for row in cursor.fetchall()]
    if cols_mel and 'desperdicio_id' not in cols_mel:
        cursor.execute("ALTER TABLE mov_estoque_loja ADD COLUMN "
                       "desperdicio_id INTEGER REFERENCES desperdicio(id)")

    # retirada_sobra_item.quantidade_coletada — conferencia do motorista na
    # coleta (mesma migracao do _migrate_postgres).
    cursor.execute("PRAGMA table_info(retirada_sobra_item)")
    cols_rsi = [row[1] for row in cursor.fetchall()]
    if cols_rsi and 'quantidade_coletada' not in cols_rsi:
        cursor.execute("ALTER TABLE retirada_sobra_item ADD COLUMN "
                       "quantidade_coletada INTEGER")

    cursor.execute("PRAGMA table_info(vigia_veredito)")
    cols_vv = [row[1] for row in cursor.fetchall()]
    if cols_vv and 'tools_usadas' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN tools_usadas TEXT")

    # seru_loja_map.seru_company_id — mesma migracao do _migrate_postgres
    # (ancora estavel do vinculo; renome no Seru so atualiza o rotulo).
    cursor.execute("PRAGMA table_info(seru_loja_map)")
    cols_slm = [row[1] for row in cursor.fetchall()]
    if cols_slm and 'seru_company_id' not in cols_slm:
        cursor.execute("ALTER TABLE seru_loja_map ADD COLUMN "
                       "seru_company_id VARCHAR(64)")
    if cols_slm and 'seru_company_document' not in cols_slm:
        cursor.execute("ALTER TABLE seru_loja_map ADD COLUMN "
                       "seru_company_document VARCHAR(20)")
    if cols_vv and 'reconhecido_em' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN reconhecido_em TIMESTAMP")
    if cols_vv and 'reconhecido_por_id' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN "
                       "reconhecido_por_id INTEGER REFERENCES usuario(id)")

    # Priority fee (gorjeta) da Lalamove — coluna nova em lalamove_entrega.
    # Em SQLite novo, create_all ja cria com a coluna; aqui cobre bancos
    # locais antigos. Guarda contra a tabela nao existir ainda.
    cursor.execute("PRAGMA table_info(lalamove_entrega)")
    cols_lala = [row[1] for row in cursor.fetchall()]
    if cols_lala and 'priority_fee' not in cols_lala:
        cursor.execute("ALTER TABLE lalamove_entrega ADD COLUMN "
                       "priority_fee NUMERIC(10, 2)")

    # Email do usuario (envio de senha/convite via Postmark, 16/06/2026).
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user = [row[1] for row in cursor.fetchall()]
    if cols_user and 'email' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN email VARCHAR(200)")
    # Senha provisoria + acesso so treinamento (23/07/2026). Commit 1 (schema).
    if cols_user and 'senha_provisoria' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN "
                       "senha_provisoria BOOLEAN DEFAULT 0")
    if cols_user and 'somente_treino' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN "
                       "somente_treino BOOLEAN DEFAULT 0")

    # Migração tabela receita_ingrediente
    cursor.execute("PRAGMA table_info(receita_ingrediente)")
    cols_ing = [row[1] for row in cursor.fetchall()]
    if cols_ing and 'tipo' not in cols_ing:
        cursor.execute("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'")
    # FK pra sub-receita (espelha o Postgres) + backfill por nome (idempotente).
    if cols_ing and 'sub_receita_id' not in cols_ing:
        cursor.execute("ALTER TABLE receita_ingrediente ADD COLUMN sub_receita_id INTEGER")
    cursor.execute("""
        UPDATE receita_ingrediente SET sub_receita_id = (
            SELECT r.id FROM receita r
            WHERE lower(trim(r.nome)) = lower(trim(receita_ingrediente.ingrediente_nome))
            LIMIT 1)
        WHERE tipo = 'receita' AND sub_receita_id IS NULL
    """)

    # Migração tabela produto
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod = [row[1] for row in cursor.fetchall()]
    if cols_prod and 'custo_direto' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_direto REAL")
    if cols_prod and 'custo_embalagem' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0")
    if cols_prod and 'preco_interno' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN preco_interno REAL")
    if cols_prod and 'imagem_url' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_url VARCHAR(400)")
    if cols_prod and 'imagem_blob' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_blob BLOB")
    if cols_prod and 'imagem_mimetype' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_mimetype VARCHAR(50)")
    if cols_prod and 'modo_preparo' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN modo_preparo TEXT")
    if cols_prod and 'sob_encomenda' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN sob_encomenda "
                       "BOOLEAN NOT NULL DEFAULT 0")

    # Migração receita.modo_preparo
    if 'modo_preparo' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN modo_preparo TEXT")
    if 'observacao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN observacao TEXT")
    if 'estado_padrao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN estado_padrao VARCHAR(20)")

    # Brioche entra no pre-preparo como assado por default (idempotente).
    cursor.execute(
        "UPDATE receita SET estado_padrao='assado' "
        "WHERE LOWER(nome) LIKE '%brioche%' AND estado_padrao IS NULL"
    )

    # Migração produto.observacao
    if cols_prod and 'observacao' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN observacao TEXT")

    # Migração funcionario
    cursor.execute("PRAGMA table_info(funcionario)")
    cols_func = [row[1] for row in cursor.fetchall()]
    if cols_func and 'funcao_operacional' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)")
    if cols_func and 'periodo' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)")
    if cols_func and 'cadastro_pendente' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT 0")
    if cols_func and 'data_nascimento' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN data_nascimento DATE")
    if cols_func and 'horas_extras' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func and 'tem_cargo_confianca' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT 0")
        cursor.execute("UPDATE funcionario SET tem_cargo_confianca = 1 WHERE cargo_confianca > 0")
    if cols_func and 'cargo_id' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        cursor.execute("""
            INSERT OR IGNORE INTO cargo (nome, salario_base, ativo)
            SELECT funcao, MAX(salario_base), 1
            FROM funcionario
            WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
            GROUP BY funcao
        """)
        cursor.execute("""
            UPDATE funcionario SET cargo_id = (
                SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
            ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # Migração loja
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja = [row[1] for row in cursor.fetchall()]
    if cols_loja and 'planta_imagem' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_imagem BLOB")
    if cols_loja and 'planta_mimetype' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)")
    if cols_loja and 'pin' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN pin VARCHAR(8)")

    # Migração slot_mapa
    cursor.execute("PRAGMA table_info(slot_mapa)")
    cols_slot = [row[1] for row in cursor.fetchall()]
    if cols_slot and 'largura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15")
    if cols_slot and 'altura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8")

    # Migração usuario.loja_id
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user = [row[1] for row in cursor.fetchall()]
    if cols_user and 'loja_id' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)")

    # Migração posicao.origem
    cursor.execute("PRAGMA table_info(posicao)")
    cols_pos = [row[1] for row in cursor.fetchall()]
    if cols_pos and 'origem' not in cols_pos:
        cursor.execute("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'")
        cursor.execute(
            "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
            "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
            "  AND slot_mapa.nome = posicao.nome_posicao)"
        )

    # Migração materia_prima.estoque_atual + peso_unidade + sugerir_pedido_loja
    cursor.execute("PRAGMA table_info(materia_prima)")
    cols_mp = [row[1] for row in cursor.fetchall()]
    if cols_mp and 'estoque_atual' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0")
    if cols_mp and 'peso_unidade' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL")
    if cols_mp and 'sugerir_pedido_loja' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN sugerir_pedido_loja "
                       "BOOLEAN NOT NULL DEFAULT 0")
    if cols_mp and 'arquivada_em' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN arquivada_em TIMESTAMP")
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN arquivada_por_id INTEGER "
                       "REFERENCES usuario(id)")

    # Migração pedido_item.quantidade_recebida + materia_prima_id
    cursor.execute("PRAGMA table_info(pedido_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'quantidade_recebida' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER")
    if cols_pi and 'materia_prima_id' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    # Migração estoque_loja.materia_prima_id
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'materia_prima_id' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    # Migração usuario.is_owner
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user2 = [row[1] for row in cursor.fetchall()]
    if cols_user2 and 'is_owner' not in cols_user2:
        cursor.execute("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT 0")
        cursor.execute(
            "UPDATE usuario SET is_owner = 1 WHERE id = "
            "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)"
        )

    # Migracao papel_v1: introduz niveis. Roda uma vez.
    try:
        cursor.execute("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em DATETIME DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")
        ja_rodou = cursor.fetchone() is not None
        if not ja_rodou:
            cursor.execute("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = 0)")
            cursor.execute("UPDATE usuario SET papel='admin' WHERE is_owner = 1 AND papel <> 'admin'")
            cursor.execute("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")
    except Exception:
        pass

    # Migração projeto_area.cor
    cursor.execute("PRAGMA table_info(projeto_area)")
    cols_pa = [row[1] for row in cursor.fetchall()]
    if cols_pa and 'cor' not in cols_pa:
        cursor.execute("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    # Migração tarefa_projeto.observacao + recorrencia
    cursor.execute("PRAGMA table_info(tarefa_projeto)")
    cols_tp = [row[1] for row in cursor.fetchall()]
    if cols_tp and 'observacao' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    # estoque_producao.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_producao)")
    cols_ep = [row[1] for row in cursor.fetchall()]
    if cols_ep and 'nome_pendente' not in cols_ep:
        cursor.execute("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)")

    # estoque_loja.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'nome_pendente' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)")
    # estoque_loja.estoque_minimo — piso do pedido loja->industria por item.
    if cols_el and 'estoque_minimo' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN estoque_minimo INTEGER")
    # estoque_loja.pedido_minimo_diario — piso INCONDICIONAL do pedido do
    # dia (danishes assadas, dono 17/08/2026); nao desconta estoque.
    if cols_el and 'pedido_minimo_diario' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN pedido_minimo_diario INTEGER")
    # Reserva de estoque (21/06/2026 — race condition no cutover).
    if cols_el and 'quantidade_reservada' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN "
                       "quantidade_reservada INTEGER NOT NULL DEFAULT 0")

    cursor.execute("PRAGMA table_info(pedido_online)")
    cols_po = [row[1] for row in cursor.fetchall()]
    if cols_po and 'reserva_expira_em' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "reserva_expira_em TIMESTAMP")
    if cols_po and 'motivo_cancelamento' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "motivo_cancelamento VARCHAR(40)")
    if cols_po and 'ga_client_id' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "ga_client_id VARCHAR(64)")
    # pedido_online.divulgacao (21/07/2026): pedido de brinde/PR sem
    # pagamento — espelho do bloco Postgres.
    if cols_po and 'divulgacao' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "divulgacao BOOLEAN NOT NULL DEFAULT 0")

    # pedido_online_item.fatiado (16/07/2026): preferencia de corte do
    # sourdough. So preferencia — nao mexe em preco/estoque.
    cursor.execute("PRAGMA table_info(pedido_online_item)")
    cols_poi = [row[1] for row in cursor.fetchall()]
    if cols_poi and 'fatiado' not in cols_poi:
        cursor.execute("ALTER TABLE pedido_online_item ADD COLUMN "
                       "fatiado BOOLEAN")

    # chatbot_conversa.contato_key (19/07/2026): memoria cross-conversa do
    # bot — telefone canonizado do contato pra conversa NOVA achar o
    # historico recente do mesmo cliente. Espelha o ALTER do Postgres.
    cursor.execute("PRAGMA table_info(chatbot_conversa)")
    cols_cbc = [row[1] for row in cursor.fetchall()]
    if cols_cbc and 'contato_key' not in cols_cbc:
        cursor.execute("ALTER TABLE chatbot_conversa ADD COLUMN "
                       "contato_key VARCHAR(40)")
        cursor.execute("CREATE INDEX IF NOT EXISTS "
                       "ix_chatbot_conversa_contato_key "
                       "ON chatbot_conversa (contato_key)")

    # venda_b2b.frete_valor (20/07/2026) — ver _migrate_postgres.
    cursor.execute("PRAGMA table_info(venda_b2b)")
    cols_vb2b = [row[1] for row in cursor.fetchall()]
    if cols_vb2b and 'frete_valor' not in cols_vb2b:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN "
                       "frete_valor NUMERIC(10, 2) NOT NULL DEFAULT 0")

    # NF de transferencia industria→loja (20/07/2026) — ver _migrate_postgres.
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja = [row[1] for row in cursor.fetchall()]
    for _c, _t in (('cnpj', 'VARCHAR(20)'),
                   ('inscricao_estadual', 'VARCHAR(20)'),
                   ('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cep', 'VARCHAR(9)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)'),
                   ('razao_social', 'VARCHAR(200)')):
        if cols_loja and _c not in cols_loja:
            cursor.execute(f"ALTER TABLE loja ADD COLUMN {_c} {_t}")
    # Dias de funcionamento (27/07/2026) — ver _migrate_postgres. NULL = abre
    # todo dia; '56' = so sabado e domingo (digitos do date.weekday()).
    if cols_loja and 'dias_funcionamento' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN "
                       "dias_funcionamento VARCHAR(7)")
        # Backfill unico, so na criacao da coluna (nao sobrescreve edicao
        # futura do dono): a Cantina abre so no fim de semana.
        cursor.execute("UPDATE loja SET dias_funcionamento = '56' "
                       "WHERE LOWER(nome) LIKE '%cantina%' "
                       "  AND dias_funcionamento IS NULL")
    cursor.execute("PRAGMA table_info(pedido_loja)")
    cols_pl = [row[1] for row in cursor.fetchall()]
    for _c, _t in (('tiny_nota_fiscal_id', 'VARCHAR(40)'),
                   ('nf_status', 'VARCHAR(40)'),
                   ('nf_emitida_em', 'TIMESTAMP'),
                   ('nf_numero', 'VARCHAR(50)'),
                   ('nf_dispensada', 'BOOLEAN NOT NULL DEFAULT 0'),
                   ('nf_erro', 'TEXT')):
        if cols_pl and _c not in cols_pl:
            cursor.execute(f"ALTER TABLE pedido_loja ADD COLUMN {_c} {_t}")
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja2 = [row[1] for row in cursor.fetchall()]
    if cols_loja2 and 'nf_dispensada' not in cols_loja2:
        cursor.execute("ALTER TABLE loja ADD COLUMN "
                       "nf_dispensada BOOLEAN NOT NULL DEFAULT 0")

    # seru_produto_map.fator_quantidade
    cursor.execute("PRAGMA table_info(seru_produto_map)")
    cols_spm = [row[1] for row in cursor.fetchall()]
    if cols_spm and 'fator_quantidade' not in cols_spm:
        cursor.execute("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0")

    # conta_pagar: conferencia humana (Fase 2, 2026-06-10)
    cursor.execute("PRAGMA table_info(conta_pagar)")
    cols_cp = [row[1] for row in cursor.fetchall()]
    if cols_cp and 'revisada_em' not in cols_cp:
        cursor.execute("ALTER TABLE conta_pagar ADD COLUMN revisada_em TIMESTAMP")
    if cols_cp and 'revisada_por_id' not in cols_cp:
        cursor.execute("ALTER TABLE conta_pagar ADD COLUMN revisada_por_id INTEGER REFERENCES usuario(id)")

    # Status intermediario 'pendente' vira 'confirmado' direto (idempotente).
    try:
        cursor.execute("UPDATE pedido_loja SET status='confirmado' WHERE status='pendente'")
    except sqlite3.OperationalError:
        pass

    # Cronograma -> padeiro: alvo/produzido por item + origem do plano.
    cursor.execute("PRAGMA table_info(planejamento_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'qtd_alvo' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN qtd_alvo INTEGER")
    if cols_pi and 'produzido_qtd' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN "
                       "produzido_qtd INTEGER NOT NULL DEFAULT 0")
    if cols_pi and 'dispensada_em' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN dispensada_em TIMESTAMP")
    if cols_pi and 'dispensada_por_id' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN "
                       "dispensada_por_id INTEGER REFERENCES usuario(id)")
    if cols_pi and 'qtd_extra' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN "
                       "qtd_extra INTEGER NOT NULL DEFAULT 0")
    # Falta encerrada pelo padeiro (17/07/2026) — ver _migrate_postgres.
    if cols_pi and 'falta_encerrada_em' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN "
                       "falta_encerrada_em TIMESTAMP")
    cursor.execute("PRAGMA table_info(planejamento_producao)")
    cols_pp = [row[1] for row in cursor.fetchall()]
    if cols_pp and 'origem' not in cols_pp:
        cursor.execute("ALTER TABLE planejamento_producao ADD COLUMN origem VARCHAR(20)")
    if cols_pp and 'enviado_ao_padeiro' not in cols_pp:
        cursor.execute("ALTER TABLE planejamento_producao "
                       "ADD COLUMN enviado_ao_padeiro BOOLEAN DEFAULT 1")

    # ── Acuracia por MOTOR (Fase 0, 02/07/2026): previsao_snapshot ganha
    # motor/lead_dias e a unique passa a incluir o motor. SQLite nao dropa
    # constraint de tabela -> reconstroi (tabela de telemetria, copia barata).
    cursor.execute("PRAGMA table_info(previsao_snapshot)")
    cols_ps = [row[1] for row in cursor.fetchall()]
    if cols_ps and 'motor' not in cols_ps:
        cursor.execute("""
            CREATE TABLE previsao_snapshot_novo (
                id INTEGER PRIMARY KEY,
                data_alvo DATE NOT NULL,
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                receita_id INTEGER NOT NULL REFERENCES receita(id),
                previsto INTEGER NOT NULL DEFAULT 0,
                realizado INTEGER,
                casado_em TIMESTAMP,
                criado_em TIMESTAMP,
                motor VARCHAR(20) NOT NULL DEFAULT 'pedido_semana',
                lead_dias INTEGER,
                CONSTRAINT uq_previsao_snapshot_alvo_motor
                    UNIQUE (data_alvo, loja_id, receita_id, motor)
            )
        """)
        cursor.execute("""
            INSERT INTO previsao_snapshot_novo
                (id, data_alvo, loja_id, receita_id, previsto, realizado,
                 casado_em, criado_em)
            SELECT id, data_alvo, loja_id, receita_id, previsto, realizado,
                   casado_em, criado_em
            FROM previsao_snapshot
        """)
        cursor.execute("DROP TABLE previsao_snapshot")
        cursor.execute("ALTER TABLE previsao_snapshot_novo "
                       "RENAME TO previsao_snapshot")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_previsao_snapshot_data_alvo "
                       "ON previsao_snapshot(data_alvo)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_previsao_snapshot_criado_em "
                       "ON previsao_snapshot(criado_em)")

    # ── Caixa/piso de pedido pra MP (Fase 1, 02/07/2026) ──
    cursor.execute("PRAGMA table_info(materia_prima)")
    cols_mp2 = [row[1] for row in cursor.fetchall()]
    if cols_mp2 and 'lote_pedido' not in cols_mp2:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN lote_pedido INTEGER")
    if cols_mp2 and 'minimo_pedido' not in cols_mp2:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN minimo_pedido INTEGER")

    # ── NF-e do B2B via Tiny + endereco estruturado do cliente (06/07/2026) ──
    cursor.execute("PRAGMA table_info(venda_b2b)")
    cols_vb2b = [row[1] for row in cursor.fetchall()]
    if cols_vb2b and 'tiny_nota_fiscal_id' not in cols_vb2b:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN "
                       "tiny_nota_fiscal_id VARCHAR(40)")
    if cols_vb2b and 'nf_status' not in cols_vb2b:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN nf_status VARCHAR(40)")
    if cols_vb2b and 'nf_emitida_em' not in cols_vb2b:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN nf_emitida_em TIMESTAMP")
    cursor.execute("PRAGMA table_info(cliente_b2b)")
    cols_cb2b = [row[1] for row in cursor.fetchall()]
    for _c, _t in (('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cep', 'VARCHAR(9)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)')):
        if cols_cb2b and _c not in cols_cb2b:
            cursor.execute(f"ALTER TABLE cliente_b2b ADD COLUMN {_c} {_t}")

    # ── Fechamento mensal B2B (07/07/2026) ──
    cursor.execute("PRAGMA table_info(cliente_b2b)")
    cols_cli2 = [row[1] for row in cursor.fetchall()]
    if cols_cli2 and 'faturamento_mensal' not in cols_cli2:
        cursor.execute("ALTER TABLE cliente_b2b ADD COLUMN "
                       "faturamento_mensal BOOLEAN NOT NULL DEFAULT 0")
    cursor.execute("PRAGMA table_info(venda_b2b)")
    cols_vb = [row[1] for row in cursor.fetchall()]
    if cols_vb and 'fatura_id' not in cols_vb:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN "
                       "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    cursor.execute("PRAGMA table_info(venda_b2b_parcela)")
    cols_vp = [row[1] for row in cursor.fetchall()]
    if cols_vp and 'fatura_id' not in cols_vp:
        cursor.execute("ALTER TABLE venda_b2b_parcela ADD COLUMN "
                       "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    cursor.execute("PRAGMA table_info(cobranca)")
    cols_cob = [row[1] for row in cursor.fetchall()]
    if cols_cob and 'fatura_id' not in cols_cob:
        cursor.execute("ALTER TABLE cobranca ADD COLUMN "
                       "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    # Índices iguais aos do Postgres (banco local antigo; create_all já
    # cria em banco novo pelo modelo).
    if cols_vb:
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_venda_b2b_fatura "
                       "ON venda_b2b(fatura_id)")
    if cols_vp:
        cursor.execute("CREATE INDEX IF NOT EXISTS "
                       "ix_venda_b2b_parcela_fatura "
                       "ON venda_b2b_parcela(fatura_id)")
    if cols_cob:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cobranca_fatura "
                       "ON cobranca(fatura_id) WHERE fatura_id IS NOT NULL")

    # ── Baixa do B2B na separacao + vinculo orcamento→venda (07/07/2026) ──
    cursor.execute("PRAGMA table_info(venda_b2b)")
    cols_vb3 = [row[1] for row in cursor.fetchall()]
    if cols_vb3 and 'estoque_baixado_em' not in cols_vb3:
        cursor.execute("ALTER TABLE venda_b2b ADD COLUMN "
                       "estoque_baixado_em TIMESTAMP")
        # Backfill one-shot: venda ativa existente baixou na criacao.
        cursor.execute("UPDATE venda_b2b SET estoque_baixado_em = "
                       "COALESCE(criado_em, CURRENT_TIMESTAMP) "
                       "WHERE estoque_baixado_em IS NULL "
                       "AND status = 'ativa'")
    cursor.execute("PRAGMA table_info(orcamento)")
    cols_orc = [row[1] for row in cursor.fetchall()]
    if cols_orc and 'venda_id' not in cols_orc:
        cursor.execute("ALTER TABLE orcamento ADD COLUMN "
                       "venda_id INTEGER REFERENCES venda_b2b(id)")
    if cols_orc and 'arquivado_em' not in cols_orc:
        # Rascunho arquivavel (08/07/2026) — espelha o ALTER do Postgres.
        cursor.execute("ALTER TABLE orcamento ADD COLUMN "
                       "arquivado_em TIMESTAMP")

    # ── SKU do Tiny por canal (06/07/2026) ──
    # SQLite nao altera UNIQUE embutida — rebuild da tabela (mesmo padrao
    # do previsao_snapshot acima): copia com canal='site' e troca a unique
    # (kind, item_id) por (canal, kind, item_id).
    cursor.execute("PRAGMA table_info(tiny_produto_map)")
    cols_tiny = [row[1] for row in cursor.fetchall()]
    if cols_tiny and 'canal' not in cols_tiny:
        cursor.execute("""
            CREATE TABLE tiny_produto_map_novo (
                id INTEGER PRIMARY KEY,
                canal VARCHAR(10) NOT NULL DEFAULT 'site',
                kind VARCHAR(10) NOT NULL,
                item_id INTEGER NOT NULL,
                tiny_sku VARCHAR(100),
                tiny_nome VARCHAR(300),
                auto_match BOOLEAN,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id),
                criado_em TIMESTAMP,
                atualizado_em TIMESTAMP,
                CONSTRAINT uq_tiny_map_canal_item
                    UNIQUE (canal, kind, item_id)
            )
        """)
        cursor.execute("""
            INSERT INTO tiny_produto_map_novo
                (id, canal, kind, item_id, tiny_sku, tiny_nome, auto_match,
                 confirmado_em, confirmado_por, criado_em, atualizado_em)
            SELECT id, 'site', kind, item_id, tiny_sku, tiny_nome, auto_match,
                   confirmado_em, confirmado_por, criado_em, atualizado_em
            FROM tiny_produto_map
        """)
        cursor.execute("DROP TABLE tiny_produto_map")
        cursor.execute("ALTER TABLE tiny_produto_map_novo "
                       "RENAME TO tiny_produto_map")

    # ── Acuracia por ANTECEDENCIA (11/07/2026, aprovado pelo dono) ──
    # A unique (data_alvo, loja, receita, motor) passa a incluir lead_dias
    # (1 snapshot por antecedencia da mesma data). SQLite nao altera UNIQUE
    # embutida -> rebuild (mesmo padrao do rebuild de motor/lead acima).
    # Detecta a unique VELHA de 4 colunas via PRAGMA (nao ha coluna nova
    # pra usar de marcador).
    unique_velha = False
    cursor.execute("PRAGMA index_list(previsao_snapshot)")
    for row in cursor.fetchall():
        idx_nome, idx_unique = row[1], row[2]
        if not idx_unique:
            continue
        cursor.execute("PRAGMA index_info('%s')" % idx_nome)
        cols_idx = [r[2] for r in cursor.fetchall()]
        if cols_idx == ['data_alvo', 'loja_id', 'receita_id', 'motor']:
            unique_velha = True
            break
    if unique_velha:
        cursor.execute("""
            CREATE TABLE previsao_snapshot_novo (
                id INTEGER PRIMARY KEY,
                data_alvo DATE NOT NULL,
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                receita_id INTEGER NOT NULL REFERENCES receita(id),
                previsto INTEGER NOT NULL DEFAULT 0,
                realizado INTEGER,
                casado_em TIMESTAMP,
                criado_em TIMESTAMP,
                motor VARCHAR(20) NOT NULL DEFAULT 'pedido_semana',
                lead_dias INTEGER,
                CONSTRAINT uq_previsao_snapshot_alvo_motor_lead
                    UNIQUE (data_alvo, loja_id, receita_id, motor,
                            lead_dias)
            )
        """)
        cursor.execute("""
            INSERT INTO previsao_snapshot_novo
                (id, data_alvo, loja_id, receita_id, previsto, realizado,
                 casado_em, criado_em, motor, lead_dias)
            SELECT id, data_alvo, loja_id, receita_id, previsto, realizado,
                   casado_em, criado_em, motor, lead_dias
            FROM previsao_snapshot
        """)
        cursor.execute("DROP TABLE previsao_snapshot")
        cursor.execute("ALTER TABLE previsao_snapshot_novo "
                       "RENAME TO previsao_snapshot")
        cursor.execute("CREATE INDEX IF NOT EXISTS "
                       "ix_previsao_snapshot_data_alvo "
                       "ON previsao_snapshot(data_alvo)")
        cursor.execute("CREATE INDEX IF NOT EXISTS "
                       "ix_previsao_snapshot_criado_em "
                       "ON previsao_snapshot(criado_em)")

    # ── Restauracao do incidente "M6 Commit D" (12/07/2026) ──
    # Espelho do bloco Postgres: re-cria as colunas BLOB dropadas (vazias).
    for _tab, _col in (('receita', 'imagem_blob'),
                       ('produto', 'imagem_blob'),
                       ('foto_recebimento', 'imagem'),
                       ('pedido_item_foto', 'imagem')):
        cursor.execute("PRAGMA table_info(%s)" % _tab)
        if _col not in [row[1] for row in cursor.fetchall()]:
            cursor.execute("ALTER TABLE %s ADD COLUMN %s BLOB"
                           % (_tab, _col))

    # Aniversário do cliente do site (11/07/2026, portal Wi-Fi): dia/mês
    # pra campanha, ano opcional (LGPD). Espelho do ALTER do Postgres.
    cursor.execute("PRAGMA table_info(cliente)")
    cols_cli = [row[1] for row in cursor.fetchall()]
    for _c in ('aniversario_dia', 'aniversario_mes', 'nascimento_ano'):
        if cols_cli and _c not in cols_cli:
            cursor.execute(f"ALTER TABLE cliente ADD COLUMN {_c} INTEGER")

    # Módulo antigo "Vídeos simples" REMOVIDO (24/07/2026, DROP autorizado pelo
    # dono). Espelho do bloco Postgres. Filhas antes das pais (sem CASCADE no
    # SQLite); idempotente (IF EXISTS). Em teste as tabelas nem existem mais
    # (o modelo foi apagado, db.create_all não as cria) — no-op gracioso.
    for _t in ('treinamento_progresso', 'treinamento_conclusao',
               'treinamento_tentativa', 'treinamento_opcao',
               'treinamento_pergunta', 'treinamento'):
        cursor.execute(f"DROP TABLE IF EXISTS {_t}")

    # Menu degustação configurável no site (26/07/2026) — espelho do bloco
    # Postgres. Ver lá o porquê de `preco_menu` morar no ProdutoItem.
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod2 = [row[1] for row in cursor.fetchall()]
    if cols_prod2 and 'menu_configuravel' not in cols_prod2:
        cursor.execute("ALTER TABLE produto ADD COLUMN menu_configuravel "
                       "BOOLEAN NOT NULL DEFAULT 0")
    for _c in ('menu_total_unidades', 'menu_max_por_item'):
        if cols_prod2 and _c not in cols_prod2:
            cursor.execute(f"ALTER TABLE produto ADD COLUMN {_c} INTEGER")
    cursor.execute("PRAGMA table_info(produto_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'preco_menu' not in cols_pi:
        cursor.execute("ALTER TABLE produto_item ADD COLUMN preco_menu "
                       "NUMERIC(10, 2)")

    # Setor no item de checklist (03/08/2026) — espelho do bloco Postgres.
    cursor.execute("PRAGMA table_info(checklist_item_modelo)")
    cols_chk = [row[1] for row in cursor.fetchall()]
    if cols_chk and 'setor' not in cols_chk:
        cursor.execute("ALTER TABLE checklist_item_modelo ADD COLUMN setor "
                       "VARCHAR(60)")

    # Descadastro de marketing (05/08/2026) — espelho do bloco Postgres.
    cursor.execute("PRAGMA table_info(cliente)")
    cols_cli2 = [row[1] for row in cursor.fetchall()]
    if cols_cli2 and 'marketing_descadastro_em' not in cols_cli2:
        cursor.execute("ALTER TABLE cliente ADD COLUMN "
                       "marketing_descadastro_em TIMESTAMP")

    # Origem do cadastro (05/08/2026) — espelho do bloco Postgres.
    if cols_cli2 and 'origem' not in cols_cli2:
        cursor.execute("ALTER TABLE cliente ADD COLUMN origem VARCHAR(20)")
        cursor.execute(
            "UPDATE cliente SET origem = 'wifi' WHERE origem IS NULL AND ("
            "aniversario_dia IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM wifi_portal_sessao s "
            "WHERE LOWER(s.email) = LOWER(cliente.email)))")

    # Roteiro da aula do treinamento (13/08/2026) — espelho do bloco Postgres.
    cursor.execute("PRAGMA table_info(treino_video)")
    cols_tv = [row[1] for row in cursor.fetchall()]
    if cols_tv and 'roteiro' not in cols_tv:
        cursor.execute("ALTER TABLE treino_video ADD COLUMN roteiro TEXT")

    # Bloqueio de itens por data especial (07/08/2026) — espelho do PG.
    cursor.execute("PRAGMA table_info(loja_data_especial)")
    cols_lde = [row[1] for row in cursor.fetchall()]
    if cols_lde and 'bloquear_itens' not in cols_lde:
        cursor.execute("ALTER TABLE loja_data_especial ADD COLUMN "
                       "bloquear_itens TEXT")

    # "Pular endereço" do motorista (08/08/2026) — espelho do bloco PG.
    cursor.execute("PRAGMA table_info(atribuicao_entrega)")
    cols_ae = [row[1] for row in cursor.fetchall()]
    if cols_ae and 'pulado_em' not in cols_ae:
        cursor.execute("ALTER TABLE atribuicao_entrega ADD COLUMN "
                       "pulado_em TIMESTAMP")

    # Responsável pela perda de produção (13/08/2026) — espelho do bloco PG.
    cursor.execute("PRAGMA table_info(perda_producao)")
    cols_pp = [row[1] for row in cursor.fetchall()]
    if cols_pp and 'funcionario_id' not in cols_pp:
        cursor.execute("ALTER TABLE perda_producao ADD COLUMN "
                       "funcionario_id INTEGER")

    # Liderança direta e checklist de aplicação editável (24/08/2026).
    cursor.execute("PRAGMA table_info(funcionario)")
    cols_func = [row[1] for row in cursor.fetchall()]
    if cols_func and 'lider_id' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN "
                       "lider_id INTEGER REFERENCES funcionario(id) ")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_funcionario_lider "
                   "ON funcionario(lider_id)")
    cursor.execute("PRAGMA table_info(treino_item_checklist)")
    cols_tic = [row[1] for row in cursor.fetchall()]
    if cols_tic and 'ativo' not in cols_tic:
        cursor.execute("ALTER TABLE treino_item_checklist ADD COLUMN "
                       "ativo BOOLEAN NOT NULL DEFAULT 1")

    conn.commit()
    conn.close()
