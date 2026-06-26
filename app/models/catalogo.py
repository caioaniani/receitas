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

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50))
    # Descricao SEO (2-3 frases, ate ~300 chars) usada no <meta description>,
    # no JSON-LD Product e no card do site. Gerada com IA pela tela admin
    # /admin/seo/descricoes a partir do nome + ingredientes; revisada pelo
    # dono antes de virar publica. NULL = fallback "Nome — Categoria".
    descricao_seo = db.Column(db.Text, nullable=True)
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
    # Quando True, desperdicio com motivo='validade' NAO baixa estoque
    # — o item vencido vira outra coisa (ex: Croissant Tradicional vencido
    # vira Croissant Almond, Sourdough Tradicional vira chapa). Outros
    # motivos (estragou/caiu/queimou) ainda baixam normalmente.
    reaproveitavel = db.Column(db.Boolean, default=False, nullable=False)
    # Arquivamento: receita com historico (pedidos/vendas/estoque) nunca e
    # excluida — arquivar tira ela das listas e seletores preservando tudo.
    # NULL = ativa. Colunas criadas via _migrate_postgres/_migrate_sqlite
    # (ALTER deployado e confirmado em 10/06/2026, antes deste modelo).
    arquivada_em = db.Column(db.DateTime, nullable=True)
    arquivada_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'),
                                 nullable=True)

    ingredientes = db.relationship(
        'ReceitaIngrediente',
        backref='receita',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ReceitaIngrediente.id'
    )

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

class ReceitaIngrediente(db.Model):
    __tablename__ = 'receita_ingrediente'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    tipo = db.Column(db.String(10), default='mp')  # 'mp' ou 'receita'
    ingrediente_nome = db.Column(db.String(100), nullable=False)
    porcentagem = db.Column(db.Float, nullable=False)  # % padeiro (mp) ou qtd unidades (receita)
    eh_base = db.Column(db.Boolean, default=False)
    nota = db.Column(db.String(200))

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
