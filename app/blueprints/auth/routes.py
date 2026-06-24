from urllib.parse import urlparse

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.decorators import admin_required
from app.extensions import db, limiter
from app.models import Atribuicao, Receita, Usuario
from app.utils import agora


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        login_val = request.form.get('login', '').strip()
        senha = request.form.get('senha', '')

        usuario = Usuario.query.filter_by(login=login_val).first()
        if usuario and usuario.check_senha(senha):
            # remember=True: cookie persistente (Flask-Login, ~1 ano) — a sessao
            # sobrevive a reiniciar o navegador/PC. Essencial pro kiosk do padeiro
            # nao ficar pedindo senha toda vez que reabre.
            login_user(usuario, remember=True)
            next_page = request.args.get('next')
            # Bloqueia redirect para URLs externas
            if next_page and urlparse(next_page).netloc:
                next_page = None
            if usuario.is_padeiro():
                return redirect(url_for('padeiro.index'))
            return redirect(next_page or url_for('main.index'))
        else:
            flash('Login ou senha incorretos.', 'danger')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Voce saiu do sistema.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/minhas-fichas')
@login_required
def minhas_fichas():
    """Painel do funcionário — fichas atribuídas."""
    atribuicoes = Atribuicao.query.filter_by(
        usuario_id=current_user.id
    ).order_by(Atribuicao.status, Atribuicao.data_atribuicao.desc()).all()

    return render_template('auth/minhas_fichas.html', atribuicoes=atribuicoes)


