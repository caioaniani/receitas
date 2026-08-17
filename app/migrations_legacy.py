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
        _seed_minimo_danish(app)
        _seed_minimo_danish_v2(app)
        _seed_minimo_danish_v3(app)
        _seed_minimo_cinnamon(app)


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
                              'DEFAULT FALSE'),
        }
        for col, sql in migrações_receita.items():
            if col not in colunas:
                conn.execute(text(sql))
        # Backfill ÚNICO (só quando a coluna acabou de nascer, pra não
        # sobrescrever um desmarque futuro do dono): o caso que motivou a
        # flag — o Levain (pé) sumiu da tela do padeiro ao virar sub-receita.
        if 'sub_na_amassadeira' not in colunas:
            conn.execute(text(
                "UPDATE receita SET sub_na_amassadeira = TRUE "
                "WHERE nome = 'Levain (pé)'"))
        # Backfill ÚNICO da flag nova (mesmo padrão do Levain): o dono pediu
        # a política pra Massa para folhar. Junto, a correção de ficha que
        # ele ditou na mesma conversa (19/07/2026): croissant tradicional
        # leva 86 g de massa → 50 un × 86 g ÷ 3.580 g/bola = 1,2011 bola
        # por batida (a ficha estava 1.0 = 71,6 g; o valor documentado de
        # 03/07 era 1,257 = 90 g). Guard em porcentagem = 1.0: se o dono já
        # tiver editado a ficha pra outro valor, não sobrescreve.
        if 'estoque_nao_abate' not in colunas:
            conn.execute(text(
                "UPDATE receita SET estoque_nao_abate = TRUE "
                "WHERE nome = 'Massa para folhar'"))
            # ORDER BY id LIMIT 1: receita.nome NAO tem unique — um nome
            # duplicado faria a subquery escalar estourar no Postgres e
            # derrubar o boot (todos os ALTERs do bloco iriam junto).
            conn.execute(text(
                "UPDATE receita_ingrediente SET porcentagem = 1.2011 "
                "WHERE receita_id = (SELECT id FROM receita "
                "                    WHERE nome = 'Croissant Tradicional' "
                "                    ORDER BY id LIMIT 1) "
                "  AND sub_receita_id = (SELECT id FROM receita "
                "                        WHERE nome = 'Massa para folhar' "
                "                        ORDER BY id LIMIT 1) "
                "  AND porcentagem = 1.0"))

        # receita_etapa.descricao — passo-a-passo do que fazer em cada etapa,
        # preenchido pelo padeiro na ficha de preparo (14/07/2026). Alimenta o
        # fluxograma junto com nome/duracao/equipamento. Commit 1 do procedimento
        # de 2 commits: schema primeiro, modelo depois.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita_etapa'"
        ))
        cols_re = {row[0] for row in result}
        if cols_re and 'descricao' not in cols_re:
            conn.execute(text("ALTER TABLE receita_etapa ADD COLUMN descricao TEXT"))

        # Lote de pedido padrao (UMA vez: so se NADA foi configurado ainda — uma
        # vez que o dono mexa em qualquer lote, nunca mais sobrescreve). A loja
        # pede em pacotes: pao frances 50, sourdough 20, croissant almond 15,
        # croissant tradicional 50 com minimo 250 (-> 250/300). Ajustavel na
        # ficha. Nomes conferidos no pedido real (29/06).
        ja_tem_lote = conn.execute(text(
            "SELECT COUNT(*) FROM receita WHERE lote_pedido IS NOT NULL")
        ).scalar()
        if not ja_tem_lote:
            for sql in (
                "UPDATE receita SET lote_pedido=50 WHERE "
                "LOWER(nome) LIKE '%pão franc%' OR LOWER(nome) LIKE '%pao franc%'",
                "UPDATE receita SET lote_pedido=20 WHERE "
                "LOWER(nome) LIKE '%sourdough%'",
                "UPDATE receita SET lote_pedido=15 WHERE "
                "LOWER(nome) LIKE '%croissant%almond%'",
                "UPDATE receita SET lote_pedido=50, minimo_pedido=250 WHERE "
                "LOWER(nome) LIKE '%croissant%' AND LOWER(nome) NOT LIKE '%almond%'",
            ):
                conn.execute(text(sql))

        # Brioche entra no pre-preparo como assado por default (idempotente:
        # so seta se ainda nao houver valor — preserva decisao manual posterior).
        conn.execute(text(
            "UPDATE receita SET estado_padrao='assado' "
            "WHERE LOWER(nome) LIKE '%brioche%' AND estado_padrao IS NULL"
        ))

        # receita_ingrediente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita_ingrediente'"
        ))
        cols_ing = {row[0] for row in result}
        if cols_ing and 'tipo' not in cols_ing:
            conn.execute(text("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'"))
        # FK pra sub-receita (tipo='receita'): liga por ID, não só por nome —
        # necessário pra baixa de estoque confiável (ex: croissant almond consome
        # croissant tradicional congelado). Backfill por nome exato (idempotente).
        if cols_ing and 'sub_receita_id' not in cols_ing:
            conn.execute(text(
                "ALTER TABLE receita_ingrediente "
                "ADD COLUMN sub_receita_id INTEGER REFERENCES receita(id)"))
        conn.execute(text("""
            UPDATE receita_ingrediente ri SET sub_receita_id = r.id
            FROM receita r
            WHERE ri.tipo = 'receita' AND ri.sub_receita_id IS NULL
              AND lower(trim(r.nome)) = lower(trim(ri.ingrediente_nome))
        """))

        # produto
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'produto'"
        ))
        cols_prod = {row[0] for row in result}
        if cols_prod:
            migrações_produto = {
                'custo_direto': 'ALTER TABLE produto ADD COLUMN custo_direto REAL',
                'custo_embalagem': 'ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0',
                'preco_interno': 'ALTER TABLE produto ADD COLUMN preco_interno REAL',
                'modo_preparo': 'ALTER TABLE produto ADD COLUMN modo_preparo TEXT',
                'observacao': 'ALTER TABLE produto ADD COLUMN observacao TEXT',
                'reaproveitavel': 'ALTER TABLE produto ADD COLUMN reaproveitavel BOOLEAN NOT NULL DEFAULT FALSE',
                # Sob encomenda D+2 (dono 21/07/2026) — espelho da Receita.
                'sob_encomenda': 'ALTER TABLE produto ADD COLUMN sob_encomenda BOOLEAN NOT NULL DEFAULT FALSE',
            }
            for col, sql in migrações_produto.items():
                if col not in cols_prod:
                    conn.execute(text(sql))

        # funcionario
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'funcionario'"
        ))
        cols_func = {row[0] for row in result}
        if cols_func:
            migrações_func = {
                'funcao_operacional': 'ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)',
                'periodo': 'ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)',
                'cadastro_pendente': 'ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT FALSE',
                'data_nascimento': 'ALTER TABLE funcionario ADD COLUMN data_nascimento DATE',
                'horas_extras': 'ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0',
            }
            for col, sql in migrações_func.items():
                if col not in cols_func:
                    conn.execute(text(sql))

        # loja
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'loja'"
        ))
        cols_loja = {row[0] for row in result}
        if cols_loja:
            if 'planta_imagem' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_imagem BYTEA"))
            if 'planta_mimetype' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)"))

        # slot_mapa
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'slot_mapa'"
        ))
        cols_slot = {row[0] for row in result}
        if cols_slot:
            if 'largura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15"))
            if 'altura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8"))

        # usuario.loja_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'usuario'"
        ))
        cols_user = {row[0] for row in result}
        if cols_user and 'loja_id' not in cols_user:
            conn.execute(text("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)"))

        # posicao.origem
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'posicao'"
        ))
        cols_pos = {row[0] for row in result}
        if cols_pos and 'origem' not in cols_pos:
            conn.execute(text("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'"))
            conn.execute(text(
                "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
                "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
                "  AND slot_mapa.nome = posicao.nome_posicao)"
            ))

        # materia_prima.estoque_atual
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'materia_prima'"
        ))
        cols_mp = {row[0] for row in result}
        if cols_mp and 'estoque_atual' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0"))
        if cols_mp and 'peso_unidade' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL"))
        if cols_mp and 'sugerir_pedido_loja' not in cols_mp:
            # MP que as lojas pedem da industria (checkbox no banco de MPs) —
            # entra na tela de pedidos da semana por venda+estoque.
            conn.execute(text(
                "ALTER TABLE materia_prima ADD COLUMN sugerir_pedido_loja "
                "BOOLEAN NOT NULL DEFAULT FALSE"))
        if cols_mp and 'arquivada_em' not in cols_mp:
            # MP fora de circulacao (some de pickers/matchers, historico fica).
            conn.execute(text(
                "ALTER TABLE materia_prima ADD COLUMN arquivada_em TIMESTAMP"))
            conn.execute(text(
                "ALTER TABLE materia_prima ADD COLUMN arquivada_por_id INTEGER "
                "REFERENCES usuario(id)"))

        # pedido_item.quantidade_recebida + materia_prima_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_item'"
        ))
        cols_pi = {row[0] for row in result}
        if cols_pi and 'quantidade_recebida' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER"))
        if cols_pi and 'materia_prima_id' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))
        if cols_pi and 'estado' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN estado VARCHAR(20)"))

        # estoque_loja.materia_prima_id + nome_pendente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_loja'"
        ))
        cols_el = {row[0] for row in result}
        if cols_el and 'materia_prima_id' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))
        if cols_el and 'nome_pendente' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)"))
        if cols_el and 'estado' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN estado VARCHAR(20)"))
        # Estoque minimo da LOJA por item (loja x receita/produto/MP): piso
        # da sugestao de pedido loja->industria (motor venda+estoque em
        # previsao_producao.sugerir_pedidos_por_venda). Vazio = sem piso.
        if cols_el and 'estoque_minimo' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN estoque_minimo INTEGER"))
        # Pedido minimo DIARIO por item (dono 17/08/2026, danishes assadas:
        # "as lojas devem receber 2 danishes desses por dia
        # IMPRETERIVELMENTE"): piso INCONDICIONAL do pedido do dia — nao
        # desconta o estoque que sobrou na loja (diferente do
        # estoque_minimo, que e colchao). Vazio = sem piso.
        if cols_el and 'pedido_minimo_diario' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN pedido_minimo_diario INTEGER"))

        # Tabelas Seru (mapeamento + idempotencia). db.create_all() cria
        # automaticamente, este bloco e so safety pra ambientes que ja
        # existiam antes do schema.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_produto_map (
                id SERIAL PRIMARY KEY,
                seru_nome VARCHAR(300) NOT NULL UNIQUE,
                seru_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_seru_produto_nome ON seru_produto_map(seru_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_loja_map (
                id SERIAL PRIMARY KEY,
                seru_company_name VARCHAR(300) NOT NULL UNIQUE,
                loja_id INTEGER REFERENCES loja(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                auto_match BOOLEAN DEFAULT FALSE,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_pedido_processado (
                seru_pedido_id VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                loja_id INTEGER REFERENCES loja(id),
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))

        # Coluna nova em SeruProdutoMap pra produtos compostos/fracionados
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'seru_produto_map'"
        ))
        cols_spm = {row[0] for row in result}
        if cols_spm and 'fator_quantidade' not in cols_spm:
            conn.execute(text("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0"))

        # Acumulador de baixas fracionadas
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                seru_produto_map_id INTEGER NOT NULL REFERENCES seru_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, seru_produto_map_id)
            )
        """))

        # mov_estoque_loja.tipo: VARCHAR(20) era curto pra 'venda_seru_sem_estoque' (22)
        try:
            conn.execute(text("ALTER TABLE mov_estoque_loja ALTER COLUMN tipo TYPE VARCHAR(50)"))
        except Exception:
            pass

        # mov_estoque_loja.desperdicio_id — liga a BAIXA de estoque ao registro
        # de Desperdicio que a causou. Permite excluir um desperdicio duplicado
        # estornando exatamente o que ele baixou (caso real 02/07/2026: lote
        # re-enviado pelo bot duplicou 4 perdas na Nebraska). NULL = registro
        # anterior a esta coluna (exclusao nao mexe em estoque nesses).
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'mov_estoque_loja'"
        ))
        cols_mel = {row[0] for row in result}
        if cols_mel and 'desperdicio_id' not in cols_mel:
            conn.execute(text(
                'ALTER TABLE mov_estoque_loja ADD COLUMN desperdicio_id '
                'INTEGER REFERENCES desperdicio(id) ON DELETE SET NULL'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_mov_estoque_loja_desperdicio_id '
                'ON mov_estoque_loja(desperdicio_id)'
            ))

        # retirada_sobra_item.quantidade_coletada — o motorista confere na
        # COLETA quanto esta levando de fato (loja declarou 15, sairam 12).
        # NULL = coletou o declarado. A baixa da loja usa o coletado; o
        # recebimento na industria parte dele (decisao do dono 03/07/2026).
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'retirada_sobra_item'"
        ))
        cols_rsi = {row[0] for row in result}
        if cols_rsi and 'quantidade_coletada' not in cols_rsi:
            conn.execute(text(
                'ALTER TABLE retirada_sobra_item ADD COLUMN '
                'quantidade_coletada INTEGER'
            ))

        # seru_loja_map.seru_company_id — ancora ESTAVEL do vinculo Seru->Loja
        # (UUID da company na API). Incidente 06-07/07/2026: renomearam as
        # lojas no Seru e o vinculo por NOME quebrou em silencio (Ribeiro sem
        # baixa; vendas caindo na Anesio). Com o id, renome so atualiza o
        # rotulo. Procedimento de 2 commits: este ALTER sobe ANTES do modelo.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'seru_loja_map'"
        ))
        cols_slm = {row[0] for row in result}
        if cols_slm and 'seru_company_id' not in cols_slm:
            conn.execute(text(
                'ALTER TABLE seru_loja_map ADD COLUMN '
                'seru_company_id VARCHAR(64)'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_seru_loja_map_company_id '
                'ON seru_loja_map (seru_company_id)'
            ))
        # seru_loja_map.seru_company_document — CNPJ da company (pedido do
        # dono 07/07/2026: vincular loja pelo CNPJ, que ele reconhece —
        # matriz x filial — em vez do nome, que o Seru renomeia).
        if cols_slm and 'seru_company_document' not in cols_slm:
            conn.execute(text(
                'ALTER TABLE seru_loja_map ADD COLUMN '
                'seru_company_document VARCHAR(20)'
            ))

        # mov_estoque_producao.tipo: VARCHAR(20) era curto pra 'venda_b2b_sem_estoque' (21).
        # Estourava o INSERT quando uma venda B2B nao tinha estoque suficiente,
        # quebrando o POST de /b2b/vendas/nova com 500 (causa identificada via
        # log em 06/06/2026). Mesmo motivo do mov_estoque_loja acima.
        try:
            conn.execute(text("ALTER TABLE mov_estoque_producao ALTER COLUMN tipo TYPE VARCHAR(50)"))
        except Exception:
            pass

        # Tabelas VNDA (mapeamento + idempotencia + acumulador fracionario)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_produto_map (
                id SERIAL PRIMARY KEY,
                vnda_nome VARCHAR(300) NOT NULL UNIQUE,
                vnda_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vnda_produto_nome ON vnda_produto_map(vnda_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_pedido_processado (
                vnda_pedido_code VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_entrega DATE,
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_debito (
                vnda_produto_map_id INTEGER NOT NULL REFERENCES vnda_produto_map(id) ON DELETE CASCADE,
                componente_key VARCHAR(50) NOT NULL DEFAULT 'self',
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (vnda_produto_map_id, componente_key)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_produto_map (
                id SERIAL PRIMARY KEY,
                nome_digitado VARCHAR(200) NOT NULL UNIQUE,
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                materia_prima_id INTEGER REFERENCES materia_prima(id),
                ignorar BOOLEAN DEFAULT FALSE NOT NULL,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_loja_produto_map_nome ON loja_produto_map(nome_digitado)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                loja_produto_map_id INTEGER NOT NULL REFERENCES loja_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, loja_produto_map_id)
            )
        """))

        # pedido_loja.driver_id — quem pegou via handshake de saida
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_loja'"
        ))
        cols_pl = {row[0] for row in result}
        if cols_pl and 'driver_id' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN driver_id INTEGER '
                'REFERENCES driver_entrega(id) ON DELETE SET NULL'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_pedido_loja_driver_id '
                'ON pedido_loja(driver_id)'
            ))

        # pedido_loja.modificado_em / modificado_por_id — quem editou e quando
        if cols_pl and 'modificado_em' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN modificado_em TIMESTAMP'
            ))
        if cols_pl and 'modificado_por_id' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN modificado_por_id INTEGER '
                'REFERENCES usuario(id) ON DELETE SET NULL'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_pedido_loja_modificado_por_id '
                'ON pedido_loja(modificado_por_id)'
            ))

        # pedido_item_foto — foto de conferencia por SKU
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pedido_item_foto (
                id SERIAL PRIMARY KEY,
                pedido_item_id INTEGER NOT NULL REFERENCES pedido_item(id),
                etapa VARCHAR(10) NOT NULL,
                imagem BYTEA NOT NULL,
                mimetype VARCHAR(100),
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                criado_por_id INTEGER REFERENCES usuario(id),
                criado_por_driver_id INTEGER REFERENCES driver_entrega(id),
                CONSTRAINT uq_pedidoitemfoto_item_etapa UNIQUE (pedido_item_id, etapa)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pedido_item_foto_pedido_item_id "
            "ON pedido_item_foto(pedido_item_id)"
        ))

        # M6: migracao BLOB → Dropbox em pedido_item_foto.
        # Adiciona colunas Dropbox e relaxa NOT NULL do BLOB pra novas fotos
        # ja subirem direto sem blob. Backfill popula URLs depois.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_item_foto'"
        ))
        cols_pif = {row[0] for row in result}
        if cols_pif and 'imagem_url' not in cols_pif:
            conn.execute(text(
                'ALTER TABLE pedido_item_foto ADD COLUMN imagem_url VARCHAR(500)'
            ))
        if cols_pif and 'imagem_storage_path' not in cols_pif:
            conn.execute(text(
                'ALTER TABLE pedido_item_foto '
                'ADD COLUMN imagem_storage_path VARCHAR(500)'
            ))
        if cols_pif and 'imagem' in cols_pif:
            # Best-effort: drop NOT NULL. Em PG, eh idempotente em coluna ja
            # nullable. Se ja foi feito, este DDL ainda eh OK (no-op).
            conn.execute(text(
                'ALTER TABLE pedido_item_foto ALTER COLUMN imagem DROP NOT NULL'
            ))

        # M6: mesmas colunas em foto_recebimento (criada via db.create_all)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'foto_recebimento'"
        ))
        cols_fr = {row[0] for row in result}
        if cols_fr and 'imagem_url' not in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento ADD COLUMN imagem_url VARCHAR(500)'
            ))
        if cols_fr and 'imagem_storage_path' not in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento '
                'ADD COLUMN imagem_storage_path VARCHAR(500)'
            ))
        if cols_fr and 'imagem' in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento ALTER COLUMN imagem DROP NOT NULL'
            ))

        # M6: imagem_dropbox_url + imagem_storage_path em receita e produto.
        # Aqui usamos nome diferente do imagem_url legado (URL externa de
        # fallback pra cardapio digital) que NAO eh substituido.
        for tabela in ('receita', 'produto'):
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ), {'t': tabela})
            cols = {row[0] for row in result}
            if cols and 'imagem_dropbox_url' not in cols:
                conn.execute(text(
                    f'ALTER TABLE {tabela} '
                    f'ADD COLUMN imagem_dropbox_url VARCHAR(500)'
                ))
            if cols and 'imagem_storage_path' not in cols:
                conn.execute(text(
                    f'ALTER TABLE {tabela} '
                    f'ADD COLUMN imagem_storage_path VARCHAR(500)'
                ))

        # driver_magic_token — magic link diario do motorista
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS driver_magic_token (
                id SERIAL PRIMARY KEY,
                driver_id INTEGER NOT NULL REFERENCES driver_entrega(id),
                token VARCHAR(64) NOT NULL UNIQUE,
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expira_em TIMESTAMP NOT NULL,
                revogado BOOLEAN NOT NULL DEFAULT FALSE,
                enviado_em TIMESTAMP,
                enviado_ok BOOLEAN
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_driver_magic_token_driver_id "
            "ON driver_magic_token(driver_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_driver_magic_token_token "
            "ON driver_magic_token(token)"
        ))

        # estoque_producao.nome_pendente (balanco aceita itens sem cadastro previo)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_producao'"
        ))
        cols_ep = {row[0] for row in result}
        if cols_ep and 'nome_pendente' not in cols_ep:
            conn.execute(text("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)"))
        if cols_ep and 'estado' not in cols_ep:
            conn.execute(text("ALTER TABLE estoque_producao ADD COLUMN estado VARCHAR(20)"))

        # vnda_debito.componente_key (cestas: PK composta para 1 acumulador por componente)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vnda_debito'"
        ))
        cols_vd = {row[0] for row in result}
        if cols_vd and 'componente_key' not in cols_vd:
            conn.execute(text("ALTER TABLE vnda_debito DROP CONSTRAINT IF EXISTS vnda_debito_pkey"))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD COLUMN componente_key VARCHAR(50) NOT NULL DEFAULT 'self'"
            ))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD PRIMARY KEY (vnda_produto_map_id, componente_key)"
            ))

        # B2B no padeiro (Fase 1): venda_b2b ganha data de entrega + um status
        # de ENTREGA proprio (pendente/separado/em_transporte/entregue),
        # separado do status FINANCEIRO (ativa/cancelada) pra nao mexer em
        # parcelas/faturamento. venda_b2b_item ganha estado (cru/backup/assado),
        # igual PedidoItem. So ALTER aqui; modelo entra no commit seguinte.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venda_b2b'"
        ))
        cols_vb = {row[0] for row in result}
        if cols_vb and 'data_entrega' not in cols_vb:
            conn.execute(text("ALTER TABLE venda_b2b ADD COLUMN data_entrega DATE"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_venda_b2b_data_entrega "
                "ON venda_b2b(data_entrega)"
            ))
        if cols_vb and 'status_entrega' not in cols_vb:
            conn.execute(text(
                "ALTER TABLE venda_b2b ADD COLUMN status_entrega VARCHAR(20) "
                "NOT NULL DEFAULT 'pendente'"
            ))

        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venda_b2b_item'"
        ))
        cols_vbi = {row[0] for row in result}
        if cols_vbi and 'estado' not in cols_vbi:
            conn.execute(text("ALTER TABLE venda_b2b_item ADD COLUMN estado VARCHAR(20)"))
        if cols_vbi and 'observacao' not in cols_vbi:
            conn.execute(text("ALTER TABLE venda_b2b_item ADD COLUMN observacao VARCHAR(200)"))

        conn.commit()

    # Migrações resilientes (cada ALTER em sua própria transação)
    cols_user2 = _cols('usuario')
    if cols_user2 and 'is_owner' not in cols_user2:
        _try("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT FALSE")
        _try("UPDATE usuario SET is_owner = TRUE WHERE id = "
             "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)")

    # Migracao papel_v1: introduz niveis (gerente/producao/rh). Roda uma vez.
    # Downgrade de admins NAO-owner pra funcionario; owner sobe pra papel='admin' se ainda nao for.
    _try("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    try:
        with db.engine.connect() as c:
            r = c.execute(text("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")).fetchone()
            ja_rodou = bool(r)
    except Exception:
        ja_rodou = True  # se nao consegue ler, nao mexe
    if not ja_rodou:
        _try("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = FALSE)")
        _try("UPDATE usuario SET papel='admin' WHERE is_owner = TRUE AND papel <> 'admin'")
        _try("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")

    cols_pa = _cols('projeto_area')
    if cols_pa and 'cor' not in cols_pa:
        _try("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    cols_tp = _cols('tarefa_projeto')
    if cols_tp and 'observacao' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    cols_func_res = _cols('funcionario')
    if cols_func_res and 'horas_extras' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func_res and 'tem_cargo_confianca' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT FALSE")
        # Liga a flag para quem ja tinha cargo_confianca > 0 (preserva comportamento)
        _try("UPDATE funcionario SET tem_cargo_confianca = TRUE WHERE cargo_confianca > 0")

    # Cargo: cria a coluna FK + popula cargos a partir das funcoes existentes
    if cols_func_res and 'cargo_id' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        # Cria 1 cargo por funcao distinta com o salario MAIS COMUM (moda) de quem tem essa funcao
        _try("""
        INSERT INTO cargo (nome, salario_base, ativo)
        SELECT funcao, MAX(salario_base), TRUE
        FROM funcionario
        WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        GROUP BY funcao
        ON CONFLICT (nome) DO NOTHING
        """)
        # Liga cada funcionario ao cargo correspondente
        _try("""
        UPDATE funcionario SET cargo_id = (
            SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
        ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # ── Override de data de entrega de pedido VNDA (local) ──
    _try("""
    CREATE TABLE IF NOT EXISTS override_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        data_entrega DATE NOT NULL,
        motivo TEXT,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_override_entrega_code ON override_entrega(pedido_code)")

    # ── Cache de geocoding (CEP/endereco -> lat/lng) ──
    _try("""
    CREATE TABLE IF NOT EXISTS geocode_cache (
        id SERIAL PRIMARY KEY,
        chave VARCHAR(200) NOT NULL UNIQUE,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        fonte VARCHAR(50),
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_geocode_cache_chave ON geocode_cache(chave)")
    # Aumenta coluna fonte caso ja exista com VARCHAR(20)
    _try("ALTER TABLE geocode_cache ALTER COLUMN fonte TYPE VARCHAR(50)")
    # Limpa cache de falhas legacy (Nominatim/BrasilAPI/AwesomeAPI/google_fail).
    # Endereco volta a ser geocodado na proxima execucao via Google.
    _try("DELETE FROM geocode_cache WHERE lat IS NULL")

    # ── Drivers de entrega + atribuicoes pedido<->driver ──
    _try("""
    CREATE TABLE IF NOT EXISTS driver_entrega (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(80) NOT NULL UNIQUE,
        cor VARCHAR(20),
        telefone VARCHAR(30),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("""
    CREATE TABLE IF NOT EXISTS atribuicao_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        driver_id INTEGER REFERENCES driver_entrega(id) ON DELETE SET NULL,
        data_entrega DATE,
        ordem INTEGER DEFAULT 0,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_pedido ON atribuicao_entrega(pedido_code)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_data ON atribuicao_entrega(data_entrega)")

    # ── Comprovante de entrega: token+pin no driver, status+geo+fotos na atribuicao ──
    _try("ALTER TABLE driver_entrega ADD COLUMN token VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_driver_token ON driver_entrega(token)")
    _try("ALTER TABLE driver_entrega ADD COLUMN pin VARCHAR(8)")
    _try("ALTER TABLE driver_entrega ADD COLUMN capacidade INTEGER DEFAULT 999")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN status VARCHAR(20) DEFAULT 'pendente'")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN entregue_em TIMESTAMP")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN nota VARCHAR(500)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN motivo_falha VARCHAR(50)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lat DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lng DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN proof_hash VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_atribuicao_proof_hash ON atribuicao_entrega(proof_hash)")

    # Cronograma -> padeiro: alvo de unidades e quanto ja foi produzido por
    # item do plano (opcao B: a MP sai e o produto entra na producao).
    _try("ALTER TABLE planejamento_item ADD COLUMN qtd_alvo INTEGER")
    _try("ALTER TABLE planejamento_item ADD COLUMN produzido_qtd INTEGER NOT NULL DEFAULT 0")
    _try("ALTER TABLE planejamento_producao ADD COLUMN origem VARCHAR(20)")
    # Fluxo 2 passos (aprovar -> enviar): o padeiro só vê o que foi ENVIADO.
    # DEFAULT TRUE pra ordens já existentes continuarem visíveis; novas ordens
    # do "aprovar dia" nascem FALSE (rascunho) e viram TRUE no "enviar".
    _try("ALTER TABLE planejamento_producao ADD COLUMN enviado_ao_padeiro BOOLEAN DEFAULT TRUE")
    # Dispensa de pendencia (auditoria): admin verifica que nao foi produzido
    # (ou menos) e da OK -> para de mostrar como pendente, SEM creditar estoque.
    _try("ALTER TABLE planejamento_item ADD COLUMN dispensada_em TIMESTAMP")
    _try("ALTER TABLE planejamento_item ADD COLUMN dispensada_por_id INTEGER REFERENCES usuario(id)")
    # Parcela extra adicionada a mao (reagendamento da auditoria) — o re-sync
    # do cronograma preserva/soma em vez de apagar.
    _try("ALTER TABLE planejamento_item ADD COLUMN qtd_extra INTEGER NOT NULL DEFAULT 0")
    # Falta ENCERRADA pelo padeiro (17/07/2026): ele produziu menos que o
    # alvo e deu o item por feito — some da tela dele; a diferenca fica so
    # na auditoria (admin decide: OK/dispensar ou devolver pro padeiro).
    _try("ALTER TABLE planejamento_item ADD COLUMN falta_encerrada_em TIMESTAMP")

    # ── Acuracia do forecast por MOTOR (Fase 0, 02/07/2026) ──
    # A acuracia media so o motor aposentado (sugerir_pedidos_semana); agora
    # cada snapshot diz de QUAL motor veio ('pedido_semana' legado,
    # 'media_pedido', 'venda_estoque') e com QUE antecedencia (lead_dias).
    # A unique antiga (data_alvo, loja, receita) impediria 2 motores no mesmo
    # alvo — trocada por uma que inclui o motor.
    _try("ALTER TABLE previsao_snapshot ADD COLUMN motor VARCHAR(20) "
         "NOT NULL DEFAULT 'pedido_semana'")
    _try("ALTER TABLE previsao_snapshot ADD COLUMN lead_dias INTEGER")
    _try("ALTER TABLE previsao_snapshot DROP CONSTRAINT IF EXISTS "
         "uq_previsao_snapshot_alvo")
    # (11/07/2026) O ADD CONSTRAINT da unique de 4 colunas
    # (uq_previsao_snapshot_alvo_motor) que vivia aqui foi REMOVIDO: a
    # canonica agora e a de 5 colunas com lead_dias (bloco "Acuracia por
    # ANTECEDENCIA" adiante, que dropa a velha). Mante-lo recriava a velha
    # em todo boot (pra dropar logo depois) e, com duplicatas por lead na
    # tabela, falharia com warning a cada startup.

    # ── Caixa/piso de pedido pra MATERIA-PRIMA (Fase 1, 02/07/2026) ──
    # MP pedida pela loja (ex: pao de queijo congelado em saco) precisa de
    # lote/minimo como Receita tem — sem isso a sugestao sai picada, un a un.
    _try("ALTER TABLE materia_prima ADD COLUMN lote_pedido INTEGER")
    _try("ALTER TABLE materia_prima ADD COLUMN minimo_pedido INTEGER")

    _try("""
    CREATE TABLE IF NOT EXISTS entrega_foto (
        id SERIAL PRIMARY KEY,
        atribuicao_id INTEGER NOT NULL REFERENCES atribuicao_entrega(id) ON DELETE CASCADE,
        url VARCHAR(500) NOT NULL,
        storage_path VARCHAR(500),
        tirada_em TIMESTAMP DEFAULT NOW(),
        tamanho_bytes INTEGER
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_entrega_foto_atribuicao ON entrega_foto(atribuicao_id)")

    # ── Lotes de saída ──
    _try("""
    CREATE TABLE IF NOT EXISTS lote_saida (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(120) NOT NULL,
        data_entrega DATE NOT NULL,
        criado_em TIMESTAMP DEFAULT NOW(),
        janelas_json TEXT,
        status VARCHAR(20) DEFAULT 'aberto',
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_data ON lote_saida(data_entrega)")
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_status ON lote_saida(status)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN lote_id INTEGER REFERENCES lote_saida(id)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_lote ON atribuicao_entrega(lote_id)")

    # ── Audit Log estruturado ──
    _try("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        tabela VARCHAR(60) NOT NULL,
        registro_id INTEGER,
        acao VARCHAR(10) NOT NULL,
        antes TEXT,
        depois TEXT,
        ip VARCHAR(45),
        user_agent VARCHAR(300)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_audit_usuario ON audit_log(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_criado ON audit_log(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_tabela ON audit_log(tabela)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_registro ON audit_log(tabela, registro_id)")

    # ── Fornecedores + historico de preco MP ──
    _try("""
    CREATE TABLE IF NOT EXISTS fornecedor (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(150) NOT NULL UNIQUE,
        cnpj VARCHAR(20),
        telefone VARCHAR(30),
        email VARCHAR(120),
        contato VARCHAR(100),
        observacao TEXT,
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_fornecedor_ativo ON fornecedor(ativo)")
    _try("""
    CREATE TABLE IF NOT EXISTS historico_preco_mp (
        id SERIAL PRIMARY KEY,
        materia_prima_id INTEGER NOT NULL REFERENCES materia_prima(id),
        fornecedor_id INTEGER NOT NULL REFERENCES fornecedor(id),
        preco_unitario DOUBLE PRECISION NOT NULL,
        quantidade DOUBLE PRECISION NOT NULL,
        data TIMESTAMP DEFAULT NOW(),
        referencia VARCHAR(200),
        usuario_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_mp ON historico_preco_mp(materia_prima_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_fornecedor ON historico_preco_mp(fornecedor_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_data ON historico_preco_mp(data)")
    _try("ALTER TABLE movimentacao_estoque ADD COLUMN fornecedor_id INTEGER REFERENCES fornecedor(id)")

    # ── Copilot conversas (audit trail das interacoes com LLM) ──
    _try("""
    CREATE TABLE IF NOT EXISTS copilot_conversa (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        prompt TEXT NOT NULL,
        interpretacao_json TEXT,
        tipo_acao VARCHAR(40),
        status VARCHAR(20) DEFAULT 'pendente',
        executado_em TIMESTAMP,
        registro_tipo VARCHAR(40),
        registro_id INTEGER,
        erro TEXT
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_usuario ON copilot_conversa(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_criado ON copilot_conversa(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_status ON copilot_conversa(status)")

    # Backfill: cada data com atribuicoes orfas vira 1 lote 'Histórico DD/MM' concluido.
    # Idempotente — so cria se ainda houver lote_id NULL.
    _try("""
    INSERT INTO lote_saida (nome, data_entrega, criado_em, status)
    SELECT
        'Histórico ' || TO_CHAR(data_entrega, 'DD/MM/YYYY'),
        data_entrega,
        COALESCE(MIN(atualizado_em), NOW()),
        'concluido'
    FROM atribuicao_entrega
    WHERE lote_id IS NULL AND data_entrega IS NOT NULL
    GROUP BY data_entrega
    """)
    _try("""
    UPDATE atribuicao_entrega a
    SET lote_id = l.id
    FROM lote_saida l
    WHERE a.lote_id IS NULL
      AND a.data_entrega = l.data_entrega
      AND l.nome LIKE 'Histórico %'
    """)

    # ── Pedidos cadastrados fora do VNDA (manuais) ──
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local (
        id SERIAL PRIMARY KEY,
        code VARCHAR(50) NOT NULL UNIQUE,
        destinatario VARCHAR(200) NOT NULL,
        telefone VARCHAR(50) NOT NULL,
        endereco VARCHAR(500) NOT NULL,
        data_entrega DATE NOT NULL,
        periodo VARCHAR(80),
        cartinha TEXT,
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_data ON pedido_local(data_entrega)")
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local_item (
        id SERIAL PRIMARY KEY,
        pedido_local_id INTEGER NOT NULL REFERENCES pedido_local(id) ON DELETE CASCADE,
        nome VARCHAR(200) NOT NULL,
        quantidade INTEGER DEFAULT 1,
        preco_unitario REAL DEFAULT 0
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_item_pedido ON pedido_local_item(pedido_local_id)")

    # ── Desperdicio (sobra do dia / vencido) ──
    _try("""
    CREATE TABLE IF NOT EXISTS desperdicio (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        receita_id INTEGER REFERENCES receita(id),
        produto_id INTEGER REFERENCES produto(id),
        materia_prima_id INTEGER REFERENCES materia_prima(id),
        quantidade INTEGER NOT NULL,
        data DATE NOT NULL,
        motivo VARCHAR(30) NOT NULL DEFAULT 'vencido',
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_loja ON desperdicio(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_data ON desperdicio(data)")

    # ── Slack bot (DM/@mention → copilot) ──
    _try("""
    CREATE TABLE IF NOT EXISTS slack_vinculo (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL UNIQUE,
        slack_workspace_id VARCHAR(30),
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_uid ON slack_vinculo(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_ativo ON slack_vinculo(ativo)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_evento_processado (
        event_id VARCHAR(50) PRIMARY KEY,
        processado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_evento_em ON slack_evento_processado(processado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_acao_pendente (
        id SERIAL PRIMARY KEY,
        token VARCHAR(40) NOT NULL UNIQUE,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30),
        slack_message_ts VARCHAR(30),
        tipo_acao VARCHAR(50) NOT NULL,
        params_json TEXT NOT NULL,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        executado_em TIMESTAMP,
        cancelado_em TIMESTAMP
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_token ON slack_acao_pendente(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_em ON slack_acao_pendente(criado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_conversa (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30) NOT NULL,
        mensagens_json TEXT DEFAULT '[]',
        ultima_msg_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (slack_user_id, slack_channel_id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_uid ON slack_conversa(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_em ON slack_conversa(ultima_msg_em)")

    # ── Lembrete de pedido pra amanha (opt-out por loja+data) ──
    _try("""
    CREATE TABLE IF NOT EXISTS lembrete_pedido_optout (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        data_entrega DATE NOT NULL,
        marcado_por_slack_uid VARCHAR(30),
        marcado_por_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (loja_id, data_entrega)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lembrete_optout_data ON lembrete_pedido_optout(data_entrega)")

    # Indices em tabelas que crescem por movimentacao — historico de estoque
    # e itens de pedido sao consultados muito por FK (estoque_loja_id,
    # estoque_producao_id, pedido_id) e ordenados por data. Sem indice,
    # cada listagem vira full-scan.
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_el ON mov_estoque_loja(estoque_loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_data ON mov_estoque_loja(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_ep ON mov_estoque_producao(estoque_producao_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_data ON mov_estoque_producao(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_item_pedido ON pedido_item(pedido_id)")

    # B2B — venda da industria pra clientes externos. db.create_all cria as
    # tabelas no boot; aqui so adicionamos indices uteis e migracoes futuras.
    # Tabela preco_atacado foi criada por engano (preco ja existe em
    # Receita.preco_venda e Produto.preco_atacado). Dropa se existir.
    _try("DROP TABLE IF EXISTS preco_atacado")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_data ON venda_b2b(data_venda)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_cliente ON venda_b2b(cliente_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_item_venda ON venda_b2b_item(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venda ON venda_b2b_parcela(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venc ON venda_b2b_parcela(vencimento)")

    # Remove o status intermediario 'pendente': pedido nasce 'confirmado'.
    # 'pendente' e 'confirmado' sempre foram o mesmo estado (mesmo rotulo/aba/
    # transicoes — ver app/constants.py). Idempotente.
    _try("UPDATE pedido_loja SET status = 'confirmado' WHERE status = 'pendente'")

    # Handshake QR Code — PIN da loja + tokens curtos por pedido.
    _try("ALTER TABLE loja ADD COLUMN IF NOT EXISTS pin VARCHAR(8)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_token ON pedido_qrcode(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_pedido ON pedido_qrcode(pedido_id)")
    # Auditoria de tentativas de handshake (scan + PIN) — investigar falhas.
    _try("""
    CREATE TABLE IF NOT EXISTS handshake_audit (
        id SERIAL PRIMARY KEY,
        momento TIMESTAMP DEFAULT NOW(),
        token VARCHAR(40),
        pedido_id INTEGER REFERENCES pedido_loja(id),
        tipo VARCHAR(10),
        etapa VARCHAR(20) NOT NULL,
        detalhe VARCHAR(500),
        status_pedido VARCHAR(20),
        ip VARCHAR(45),
        user_agent VARCHAR(300)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_handshake_audit_pedido ON handshake_audit(pedido_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_handshake_audit_momento ON handshake_audit(momento)")

    # Vendas manuais pra lojas sem API (Anesio): so alimenta previsao /
    # sugestao de pedido. db.create_all cria a tabela; aqui so indices.
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_loja ON venda_manual_loja(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_data ON venda_manual_loja(data_venda)")

    # Cardapio digital: URL externa de imagem em receita + produto
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_url VARCHAR(400)")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_url VARCHAR(400)")
    # BLOB upload (Rappi 403 forced this — admin sobe a foto direto)
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_mimetype VARCHAR(50)")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_mimetype VARCHAR(50)")

    # Fase 2 contas a pagar (2026-06-10): conferencia humana — separa o que
    # um humano JA revisou do que e so extracao da IA. NULL = nao conferida.
    _try("ALTER TABLE conta_pagar ADD COLUMN IF NOT EXISTS revisada_em TIMESTAMP")
    _try("ALTER TABLE conta_pagar ADD COLUMN IF NOT EXISTS revisada_por_id INTEGER REFERENCES usuario(id)")

    # Arquivamento de receita (2026-06-10): receita com historico (pedidos,
    # vendas, estoque) NUNCA e excluida — arquivar tira ela das listas e
    # preserva o historico. NULL = ativa. ALTER vai NA FRENTE do modelo
    # (procedimento de 2 commits — ver CLAUDE.md "Schema migrations").
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS arquivada_em TIMESTAMP")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS arquivada_por_id INTEGER REFERENCES usuario(id)")

    # Item de NF -> PRODUTO de revenda (2026-06-10): agua/refrigerante/etc
    # comprados prontos nao sao materia-prima — o mapeamento ganha o alvo
    # produto_id (espelha SeruProdutoMap, que ja tem os dois). ALTER na
    # frente do modelo (2 commits).
    _try("ALTER TABLE conta_pagar_item_map ADD COLUMN IF NOT EXISTS "
         "produto_id INTEGER REFERENCES produto(id)")
    _try("CREATE INDEX IF NOT EXISTS ix_conta_pagar_item_map_produto "
         "ON conta_pagar_item_map(produto_id)")

    # Tools usadas pelo chatbot do site antes do veredito (14/06/2026):
    # persistir a lista de ferramentas chamadas em cada decisao do bot
    # permite ao auditor distinguir handoff "preguicoso" (lista vazia ou
    # so transferir_para_humano) de handoff legitimo, e calcular a taxa
    # de contencao real. ALTER vai NA FRENTE do modelo (2 commits — ver
    # CLAUDE.md "Schema migrations").
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS tools_usadas TEXT")

    # Reconhecimento de alerta no painel (banner + som). ALTER no mesmo
    # commit do modelo: _migrate roda no startup antes do gunicorn servir.
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS "
         "reconhecido_em TIMESTAMP")
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS "
         "reconhecido_por_id INTEGER REFERENCES usuario(id)")

    # Priority fee (gorjeta) da Lalamove pra acelerar a alocacao do
    # entregador. Coluna nova em lalamove_entrega (tabela criada via
    # db.create_all, que NAO altera tabela existente). ALTER no mesmo
    # commit do modelo eh seguro aqui: _migrate roda no startup ANTES do
    # gunicorn servir (app/__init__.py:423), entao a coluna existe antes
    # de qualquer SELECT. Dinheiro -> Numeric(10,2).
    _try("ALTER TABLE lalamove_entrega ADD COLUMN IF NOT EXISTS "
         "priority_fee NUMERIC(10, 2)")

    # Email do usuario (envio de senha/convite via Postmark, 16/06/2026).
    _try("ALTER TABLE usuario ADD COLUMN IF NOT EXISTS email VARCHAR(200)")

    # Senha provisoria (forca troca no 1o login) + acesso so treinamento
    # (23/07/2026, decisao do dono). Colunas booleanas em usuario — commit 1
    # do procedimento de 2 commits (modelo/logica vem no commit seguinte).
    _try("ALTER TABLE usuario ADD COLUMN IF NOT EXISTS "
         "senha_provisoria BOOLEAN DEFAULT FALSE")
    _try("ALTER TABLE usuario ADD COLUMN IF NOT EXISTS "
         "somente_treino BOOLEAN DEFAULT FALSE")

    # Destinatario diferente do pagador no pedido online (Fase 3+, 17/06/2026).
    # PedidoOnline e' tabela criada por db.create_all (Fase 3) -> em prod
    # ja existe sem as colunas; ALTER aplica em commit isolado.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nome_destinatario VARCHAR(150)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "telefone_destinatario VARCHAR(30)")

    # NF-e via Tiny (Fase 5, 17/06/2026): id do pedido/NF e status no Tiny.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "tiny_pedido_id VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "tiny_nota_fiscal_id VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nf_status VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nf_emitida_em TIMESTAMP")
    _try("CREATE INDEX IF NOT EXISTS ix_pedido_online_nf "
         "ON pedido_online(tiny_nota_fiscal_id)")

    # Endereco estruturado pra NF-e (17/06/2026): a SEFAZ exige logradouro/
    # numero/bairro/cidade/uf SEPARADOS; antes so guardavamos a linha unica
    # em endereco_entrega e a nota saia com "endereco/bairro/cidade em branco".
    for _c, _t in (('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)')):
        _try(f"ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS {_c} {_t}")

    # Ordem manual na vitrine (17/06/2026): produto/receita ganham
    # `ordem_site`. NULL = vai pro fim alfabetico. Tabela `categoria_site`
    # eh criada por db.create_all (sem ALTER necessario).
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS ordem_site INTEGER")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS ordem_site INTEGER")

    # Reserva de estoque (21/06/2026 — cutover loja propria, race condition):
    # `quantidade_reservada` segura saldo entre checkout e webhook pagar.me
    # pago (Pix expira 30min). Catalogo expoe `quantidade - quantidade_reservada`
    # como disponivel. NOT NULL DEFAULT 0 — todas as linhas existentes ficam
    # 0 (nada reservado retroativamente). `reserva_expira_em` no pedido
    # dispara liberacao via cron quando o cliente abandona o checkout.
    # ALTER no mesmo commit do modelo: _migrate roda no startup do gunicorn
    # ANTES de aceitar request, entao a coluna existe quando o codigo SELECT
    # nela. Padrao igual ao priority_fee (linha 1106) e usuario.email (1110).
    _try("ALTER TABLE estoque_loja ADD COLUMN IF NOT EXISTS "
         "quantidade_reservada INTEGER NOT NULL DEFAULT 0")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "reserva_expira_em TIMESTAMP")
    _try("CREATE INDEX IF NOT EXISTS ix_pedido_online_reserva_expira "
         "ON pedido_online(reserva_expira_em) "
         "WHERE reserva_expira_em IS NOT NULL")

    # client_id do GA4 (cookie _ga) capturado no checkout (13/07/2026) —
    # amarra o purchase server-side (Measurement Protocol) à sessão real do
    # cliente. Procedimento de 2 commits: este ALTER deploya ANTES do modelo.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "ga_client_id VARCHAR(64)")

    # "Fatiado?" por item do site (16/07/2026): o cliente escolhe se o pão
    # sourdough vem fatiado. So preferencia de corte — NAO mexe em preco nem
    # estoque (mesmo SKU). PRIMEIRA coluna adicionada a pedido_online_item
    # (tabela criada por db.create_all). Procedimento de 2 commits: este
    # ALTER deploya ANTES do modelo. NULL = nao fatiado (pedidos antigos).
    _try("ALTER TABLE pedido_online_item ADD COLUMN IF NOT EXISTS "
         "fatiado BOOLEAN")

    # Divulgacao (21/07/2026, pedido do dono): pedido "como do site" mas SEM
    # pagamento (brinde/PR) — aparece no painel de entregas com estrela, baixa
    # estoque marcado como divulgacao (fora de faturamento e da previsao de
    # venda), nunca conta como receita. Procedimento de 2 commits: este ALTER
    # deploya ANTES do modelo. FALSE = pedido normal (todas as linhas antigas).
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "divulgacao BOOLEAN NOT NULL DEFAULT FALSE")

    # Descricao do cardapio de ATACADO por receita (20/07/2026, ditado do
    # dono: "descricao sincera de cada produto b2b, quanto menos e mais").
    # Procedimento de 2 commits: este ALTER deploya ANTES do modelo.
    # BACKFILL UNICO na criacao da coluna (nunca re-aplica — se rodasse a
    # cada boot, sobrescreveria edicao do dono feita na ficha): textos a
    # partir dos ingredientes REAIS das fichas (T65/T45, Callebaut) + os
    # metodos ditados (backup/assado congelado/14 fatias/fresco 3 dias).
    # ARMADILHA que crashou o 1º deploy (20/07): aqui embaixo o `conn` do
    # bloco inicial ja esta FECHADO — usar _cols/_try/sub-conexao propria.
    cols_receita2 = _cols('receita')
    if cols_receita2 and 'descricao_atacado' not in cols_receita2:
        _try('ALTER TABLE receita ADD COLUMN descricao_atacado TEXT')
        try:
            with db.engine.connect() as c:
                for _nome, _desc in DESCRICOES_ATACADO_SEED:
                    c.execute(text(
                        'UPDATE receita SET descricao_atacado = :d '
                        'WHERE nome = :n AND descricao_atacado IS NULL'),
                        {'d': _desc, 'n': _nome})
                c.commit()
        except Exception as e:
            log.warning('migrate skip (seed descricao_atacado): %s', e)

    # Cobrança de sobra POR ITEM (01/08/2026, caso croissant tradicional).
    # A conferência de 29-31/07 provou o padrão: Pão Francês na Ribeiro com
    # 1.050 recebidos, 558 vendidos e ZERO sobra lançada em 14 dias — o
    # alerta das 20h só cobrava a LOJA ("lançou algo hoje?"), então lançar a
    # sobra de UM item calava a cobrança de todos os outros. A flag marca as
    # receitas cuja sobra é cobrada item a item. Backfill único na criação
    # (COBRA_SOBRA_SEED = a lista que o dono ajustou na conferência); a
    # ficha da receita manda dali em diante. Procedimento de 2 commits.
    cols_receita3 = _cols('receita')
    if cols_receita3 and 'cobra_sobra_diaria' not in cols_receita3:
        _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS "
             "cobra_sobra_diaria BOOLEAN NOT NULL DEFAULT FALSE")
        try:
            with db.engine.connect() as c:
                for _nome in COBRA_SOBRA_SEED:
                    c.execute(text(
                        'UPDATE receita SET cobra_sobra_diaria = TRUE '
                        'WHERE nome = :n'), {'n': _nome})
                c.commit()
        except Exception as e:
            log.warning('migrate skip (seed cobra_sobra_diaria): %s', e)

    # Memoria cross-conversa do bot de atendimento (19/07/2026, achado do
    # auditor "bot reiniciando do zero"): chatbot_conversa era chaveada SO
    # pelo conversation_id do Chatwoot — cliente que volta em conversa NOVA
    # perdia todo o contexto. `contato_key` = telefone canonizado do contato;
    # conversa nova busca o historico recente do MESMO contato. Procedimento
    # de 2 commits: este ALTER deploya ANTES do modelo. NULL = conversa
    # antiga sem telefone capturado (sem memoria retroativa).
    _try("ALTER TABLE chatbot_conversa ADD COLUMN IF NOT EXISTS "
         "contato_key VARCHAR(40)")
    _try("CREATE INDEX IF NOT EXISTS ix_chatbot_conversa_contato_key "
         "ON chatbot_conversa (contato_key)")

    # Frete na venda B2B (20/07/2026, pedido do dono via Bruno): valor do
    # frete da entrega COBRADO do cliente — soma no valor_total (parcela/
    # boleto herdam) e vai no campo valor_frete da NF do Tiny (mesmo padrao
    # da NF do site). Procedimento de 2 commits: este ALTER deploya ANTES
    # do modelo. Default 0 = vendas antigas sem frete, nada muda nelas.
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "frete_valor NUMERIC(10, 2) NOT NULL DEFAULT 0")

    # NF de transferencia industria→loja (20/07/2026, pedido do dono):
    # emitida no scan do QR de saida do pedido. A Loja vira DESTINATARIA
    # de NF-e (SEFAZ exige CNPJ + endereco estruturado — mesma licao do
    # ClienteB2B) e o PedidoLoja ganha o trio de NF do Tiny (mesmo contrato
    # de VendaB2B/PedidoOnline). Procedimento de 2 commits: estes ALTERs
    # deployam ANTES do modelo.
    for _c, _t in (('cnpj', 'VARCHAR(20)'),
                   ('inscricao_estadual', 'VARCHAR(20)'),
                   ('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cep', 'VARCHAR(9)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)'),
                   # Razao social legal (20/07/2026, pedido do dono: o nome
                   # no sistema e apelido interno; a NF precisa do nome
                   # legal da filial).
                   ('razao_social', 'VARCHAR(200)')):
        _try(f"ALTER TABLE loja ADD COLUMN IF NOT EXISTS {_c} {_t}")
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS "
         "tiny_nota_fiscal_id VARCHAR(40)")
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS "
         "nf_status VARCHAR(40)")
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS "
         "nf_emitida_em TIMESTAMP")
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS "
         "nf_numero VARCHAR(50)")
    # Dispensa de NF (20/07/2026, pedido do dono): por PEDIDO e por LOJA
    # ("checkbox tem que estar no /rh/lojas tambem") — o scan do QR pula a
    # emissao quando qualquer um dos dois dispensa. Decisao e do ADMIN
    # (motorista/padeiro nunca veem a opcao). Default FALSE = emite.
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS "
         "nf_dispensada BOOLEAN NOT NULL DEFAULT FALSE")
    _try("ALTER TABLE loja ADD COLUMN IF NOT EXISTS "
         "nf_dispensada BOOLEAN NOT NULL DEFAULT FALSE")

    # Dias de funcionamento da loja (27/07/2026, pedido do dono: "Cantina nao
    # precisa lancar sobras durante a semana pois so funciona de sabado e
    # domingo"). Guarda os dias em que a loja ABRE, no formato de digitos do
    # `date.weekday()` (0=segunda ... 6=domingo): '56' = sabado e domingo.
    # NULL/vazio = abre TODO DIA — e o valor de todas as lojas existentes,
    # entao nada muda pra quem nao configurar (fail-open deliberado: uma loja
    # mal configurada continua sendo cobrada, nunca some da cobranca em
    # silencio). Hoje o unico consumidor e a cobranca de sobras
    # (desperdicio_alerta.lojas_sem_desperdicio).
    _loja_tinha_dias = 'dias_funcionamento' in _cols('loja')
    _try("ALTER TABLE loja ADD COLUMN IF NOT EXISTS "
         "dias_funcionamento VARCHAR(7)")
    if not _loja_tinha_dias:
        # Backfill UNICO — so quando a coluna acaba de nascer, pra nunca
        # sobrescrever uma edicao futura do dono na tela /rh/lojas.
        _try("UPDATE loja SET dias_funcionamento = '56' "
             "WHERE LOWER(nome) LIKE '%cantina%' "
             "  AND dias_funcionamento IS NULL")
    # Motivo da REJEICAO da SEFAZ persistido (20/07/2026): antes o texto
    # ("CST com beneficio sem cBenef, cod 32") ia so no flash e sumia — o
    # dono/contador ficava sem saber o que corrigir no Tiny. TEXT porque a
    # mensagem da SEFAZ passa de 40 chars. Procedimento de 2 commits.
    _try("ALTER TABLE pedido_loja ADD COLUMN IF NOT EXISTS nf_erro TEXT")

    # Motivo do cancelamento (25/06/2026) — registra POR QUE um pedido do site
    # foi cancelado (pix_expirado / reembolso / cancelado_admin) em vez de
    # deduzir pelos timestamps. Coluna nullable; pedidos cancelados antes desta
    # coluna ficam NULL e a UI infere o motivo pelos timestamps.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "motivo_cancelamento VARCHAR(40)")

    # Orcamento B2B (22/06/2026) — data prevista de entrega + frete. A tabela
    # `orcamento` eh criada por db.create_all no mesmo deploy; estes ALTER
    # cobrem o caso de ela ter sido criada em deploy anterior sem as colunas.
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS data_entrega DATE")
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS "
         "frete_valor NUMERIC(10, 2) NOT NULL DEFAULT 0")

    # Descricao SEO (22/06/2026) — Receita nao tinha campo de descricao;
    # Produto tinha `descricao` curta. `descricao_seo` (TEXT) eh o que vira
    # publico na vitrine/JSON-LD/meta description.
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS descricao_seo TEXT")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS descricao_seo TEXT")

    # Plano de estoque do site por dia (22/06/2026). NAO sai por db.create_all
    # em ambientes ja existentes (so cria tabela nova; nao toca em prod).
    # Tabela canonica vem do modelo `EstoqueSitePlano`.
    _try("""
        CREATE TABLE IF NOT EXISTS estoque_site_plano (
            id SERIAL PRIMARY KEY,
            kind VARCHAR(10) NOT NULL,
            item_id INTEGER NOT NULL,
            data DATE NOT NULL,
            qtd_planejada INTEGER NOT NULL DEFAULT 0,
            qtd_reservada INTEGER NOT NULL DEFAULT 0,
            criado_em TIMESTAMP,
            atualizado_em TIMESTAMP,
            CONSTRAINT uq_estoque_site_plano_item_data
                UNIQUE (kind, item_id, data)
        )
    """)
    _try("""
        CREATE INDEX IF NOT EXISTS ix_estoque_site_plano_data
            ON estoque_site_plano(data)
    """)
    _try("""
        CREATE INDEX IF NOT EXISTS ix_estoque_site_plano_data_kind_item
            ON estoque_site_plano(data, kind, item_id)
    """)

    # ── NF-e do B2B via Tiny (06/07/2026) ──
    # Espelho dos campos do PedidoOnline: id da NF no Tiny + status +
    # timestamp de emissao confirmada. ALTER no mesmo commit do modelo:
    # _migrate roda no startup do gunicorn ANTES de aceitar request
    # (padrao igual ao quantidade_reservada acima).
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "tiny_nota_fiscal_id VARCHAR(40)")
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "nf_status VARCHAR(40)")
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "nf_emitida_em TIMESTAMP")

    # Endereco estruturado do cliente B2B (06/07/2026): a SEFAZ exige
    # logradouro/numero/bairro/cidade/uf SEPARADOS na NF-e (mesma licao do
    # pedido_online — "endereco/bairro/cidade em branco" rejeitava a nota).
    # O campo livre `endereco` continua existindo como fallback humano.
    for _c, _t in (('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cep', 'VARCHAR(9)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)')):
        _try(f"ALTER TABLE cliente_b2b ADD COLUMN IF NOT EXISTS {_c} {_t}")

    # Fechamento mensal B2B (07/07/2026): flag no cliente + vinculo das
    # vendas/parcelas/cobrancas com a FaturaB2B. A tabela fatura_b2b em si
    # sai do db.create_all, que roda ANTES destes ALTERs no startup
    # (_setup_schema) — o REFERENCES ja encontra a tabela.
    _try("ALTER TABLE cliente_b2b ADD COLUMN IF NOT EXISTS "
         "faturamento_mensal BOOLEAN NOT NULL DEFAULT FALSE")
    _try("ALTER TABLE venda_b2b ADD COLUMN IF NOT EXISTS "
         "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    _try("CREATE INDEX IF NOT EXISTS ix_venda_b2b_fatura "
         "ON venda_b2b(fatura_id)")
    _try("ALTER TABLE venda_b2b_parcela ADD COLUMN IF NOT EXISTS "
         "fatura_id INTEGER REFERENCES fatura_b2b(id)")
    _try("CREATE INDEX IF NOT EXISTS ix_venda_b2b_parcela_fatura "
         "ON venda_b2b_parcela(fatura_id)")
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

    conn.commit()
    conn.close()
