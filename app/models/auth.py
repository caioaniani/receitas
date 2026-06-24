"""Modelos do dominio: auth.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.utils import agora


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuario'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    login = db.Column(db.String(50), nullable=False, unique=True)
    senha_hash = db.Column(db.String(256), nullable=False)
    papel = db.Column(db.String(20), nullable=False, default='funcionario')
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    is_owner = db.Column(db.Boolean, default=False)

    loja = db.relationship('Loja', backref='usuarios')

    def set_senha(self, senha):
        # scrypt eh resistente a GPU (vs pbkdf2 que eh quebravel rapido em
        # placa moderna). Senhas antigas em pbkdf2:sha256 continuam validas
        # — Werkzeug detecta o metodo pelo prefixo do hash em check_senha.
        self.senha_hash = generate_password_hash(senha, method='scrypt')

    def check_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    def is_admin(self):
        return self.papel == 'admin' or self.is_dono()

    def is_gerente(self):
        return self.papel == 'gerente'

    def is_producao(self):
        return self.papel == 'producao'

    def is_padeiro(self):
        """Chao de fabrica: tela touchscreen (separar + gerar QR de saida)."""
        return self.papel == 'padeiro'

    def is_rh(self):
        return self.papel == 'rh'

    def pode_lojas(self):
        """Pedidos, Estoque Loja, Relatorio."""
        return self.is_admin() or self.is_gerente()

    def pode_producao(self):
        """Plano de Producao, Congelados, Separacao."""
        return self.is_admin() or self.is_producao()

    def pode_catalogo(self):
        """Acesso ao catalogo: leitura (Receitas/MP/Produtos/Fornecedores) e
        operacoes de estoque de MP. Producao tem isso; a ESCRITA de definicoes
        (criar/editar/excluir) eh restrita a admin via @admin_required."""
        return self.is_admin() or self.is_producao()

    def pode_rh(self):
        """RH (ponto/ferias/cargos sem salario)."""
        return self.is_admin() or self.is_rh()

    def pode_pdv(self):
        """PDV, Seru, VNDA, Mapeamentos."""
        return self.is_admin()

    def is_dono(self):
        """Owner: dono unico do sistema (ve areas pessoais Vida/Igreja).
        Defensivo: se a coluna ainda nao existir, retorna False sem quebrar."""
        try:
            return bool(getattr(self, 'is_owner', False))
        except Exception:
            return False

    def __repr__(self):
        return f'<Usuario {self.login}>'

class Atribuicao(db.Model):
    __tablename__ = 'atribuicao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    status = db.Column(db.String(20), default='pendente')  # 'pendente' ou 'concluida'
    data_atribuicao = db.Column(db.DateTime, default=agora)
    data_conclusao = db.Column(db.DateTime)

    receita = db.relationship('Receita', backref='atribuicoes')
    usuario = db.relationship('Usuario', backref='atribuicoes')

    def __repr__(self):
        return f'<Atribuicao {self.receita_id} -> {self.usuario_id}>'
