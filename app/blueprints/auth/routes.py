from urllib.parse import urlparse

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app.blueprints.auth import auth_bp
from app.extensions import db, limiter
from app.utils import agora
from app.models import Usuario, Atribuicao, Receita


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
            login_user(usuario)
            next_page = request.args.get('next')
            # Bloqueia redirect para URLs externas
            if next_page and urlparse(next_page).netloc:
                next_page = None
            if usuario.is_admin():
                return redirect(next_page or url_for('main.index'))
            else:
                return redirect(url_for('auth.minhas_fichas'))
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
def usuarios():
    """Gerenciar usuários — só admin."""
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    from app.models import Loja
    usuarios = Usuario.query.order_by(Usuario.papel, Usuario.nome).all()
    lojas = (Loja.query.filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    return render_template('auth/usuarios.html', usuarios=usuarios, lojas=lojas)


@auth_bp.route('/usuarios/novo', methods=['POST'])
@login_required
def novo_usuario():
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    nome = request.form.get('nome', '').strip()
    login_val = request.form.get('login', '').strip()
    senha = request.form.get('senha', '').strip()
    papel = request.form.get('papel', 'funcionario')
    PAPEIS_VALIDOS = {'admin', 'gerente', 'producao', 'rh', 'funcionario'}
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
def excluir_usuario(id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    u = Usuario.query.get_or_404(id)
    if u.id == current_user.id:
        flash('Voce nao pode excluir a si mesmo.', 'warning')
        return redirect(url_for('auth.usuarios'))

    Atribuicao.query.filter_by(usuario_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    flash(f'Usuario "{u.nome}" excluido.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/papel', methods=['POST'])
@login_required
def alterar_papel(id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    u = Usuario.query.get_or_404(id)
    if u.is_owner:
        flash('Owner nao pode ter o papel alterado.', 'warning')
        return redirect(url_for('auth.usuarios'))

    papel = (request.form.get('papel') or '').strip()
    PAPEIS_VALIDOS = {'admin', 'gerente', 'producao', 'rh', 'funcionario'}
    if papel not in PAPEIS_VALIDOS:
        flash('Papel invalido.', 'warning')
        return redirect(url_for('auth.usuarios'))

    u.papel = papel
    db.session.commit()
    flash(f'Papel de "{u.nome}" alterado para {papel}.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/alterar-loja', methods=['POST'])
@login_required
def alterar_loja(id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))
    u = Usuario.query.get_or_404(id)
    raw = (request.form.get('loja_id') or '').strip()
    if raw == '':
        u.loja_id = None
        db.session.commit()
        flash(f'"{u.nome}" desvinculado de loja.', 'success')
        return redirect(url_for('auth.usuarios'))
    try:
        loja_id = int(raw)
    except ValueError:
        flash('Loja invalida.', 'warning')
        return redirect(url_for('auth.usuarios'))
    from app.models import Loja
    if not Loja.query.get(loja_id):
        flash('Loja nao encontrada.', 'warning')
        return redirect(url_for('auth.usuarios'))
    u.loja_id = loja_id
    db.session.commit()
    flash(f'"{u.nome}" vinculado a nova loja.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/reset-senha', methods=['POST'])
@login_required
def reset_senha(id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    u = Usuario.query.get_or_404(id)
    nova_senha = request.form.get('nova_senha', '').strip()
    if not nova_senha:
        flash('Preencha a nova senha.', 'warning')
        return redirect(url_for('auth.usuarios'))

    u.set_senha(nova_senha)
    db.session.commit()
    flash(f'Senha de "{u.nome}" alterada.', 'success')
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/atribuir/<int:receita_id>', methods=['POST'])
@login_required
def atribuir(receita_id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

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
        flash(f'Esta ficha ja foi atribuida a este funcionario.', 'warning')
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
    from datetime import datetime
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
def excluir_atribuicao(id):
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    atrib = Atribuicao.query.get_or_404(id)
    db.session.delete(atrib)
    db.session.commit()
    flash('Atribuicao removida.', 'success')
    return redirect(url_for('auth.painel'))


@auth_bp.route('/painel')
@login_required
def painel():
    """Painel do admin — ver todas as atribuições."""
    if not current_user.is_admin():
        flash('Acesso negado.', 'danger')
        return redirect(url_for('auth.minhas_fichas'))

    atribuicoes = Atribuicao.query.order_by(
        Atribuicao.status, Atribuicao.data_atribuicao.desc()
    ).all()
    return render_template('auth/painel.html', atribuicoes=atribuicoes)