@auth_bp.route('/usuarios')
@login_required
@admin_required
def usuarios():
    """Gerenciar usuários — só admin."""
    from app.models import Loja
    usuarios = Usuario.query.order_by(Usuario.papel, Usuario.nome).all()
    lojas = (Loja.query.filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    return render_template('auth/usuarios.html', usuarios=usuarios, lojas=lojas)


@auth_bp.route('/usuarios/novo', methods=['POST'])
@login_required
@admin_required
def novo_usuario():
    nome = request.form.get('nome', '').strip()
    login_val = request.form.get('login', '').strip()
    senha = request.form.get('senha', '').strip()
    papel = request.form.get('papel', 'funcionario')
    from app.constants import PAPEIS_VALIDOS
    if papel not in PAPEIS_VALIDOS:
        papel = 'funcionario'

    if not nome or not login_val or not senha:
        flash('Preencha todos os campos.', 'warning')
        return redirect(url_for('auth.usuarios'))

    if Usuario.query.filter_by(login=login_val).first():
        flash(f'Login "{login_val}" ja existe.', 'warning')
        return redirect(url_for('auth.usuarios'))

    u = Usuario(nome=nome, login=login_val, papel=papel)
    u.set_senha(senha)
    db.session.add(u)
    db.session.commit()
    flash(f'Usuario "{nome}" criado!', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_usuario(id):
    u = Usuario.query.get_or_404(id)
    if u.id == current_user.id:
        flash('Voce nao pode excluir a si mesmo.', 'warning')
        return redirect(url_for('auth.usuarios'))
    if u.is_owner and not current_user.is_owner:
        flash('So o owner pode excluir o owner.', 'danger')
        return redirect(url_for('auth.usuarios'))

    Atribuicao.query.filter_by(usuario_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    flash(f'Usuario "{u.nome}" excluido.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/papel', methods=['POST'])
@login_required
@admin_required
def alterar_papel(id):
    u = Usuario.query.get_or_404(id)
    if u.is_owner:
        flash('Owner nao pode ter o papel alterado.', 'warning')
        return redirect(url_for('auth.usuarios'))

    papel = (request.form.get('papel') or '').strip()
    from app.constants import PAPEIS_VALIDOS
    if papel not in PAPEIS_VALIDOS:
        flash('Papel invalido.', 'warning')
        return redirect(url_for('auth.usuarios'))

    u.papel = papel
    db.session.commit()
    flash(f'Papel de "{u.nome}" alterado para {papel}.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/reset-senha', methods=['POST'])
@login_required
@admin_required
def reset_senha(id):
    u = Usuario.query.get_or_404(id)
    # Owner nao pode ter senha resetada por admin nao-owner. Owner trocando
    # a propria senha usa /auth/minha-senha (que exige senha atual).
    if u.is_owner and not current_user.is_owner:
        flash('So o owner pode trocar a senha do owner.', 'danger')
        return redirect(url_for('auth.usuarios'))

    nova_senha = request.form.get('nova_senha', '').strip()
    if not nova_senha:
        flash('Preencha a nova senha.', 'warning')
        return redirect(url_for('auth.usuarios'))
    if len(nova_senha) < 8:
        flash('Senha precisa ter pelo menos 8 caracteres.', 'warning')
        return redirect(url_for('auth.usuarios'))

    u.set_senha(nova_senha)
    db.session.commit()
    flash(f'Senha de "{u.nome}" alterada.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/atribuir/<int:receita_id>', methods=['POST'])
@login_required
@admin_required
def atribuir(receita_id):
    receita = Receita.query.get_or_404(receita_id)
    usuario_id = request.form.get('usuario_id')
    if not usuario_id:
        flash('Selecione um funcionario.', 'warning')
        return redirect(url_for('receitas.ficha', id=receita_id))

    # Verificar se já está atribuída a este usuário
    existente = Atribuicao.query.filter_by(
        receita_id=receita_id, usuario_id=int(usuario_id)
    ).first()
    if existente:
        flash('Esta ficha ja foi atribuida a este funcionario.', 'warning')
        return redirect(url_for('receitas.ficha', id=receita_id))

    atrib = Atribuicao(receita_id=receita_id, usuario_id=int(usuario_id))
    db.session.add(atrib)
    db.session.commit()

    usuario = Usuario.query.get(int(usuario_id))
    flash(f'Ficha "{receita.nome}" atribuida a {usuario.nome}!', 'success')
    return redirect(url_for('receitas.ficha', id=receita_id))


@auth_bp.route('/atribuicao/<int:id>/concluir', methods=['POST'])
@login_required
def concluir(id):
    """Funcionário marca ficha como concluída."""
    atrib = Atribuicao.query.get_or_404(id)

    if not current_user.is_admin() and atrib.usuario_id != current_user.id:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    atrib.status = 'concluida'
    atrib.data_conclusao = agora()
    db.session.commit()
    flash('Ficha marcada como concluida!', 'success')
    return redirect(url_for('auth.minhas_fichas'))


@auth_bp.route('/atribuicao/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_atribuicao(id):
    atrib = Atribuicao.query.get_or_404(id)
    db.session.delete(atrib)
    db.session.commit()
    flash('Atribuicao removida.', 'success')
    return redirect(url_for('auth.painel'))


@auth_bp.route('/minha-senha', methods=['GET', 'POST'])
@login_required
def minha_senha():
    """Permite o usuario logado trocar a propria senha. Exige senha atual."""
    if request.method == 'POST':
        atual = request.form.get('senha_atual', '')
        nova = request.form.get('nova_senha', '').strip()
        confirma = request.form.get('confirma_senha', '').strip()

        if not current_user.check_senha(atual):
            flash('Senha atual incorreta.', 'danger')
            return redirect(url_for('auth.minha_senha'))
        if len(nova) < 8:
            flash('Nova senha precisa ter pelo menos 8 caracteres.', 'warning')
            return redirect(url_for('auth.minha_senha'))
        if nova != confirma:
            flash('Confirmacao nao bate com a nova senha.', 'warning')
            return redirect(url_for('auth.minha_senha'))

        current_user.set_senha(nova)
        db.session.commit()
        flash('Senha alterada com sucesso.', 'success')
        return redirect(url_for('main.index'))

    return render_template('auth/minha_senha.html')


@auth_bp.route('/painel')
@login_required
@admin_required
def painel():
    """Painel do admin — ver todas as atribuições."""
    atribuicoes = Atribuicao.query.order_by(
        Atribuicao.status, Atribuicao.data_atribuicao.desc()
    ).all()
    return render_template('auth/painel.html', atribuicoes=atribuicoes)
