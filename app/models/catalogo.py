"""Modelos do dominio: catalogo.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


class MateriaPrima(db.Model):
    __tablename__ = 'materia_prima'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    unidade = db.Column(db.String(10), nullable=False, default='g')
    custo_por_kg = db.Column(db.Float, nullable=False)
    peso_unidade = db.Column(db.Float, nullable=True)
    fornecedor = db.Column(db.String(100))
    observacoes = db.Column(db.String(200))
    # MP que as LOJAS pedem da industria (ex: pao de queijo congelado, vendido
    # via cones) — entra na tela de pedidos da semana por venda+estoque.
    # Opt-in (checkbox no banco de MPs): a maioria das MPs e insumo so da
    # industria e nao deve poluir a tela de pedido das lojas.
    sugerir_pedido_loja = db.Column(db.Boolean, nullable=False, default=False,
                                    server_default='0')
    # Caixa/piso do pedido de loja (mesma semantica de Receita.lote_pedido/
    # minimo_pedido): MP pedida em saco (ex: pao de queijo congelado) nao pode
    # sair picada na sugestao. So vale pra MPs com sugerir_pedido_loja=True.
    lote_pedido = db.Column(db.Integer, nullable=True)
    minimo_pedido = db.Column(db.Integer, nullable=True)
    # Arquivada = FORA DE CIRCULACAO (ex: MP que na verdade era receita, apos
    # transferir os vinculos): some de autocompletes, matchers e pickers pra
    # ninguem conectar nada nela de novo. Historico 100% preservado (custeio
    # por nome e movimentacoes continuam legiveis). Reversivel no banco de MPs.
    arquivada_em = db.Column(db.DateTime, nullable=True)
    arquivada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)

    @classmethod
    def ativas(cls):
        """MPs em circulacao (nao arquivadas) — USE EM pickers/matchers/
        autocompletes (tudo que CONECTA algo novo a uma MP). Leituras de
        historico/custeio usam cls.query direto, sem filtro."""
        return cls.query.filter(cls.arquivada_em.is_(None))

    def to_dict(self):
        return {
            'nome': self.nome,
            'unidade': self.unidade,
            'custo_por_kg': self.custo_por_kg,
            'peso_unidade': self.peso_unidade,
            'fornecedor': self.fornecedor or '',
            'observacoes': self.observacoes or '',
        }

    estoque_atual = db.Column(db.Float, default=0)

    def __repr__(self):
        return f'<MateriaPrima {self.nome}>'

class Fornecedor(db.Model):
    """Fornecedor de materias-primas. Histórico de compras + cadastro
    pra evitar texto solto em MateriaPrima.fornecedor (campo legacy)."""
    __tablename__ = 'fornecedor'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    cnpj = db.Column(db.String(20))
    telefone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    contato = db.Column(db.String(100))  # nome do contato/vendedor
    observacao = db.Column(db.Text)
    ativo = db.Column(db.Boolean, default=True, index=True)
    criado_em = db.Column(db.DateTime, default=agora)

class HistoricoPrecoMP(db.Model):
    """Registro de cada compra de MP de um fornecedor — preço, quantidade,
    data. Alimentado automaticamente quando ha entrada de MP via
    MovimentacaoEstoque com fornecedor_id."""
    __tablename__ = 'historico_preco_mp'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False, index=True)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'), nullable=False, index=True)
    preco_unitario = db.Column(db.Float, nullable=False)
    quantidade = db.Column(db.Float, nullable=False)
    data = db.Column(db.DateTime, default=agora, index=True)
    referencia = db.Column(db.String(200))  # NF, observacao
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    materia_prima = db.relationship('MateriaPrima')
    fornecedor = db.relationship('Fornecedor', backref='compras')
    usuario = db.relationship('Usuario')

class Receita(db.Model):
    __tablename__ = 'receita'

    @classmethod
    def ativas(cls):
        """Receitas em circulacao (nao arquivadas) — USE EM pickers/matchers/
        seletores (tudo que CONECTA algo novo a uma receita: mapeamentos PDV/
        lote, vincular inline, ajuste de estoque). Mesmo contrato de
        MateriaPrima.ativas(). Leituras de historico usam cls.query direto."""
        return cls.query.filter(cls.arquivada_em.is_(None))

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50))
    # Descricao SEO (2-3 frases, ate ~300 chars) usada no <meta description>,
    # no JSON-LD Product e no card do site. Gerada com IA pela tela admin
    # /admin/seo/descricoes a partir do nome + ingredientes; revisada pelo
    # dono antes de virar publica. NULL = fallback "Nome — Categoria".
    descricao_seo = db.Column(db.Text, nullable=True)
    # Descricao SINCERA do cardapio de ATACADO (dono 20/07/2026: "quanto
    # menos e mais" — ingredientes reais + como o produto e vendido).
    # Editavel na ficha; seed unico das 9 receitas B2B na criacao da coluna
    # (migrations_legacy.DESCRICOES_ATACADO_SEED). So o /cardapio?tipo=
    # atacado (tela + PDF) le — loja/site seguem sem descricao de receita.
    descricao_atacado = db.Column(db.Text, nullable=True)
    # Ordem manual na vitrine (menor = mais cedo). NULL = vai pro fim,
    # ordenado por nome dentro de cada categoria. Setado pela tela de
    # curadoria (campo "ordem" no card).
    ordem_site = db.Column(db.Integer)
    # Familia define quais estados (assado/backup/cru) sao validos pra essa
    # receita. Ver app/constants.py:FAMILIAS_RECEITA. NULL = pao_sourdough.
    familia = db.Column(db.String(30), nullable=True, index=True)
    preco_venda = db.Column(db.Float)
    preco_loja = db.Column(db.Float)
    preco_site = db.Column(db.Float)
    preco_interno = db.Column(db.Float)
    imagem_url = db.Column(db.String(400))  # URL externa de fallback (legado)
    imagem_blob = db.Column(db.LargeBinary)  # legado: foto admin pre-M6
    imagem_dropbox_url = db.Column(db.String(500))  # shared link Dropbox (M6+)
    imagem_storage_path = db.Column(db.String(500))  # path Dropbox pra deletar
    imagem_mimetype = db.Column(db.String(50))
    rendimento_qtd = db.Column(db.Float, nullable=False)
    rendimento_unidade = db.Column(db.String(30), nullable=False)
    peso_base = db.Column(db.Float, nullable=False)
    peso_unitario = db.Column(db.Float)
    perda_percentual = db.Column(db.Float, default=0)
    custo_embalagem = db.Column(db.Float, default=0)
    modo_preparo = db.Column(db.Text)
    observacao = db.Column(db.Text)
    # Estado padrao no pre-preparo do padeiro: 'assado' / 'backup' / NULL.
    # Quando setado, vale como default pra qualquer PedidoItem dessa receita
    # cujo `estado` esteja NULL — sem precisar marcar item a item.
    estado_padrao = db.Column(db.String(20), nullable=True)
    # Lead time de producao em DIAS: quanto a receita leva pra ficar pronta
    # (sourdough de fermentacao longa = 2 = 48h; o que assa na hora = 0).
    # Usado pelo balanco/plano de producao: "produzir HOJE = demanda das
    # entregas em (hoje + dias_producao)" — pra o pao de 48h nao faltar. NAO
    # afeta a grade loja x dia (essa e datada pela ENTREGA, nao pela producao).
    dias_producao = db.Column(db.Integer, nullable=False, default=0,
                              server_default='0')
    # Capacidade da amassadeira em GRAMAS de MASSA final por batida (a massa
    # pesa farinha + agua + tudo — nao so a farinha). Usada pelo plano pra
    # contar FORNADAS reais: fornadas = ceil(massa_total / capacidade), onde
    # massa_total = massa_receita_base x multiplicador. Padrao 50000 (50kg/50L).
    # VALOR 0 = a receita NAO passa pela amassadeira (ex: Moedas, creme almond)
    # — o plano mostra unidades, nao fornadas. NAO altera consumo de MP.
    capacidade_amassadeira_g = db.Column(db.Integer, nullable=False,
                                         default=50000, server_default='50000')
    # Se a receita pode ser PEDIDA pela loja (entra na sugestao de pedido
    # semanal). False = insumo/etapa de producao que a loja nao pede direto
    # (ex: Creme de Amendoas, que vai DENTRO do Croissant Almond) — o forecast
    # nunca sugere. Default True: tudo continua sugerivel como antes.
    sugerir_pedido_loja = db.Column(db.Boolean, nullable=False, default=True,
                                    server_default='1')
    # Padronizacao do pedido (a loja pede em PACOTES, nao picado — decisao do
    # dono 29/06). lote_pedido = tamanho do pacote (arredonda a sugestao pro
    # multiplo mais proximo, no minimo 1 pacote). minimo_pedido = piso (ex:
    # croissant lote 50 + minimo 250 -> 250/300). NULL = sem padronizacao.
    lote_pedido = db.Column(db.Integer, nullable=True)
    minimo_pedido = db.Column(db.Integer, nullable=True)
    # Estoque MINIMO da INDUSTRIA (freezer) por receita: piso da previsao de
    # producao — o alvo do dia nunca cai abaixo dele, mantendo um colchao no
    # congelador alem da demanda prevista (decisao do dono 16/07/2026,
    # "considerar o estoque minimo para previsao"). Vazio = sem piso. ALTER em
    # migrations_legacy (commit 1, deployado e confirmado antes deste modelo).
    estoque_minimo_industria = db.Column(db.Integer, nullable=True)
    # Lote SO da PRODUCAO (decisao do dono 02/07: focaccia = placa de 8
    # pedacos; as lojas pedem pedacos livremente, mas a industria nao produz
    # placa quebrada). O cronograma arredonda a producao pra multiplos disto;
    # a sobra do arredondamento fica na industria e o balanco desconta no dia
    # seguinte. VAZIO = herda lote_pedido (croissant cx 50 segue como antes).
    lote_producao = db.Column(db.Integer, nullable=True)
    # Fornada especial: produto vendido SO sexta/sabado/domingo (ex: Focaccia
    # Gorgonzola). O forecast de pedido NAO sugere em dia de semana. Default
    # False = vende todo dia (comportamento normal).
    fornada_especial = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default='0')
    # Quando True, desperdicio com motivo='validade' NAO baixa estoque
    # — o item vencido vira outra coisa (ex: Croissant Tradicional vencido
    # vira Croissant Almond, Sourdough Tradicional vira chapa). Outros
    # motivos (estragou/caiu/queimou) ainda baixam normalmente.
    reaproveitavel = db.Column(db.Boolean, default=False, nullable=False)
    # Quando ESTA receita e consumida como SUB-RECEITA de outra ficha, ela
    # entra NA AMASSADEIRA junto da massa branca (ex.: Levain (pé) nos
    # sourdoughs) — a cascata da massa base conta/mostra em gramas
    # (qtd × peso_unitario) e o rendimento volta a ser massa/peso. False =
    # sub de MONTAGEM (Massa para folhar nos Danish), fora da amassadeira.
    # ALTER + backfill em migrations_legacy (2 commits, 15/07/2026).
    sub_na_amassadeira = db.Column(db.Boolean, default=False, nullable=False)
    # Estoque físico NÃO abate a produção sugerida (balanço + MRP do
    # cronograma) — só a produção JÁ MANDADA (WIP do plano de hoje) conta.
    # Decisão do dono 19/07/2026, caso Massa para folhar: o ledger dizia 2
    # bolas inexistentes e a massa dos 300 pains saía subestimada. O consumo
    # REAL na produção segue debitando EstoqueProducao normalmente (a flag é
    # só de planejamento). ALTER + backfill em migrations_legacy (2 commits).
    estoque_nao_abate = db.Column(db.Boolean, default=False, nullable=False)
    # Sob encomenda D+2 (dono 21/07/2026): no site, este item so pode ser
    # escolhido pra ENTREGA/RETIRADA a partir de D+2 (dois dias uteis a
    # frente, desde a janela das 08:00). E PRODUZIDO PRO PEDIDO — nao abate
    # a prateleira (a venda NAO baixa EstoqueLoja) e fica SEMPRE disponivel
    # na vitrine (nao olha plano-do-dia/estoque). O pedido pago vira demanda
    # firme de producao (entra no balanco/cronograma, estilo B2B) e aparece
    # na tela do padeiro (separacao + pre-preparo). ALTER em migrations_legacy
    # (commit 1 deployado e confirmado antes deste modelo).
    sob_encomenda = db.Column(db.Boolean, default=False, nullable=False)
    # Antecedencia maxima do NIVELADOR por receita (dono 18/08/2026,
    # "quero o maximo de brioche fresco nas lojas"): NULL = regra global
    # (_ANTECEDENCIA_MAX_DIAS = 3); 0 = assa so no dia da demanda —
    # fresco maximo; fim de semana continua caindo na sexta pelo
    # calendario seg-sex (rolagem, nao nivelamento). ALTER + backfill
    # (Brioche = 0) em migrations_legacy, commit 1 confirmado pela
    # sonda antes deste modelo.
    antecedencia_max_dias = db.Column(db.Integer, nullable=True)
    # Cobranca de sobra POR ITEM no alerta das 20h (01/08/2026, caso
    # croissant tradicional): com a flag, se a loja tem saldo desta receita
    # e NAO lancou Desperdicio dela no dia, o item aparece NOMINALMENTE na
    # cobranca de sobras (desperdicio_alerta.itens_sem_sobra). Antes o
    # alerta so cobrava a LOJA ("lancou algo?") — lancar a sobra de UM item
    # calava a cobranca de todos os outros (Pao Frances ficou 14 dias com
    # zero lancamento e 492 un de rombo, achado na conferencia de 29-31/07).
    # ALTER + seed em migrations_legacy (2 commits; seed = itens que o dono
    # ajustou na conferencia). Checkbox na ficha.
    cobra_sobra_diaria = db.Column(db.Boolean, default=False, nullable=False)
    # Devolucao loja->industria: sobras devolvidas DESTA receita creditam a
    # receita apontada no estoque da industria (ex: Croissant Tradicional ->
    # "Croissant Tradicional — Retorno"). NULL = credita a propria. O retorno
    # e receita SEPARADA porque a industria mantem 1 linha por receita
    # (uq_estoque_producao_receita) e o retornado (assado, de vespera) nao
    # pode se misturar com o congelado cru que atende pedidos das lojas.
    # ALTER em migrations_legacy (commit 1, 02/07/2026).
    retorno_receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                                   nullable=True)
    # Arquivamento: receita com historico (pedidos/vendas/estoque) nunca e
    # excluida — arquivar tira ela das listas e seletores preservando tudo.
    # NULL = ativa. Colunas criadas via _migrate_postgres/_migrate_sqlite
    # (ALTER deployado e confirmado em 10/06/2026, antes deste modelo).
    arquivada_em = db.Column(db.DateTime, nullable=True)
    arquivada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)

    # Self-FK: remote_side desambigua o lado "1" (a receita-destino).
    retorno_receita = db.relationship('Receita', remote_side='Receita.id',
                                      foreign_keys=[retorno_receita_id])

    ingredientes = db.relationship(
        'ReceitaIngrediente',
        backref='receita',
        # ReceitaIngrediente tem 2 FKs pra receita (receita_id e sub_receita_id);
        # esta relação é a da receita-DONA dos ingredientes.
        foreign_keys='ReceitaIngrediente.receita_id',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ReceitaIngrediente.id'
    )
    etapas = db.relationship(
        'ReceitaEtapa',
        backref='receita',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ReceitaEtapa.ordem'
    )

    @property
    def medida_em_gramas(self):
        """True quando a receita e pedida/estocada em GRAMAS ou ML, nao em
        unidades (as "Produção - Granola 1000g" da vida: peso_unitario=1.0 e
        rendimento em g/ml). Caso real 18/08/2026: pedidos lancados em POTES
        (5) num item medido em gramas (5000) inflaram o relatorio de pedidos
        em ~1000x. Heuristica — nao ha flag cadastral; usada pra AVISO
        nao-bloqueante na tela de pedido, nunca pra validacao dura."""
        un = (self.rendimento_unidade or '').strip().lower()
        return un in ('g', 'ml', 'kg', 'l') or self.peso_unitario == 1.0

    def to_dict(self):
        return {
            'nome': self.nome,
            'categoria': self.categoria or '',
            'preco_venda': self.preco_venda,
            'preco_loja': self.preco_loja,
            'preco_site': self.preco_site,
            'preco_interno': self.preco_interno,
            'rendimento_qtd': self.rendimento_qtd,
            'rendimento_unidade': self.rendimento_unidade,
            'peso_base': self.peso_base,
            'peso_unitario': self.peso_unitario,
            'perda_percentual': self.perda_percentual or 0,
            'custo_embalagem': self.custo_embalagem or 0,
            'modo_preparo': self.modo_preparo or '',
            'observacao': self.observacao or '',
            'ingredientes': [ing.to_dict() for ing in self.ingredientes],
        }

    def __repr__(self):
        return f'<Receita {self.nome}>'

class ReceitaEtapa(db.Model):
    """Etapa do processo de producao de uma receita (Mise en place, Amassamento,
    Descanso, Forno...), na ordem. Base do Gantt da producao: cada etapa tem
    duracao e, quando usa equipamento (amassadeira/forno), serializa com as
    outras (1 de cada). `ativa=False` = etapa PASSIVA (fermentacao/descanso
    longo) que acontece entre turnos e nao ocupa mao-de-obra."""
    __tablename__ = 'receita_etapa'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False, default=0)
    nome = db.Column(db.String(80), nullable=False)
    duracao_min = db.Column(db.Integer, nullable=False, default=0)
    # 'amassadeira' / 'forno' / 'bancada' / 'camara_fria' / NULL (sem equip.)
    equipamento = db.Column(db.String(30))
    ativa = db.Column(db.Boolean, nullable=False, default=True,
                      server_default='1')
    # O QUE fazer na etapa (passo a passo do padeiro — ficha /padeiro/fichas).
    # ALTER em migrations_legacy deployado ANTES deste modelo (2 commits).
    descricao = db.Column(db.Text)

    def to_dict(self):
        return {
            'id': self.id, 'ordem': self.ordem, 'nome': self.nome,
            'duracao_min': self.duracao_min, 'equipamento': self.equipamento,
            'ativa': self.ativa, 'descricao': self.descricao,
        }

    def __repr__(self):
        return f'<ReceitaEtapa {self.nome} {self.duracao_min}min>'


class MassaBase(db.Model):
    """Grupo de receitas que saem de UMA massa-mãe comum amassada de uma vez.

    A padaria amassa a base comum (o mínimo de cada ingrediente entre as
    receitas do grupo) e vai TIRANDO cada receita em cascata, acrescentando só o
    incremento que falta pra próxima (ex: massa sourdough → tira pão francês,
    +água tira sourdough tradicional, +grãos tira sourdough 7 grãos). 1 amassada
    no lugar de N. A ordem dos itens é a ordem da cascata (da que tem menos
    acréscimos pra que tem mais)."""
    __tablename__ = 'massa_base'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False)

    itens = db.relationship('MassaBaseItem', backref='massa_base', lazy=True,
                            cascade='all, delete-orphan',
                            order_by='MassaBaseItem.ordem')

    def __repr__(self):
        return f'<MassaBase {self.nome}>'


class MassaBaseItem(db.Model):
    """Receita dentro de uma massa-base, na ordem da cascata. Uma receita está
    em no máximo uma massa-base."""
    __tablename__ = 'massa_base_item'
    __table_args__ = (
        db.UniqueConstraint('receita_id', name='uq_massa_base_item_receita'),
    )

    id = db.Column(db.Integer, primary_key=True)
    massa_base_id = db.Column(db.Integer, db.ForeignKey('massa_base.id'),
                              nullable=False, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'),
                           nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False, default=0)

    receita = db.relationship('Receita')

    def __repr__(self):
        return f'<MassaBaseItem massa={self.massa_base_id} receita={self.receita_id}>'


class ReceitaIngrediente(db.Model):
    __tablename__ = 'receita_ingrediente'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    tipo = db.Column(db.String(10), default='mp')  # 'mp' ou 'receita'
    ingrediente_nome = db.Column(db.String(100), nullable=False)
    porcentagem = db.Column(db.Float, nullable=False)  # % padeiro (mp) ou qtd unidades (receita)
    eh_base = db.Column(db.Boolean, default=False)
    nota = db.Column(db.String(200))
    # FK pra sub-receita quando tipo='receita' (ex: croissant almond -> croissant
    # tradicional). Liga por ID; ingrediente_nome fica como fallback/rótulo. NULL
    # = órfão (não resolvido por nome) — vinculável na ficha.
    sub_receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'))
    sub_receita = db.relationship('Receita', foreign_keys=[sub_receita_id])

    def to_dict(self):
        return {
            'tipo': self.tipo or 'mp',
            'ingrediente_nome': self.ingrediente_nome,
            'porcentagem': self.porcentagem,
            'eh_base': self.eh_base,
            'nota': self.nota or '',
        }

    def __repr__(self):
        return f'<ReceitaIngrediente {self.ingrediente_nome} {self.porcentagem}%>'

class Produto(db.Model):
    __tablename__ = 'produto'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50))
    # Ordem manual na vitrine (menor = mais cedo). NULL = vai pro fim
    # alfabético. Mesmo padrão da Receita.
    ordem_site = db.Column(db.Integer)
    descricao = db.Column(db.String(300))
    # Descricao SEO (espelha Receita.descricao_seo) — quando preenchida,
    # prevalece sobre `descricao` no SEO/JSON-LD do site.
    descricao_seo = db.Column(db.Text, nullable=True)
    preco_atacado = db.Column(db.Float)
    preco_loja = db.Column(db.Float)
    preco_site = db.Column(db.Float)
    preco_interno = db.Column(db.Float)
    imagem_url = db.Column(db.String(400))  # URL externa de fallback (legado)
    imagem_blob = db.Column(db.LargeBinary)  # legado: foto admin pre-M6
    imagem_dropbox_url = db.Column(db.String(500))  # shared link Dropbox (M6+)
    imagem_storage_path = db.Column(db.String(500))
    imagem_mimetype = db.Column(db.String(50))
    custo_direto = db.Column(db.Float)  # custo por unidade para itens simples
    custo_embalagem = db.Column(db.Float, default=0)  # custo embalagem por unidade (R$)
    modo_preparo = db.Column(db.Text)
    observacao = db.Column(db.Text)
    # Quando True, desperdicio com motivo='validade' NAO baixa estoque
    # (mesma logica de Receita.reaproveitavel).
    reaproveitavel = db.Column(db.Boolean, default=False, nullable=False)
    # Sob encomenda D+2 (dono 21/07/2026) — espelho da Receita: no site so
    # vende pra data >= D+2, e produzido pro pedido (nao abate prateleira) e
    # entra na producao do padeiro. ALTER em migrations_legacy (commit 1).
    sob_encomenda = db.Column(db.Boolean, default=False, nullable=False)
    # ── Menu degustacao CONFIGURAVEL no site (26/07/2026, pedido do dono) ──
    # Cesta cujo cliente ajusta as quantidades de cada componente: a
    # `ProdutoItem.quantidade` do cadastro vira a PRE-SELECAO, o total tem
    # que fechar `menu_total_unidades` (o "30 minis, quais voce quiser") e
    # nenhum item passa de `menu_max_por_item`. O preco do menu no site e a
    # SOMA do `ProdutoItem.preco_menu` do que ele escolher — o `preco_site`
    # so PUBLICA. `menu_total_unidades` NULL = 30; `menu_max_por_item` NULL = SEM
    # teto (o cliente fecha o total com um item so, se quiser).
    # Regra e sanitizacao ficam em app/services/loja_menu.py.
    # ALTER em migrations_legacy (commit 1 deployado e confirmado por
    # /api/claude/deploy antes deste modelo).
    menu_configuravel = db.Column(db.Boolean, default=False, nullable=False)
    menu_total_unidades = db.Column(db.Integer, nullable=True)
    menu_max_por_item = db.Column(db.Integer, nullable=True)
    ativo = db.Column(db.Boolean, default=True)

    itens = db.relationship(
        'ProdutoItem',
        foreign_keys='ProdutoItem.produto_id',
        backref='produto',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ProdutoItem.id'
    )

    def to_dict(self):
        return {
            'nome': self.nome,
            'categoria': self.categoria or '',
            'descricao': self.descricao or '',
            'preco_atacado': self.preco_atacado,
            'preco_loja': self.preco_loja,
            'preco_site': self.preco_site,
            'preco_interno': self.preco_interno,
            'custo_direto': self.custo_direto,
            'custo_embalagem': self.custo_embalagem or 0,
            'modo_preparo': self.modo_preparo or '',
            'observacao': self.observacao or '',
            'ativo': self.ativo,
            'itens': [item.to_dict() for item in self.itens],
        }

    def __repr__(self):
        return f'<Produto {self.nome}>'

class ProdutoItem(db.Model):
    """Componente de uma cesta (Produto que agrupa outros itens).

    O vinculo eh por FK (receita_id ou materia_prima_id) — `item_nome` eh
    mantido por compat e como fallback humano-legivel, mas a baixa de
    estoque usa SEMPRE a FK. Renomear a receita NAO quebra a cesta.

    Antes da migration B5, era `item_nome` string e renomear receita
    fazia o componente sumir silenciosamente da baixa.
    """
    __tablename__ = 'produto_item'

    id = db.Column(db.Integer, primary_key=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' | 'produto' | 'mp'
    # FK do alvo (mutuamente exclusivas). NULL = orfao — precisa
    # vinculacao manual em /produtos/cestas/orfaos.
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True, index=True)
    # produto_componente_id eh o Produto-componente da cesta (NAO confundir com
    # produto_id acima, que eh a cesta pai). Ex: cesta "Family Box" tem como
    # componente o Produto "Iogurte 200ml" (comprado pronto, sem receita).
    produto_componente_id = db.Column(db.Integer, db.ForeignKey('produto.id'),
                                       nullable=True, index=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'),
                                  nullable=True, index=True)
    # Mantido por compat e como nome humano-legivel quando FK estiver NULL.
    item_nome = db.Column(db.String(150), nullable=False)
    quantidade = db.Column(db.Float, nullable=False, default=1)
    # Preco por UNIDADE deste componente DENTRO de um menu configuravel
    # (26/07/2026). So e lido quando o Produto-pai tem `menu_configuravel`;
    # o preco do menu vira a soma disto pelo que o cliente escolher (decisao
    # do dono: "cadastrar preco por mini"). Mora AQUI, e nao na Receita, de
    # proposito: os minis nao sao vendidos avulsos e um `preco_site` neles os
    # publicaria na vitrine (`loja_catalogo.produtos_publicados` usa
    # `preco_site > 0` como flag). NULL = nao cadastrado — o menu inteiro sai
    # do ar em vez de cobrar um preco que nao e o dele (fail-close).
    # ALTER em migrations_legacy (commit 1 deployado e confirmado antes).
    preco_menu = db.Column(db.Numeric(10, 2), nullable=True)

    receita = db.relationship('Receita', foreign_keys=[receita_id])
    produto_componente = db.relationship('Produto', foreign_keys=[produto_componente_id])
    materia_prima = db.relationship('MateriaPrima', foreign_keys=[materia_prima_id])

    @property
    def nome_resolvido(self):
        """Nome via FK (autoritativo). Fallback pra item_nome se orfao."""
        if self.tipo == 'receita' and self.receita:
            return self.receita.nome
        if self.tipo == 'produto' and self.produto_componente:
            return self.produto_componente.nome
        if self.tipo == 'mp' and self.materia_prima:
            return self.materia_prima.nome
        return self.item_nome  # orfao

    @property
    def unidade_resolvida(self):
        """Unidade real do componente (vem do alvo da FK).
        Sem alvo (orfao) → 'un'. Receita usa `rendimento_unidade` (geralmente
        'un'); MateriaPrima usa `unidade` (geralmente 'g'/'ml')."""
        if self.tipo == 'receita' and self.receita:
            return (self.receita.rendimento_unidade or 'un').strip().lower()
        if self.tipo == 'mp' and self.materia_prima:
            return (self.materia_prima.unidade or 'g').strip().lower()
        # 'produto' componente nao tem unidade clara — assumimos 'un'.
        return 'un'

    @property
    def qtd_formatada(self):
        """`quantidade` + unidade em string humano-legivel.
        - peso/volume (g, ml, kg, l) → "100g" / "1kg" (junto, sem espaco)
        - unidade ('un', '') → "2x" (formato classico das cestas)
        Incidente 22/06/2026: 'Family Box' mostrava "100x peito de peru"
        quando era 100g de peito de peru. Sem unidade, fica ambiguo."""
        qtd = self.quantidade or 0
        # Sem decimais quando inteiro; senao 1-3 casas sem zeros a direita.
        if qtd == int(qtd):
            num = f'{int(qtd)}'
        else:
            num = f'{qtd:.3f}'.rstrip('0').rstrip('.')
        un = self.unidade_resolvida
        if un in ('g', 'ml', 'kg', 'l'):
            return f'{num}{un}'
        return f'{num}x'

    @property
    def orfao(self):
        """True se nao tem FK setada — precisa vinculacao manual."""
        if self.tipo == 'receita':
            return self.receita_id is None
        if self.tipo == 'produto':
            return self.produto_componente_id is None
        if self.tipo == 'mp':
            return self.materia_prima_id is None
        return True

    def to_dict(self):
        return {
            'tipo': self.tipo,
            'item_nome': self.item_nome,
            'nome_resolvido': self.nome_resolvido,
            'orfao': self.orfao,
            'quantidade': self.quantidade,
        }

    def __repr__(self):
        return f'<ProdutoItem {self.nome_resolvido} x{self.quantidade}>'


class CatalogoFoto(db.Model):
    """Foto EXTRA de uma receita/produto — a galeria do site (26/07/2026).

    Pedido do dono: "gostaria de adicionar mais de uma, pelo menos 4".

    A foto PRINCIPAL continua sendo `Receita/Produto.imagem_dropbox_url` (a
    capa): é ela que aparece no card da vitrine, no cardápio, no PDF, no
    e-mail e no painel. Esta tabela só acrescenta as fotos SEGUINTES, que
    aparecem como miniaturas na página do produto. Nada que lê a capa hoje
    muda de comportamento.

    Endereçamento por (`kind`, `item_id`) em vez de duas FKs porque serve os
    dois catálogos com o mesmo código — mesmo par que `loja_catalogo` já usa
    ('receita'|'produto'). Tabela NOVA: nasce por `db.create_all`, sem ALTER.
    """
    __tablename__ = 'catalogo_foto'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)      # 'receita'|'produto'
    item_id = db.Column(db.Integer, nullable=False)
    dropbox_url = db.Column(db.String(500), nullable=False)
    # Path no Dropbox pra conseguir DELETAR o arquivo junto com a linha (sem
    # isso a foto removida ficaria órfã ocupando espaço pra sempre).
    storage_path = db.Column(db.String(500))
    ordem = db.Column(db.Integer, nullable=False, default=0)
    criado_em = db.Column(db.DateTime, default=agora)

    __table_args__ = (
        db.Index('ix_catalogo_foto_item', 'kind', 'item_id'),
    )

    def __repr__(self):
        return f'<CatalogoFoto {self.kind}:{self.item_id} #{self.ordem}>'
