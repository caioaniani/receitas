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
    # Email pra envio de senha/convite (Postmark). Nullable: usuários antigos
    # não têm; cadastro novo passa a exigir. Coluna criada via
    # _migrate_postgres/_migrate_sqlite no mesmo commit do modelo
    # (ADD COLUMN IF NOT EXISTS roda no startup antes de servir).
    email = db.Column(db.String(200), nullable=True)
    senha_hash = db.Column(db.String(256), nullable=False)
    papel = db.Column(db.String(20), nullable=False, default='funcionario')
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    is_owner = db.Column(db.Boolean, default=False)
    # Senha provisória (gerada no cadastro): força a troca no 1º login antes de
    # liberar qualquer tela. Coluna via _migrate_* (procedimento de 2 commits).
    senha_provisoria = db.Column(db.Boolean, default=False, nullable=False)
    # Acesso SÓ TREINAMENTO (por pessoa — decisão do dono 23/07/2026): o usuário
    # marcado só enxerga /treino; o gate global barra o resto (mesmo por URL).
    somente_treino = db.Column(db.Boolean, default=False, nullable=False)

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

    def lidera_equipe(self):
        """Verdadeiro quando a conta está ligada a um líder com equipe ativa."""
        try:
            funcionario = self.funcionario
            return bool(funcionario and any(
                liderado.ativo for liderado in funcionario.liderados))
        except Exception:
            return False

    def pode_organizar_equipe(self):
        """Acesso estreito ao cadastro de estrutura da equipe.

        O RH completo continua exclusivo do dono. Dakson recebeu apenas a
        tarefa operacional de informar líder, unidade principal e período;
        nenhum salário, documento ou dado financeiro é exposto por essa rota.
        """
        if self.is_dono():
            return True
        try:
            return bool(
                (self.login or '').strip().casefold() == 'dakson'
                and self.funcionario
                and self.funcionario.ativo
            )
        except Exception:
            return False

    def pode_cadastrar_funcionarios(self):
        """Acesso estreito do responsável de RH ao cadastro básico.

        O restante do RH continua exclusivo do dono. Dakson pode consultar a
        lista e incluir pessoas, mas não vê fichas, salários, folha ou acessos.
        """
        if self.is_dono():
            return True
        try:
            return bool(
                (self.login or '').strip().casefold() == 'dakson'
                and self.funcionario
                and self.funcionario.ativo
            )
        except Exception:
            return False

    def is_producao(self):
        return self.papel == 'producao'

    def is_padeiro(self):
        """Chao de fabrica: tela touchscreen (separar + gerar QR de saida)."""
        return self.papel == 'padeiro'

    def is_rh(self):
        return self.papel == 'rh'

    def is_marketing(self):
        """Marketing (21/07/2026): papel enxuto criado pra LANCAR DIVULGACAO
        (brinde/PR pela tela do site). So esse gesto — nenhuma outra area."""
        return self.papel == 'marketing'

    def pode_divulgacao(self):
        """Quem lanca/gerencia divulgacao: SO o dono e o marketing (decisao do
        dono 21/07/2026 — 'so o owner e marketing'). Admin comum NAO entra."""
        return self.is_dono() or self.is_marketing()

    def pode_lojas(self):
        """Pedidos, Estoque Loja, Relatorio."""
        return self.is_admin() or self.is_gerente()

    def pode_checklist(self):
        """Checklist de abertura/troca/fechamento da loja (03/08/2026).

        Espelha o decorator `checklist_required` pra sidebar poder mostrar o
        link ao atendente chefe (papel funcionario), que NAO ve a area Lojas.
        """
        if self.is_admin():
            return True
        from app.services import permissoes
        return permissoes.pode(self.papel or '', 'web_checklist')

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
