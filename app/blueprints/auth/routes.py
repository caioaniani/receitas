import json
from urllib.parse import urlparse

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.decorators import admin_required, owner_required
from app.extensions import db, limiter
from app.models import Atribuicao, Receita, Usuario
from app.services.identidade_usuario import usuario_por_identificador
from app.utils import agora


def _usuario_por_identificador(valor):
    """Localiza conta por login ou e-mail sem depender de maiúsculas.

    Mantém a correspondência exata como prioridade e só aceita as buscas
    flexíveis quando elas identificam uma única conta. Isso evita que um e-mail
    duplicado ou dois logins antigos que diferem apenas por caixa escolham o
    usuário errado.
    """
    return usuario_por_identificador(valor)


def _senha_confere(usuario, valor):
    """Tolera espaços acidentais de copiar/colar sem mudar senhas válidas."""
    if usuario.check_senha(valor):
        return True
    sem_espacos = valor.strip()
    return sem_espacos != valor and usuario.check_senha(sem_espacos)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        login_val = request.form.get('login', '').strip()
        senha = request.form.get('senha', '')

        usuario = _usuario_por_identificador(login_val)
        if usuario and _senha_confere(usuario, senha):
            # remember=True: cookie persistente (Flask-Login, ~1 ano) — a sessao
            # sobrevive a reiniciar o navegador/PC. Essencial pro kiosk do padeiro
            # nao ficar pedindo senha toda vez que reabre.
            login_user(usuario, remember=True)
            next_page = request.args.get('next')
            # Bloqueia redirect para URLs externas
            if next_page and urlparse(next_page).netloc:
                next_page = None
            if usuario.senha_provisoria:
                return redirect(url_for('auth.minha_senha'))
            if usuario.is_relatorio_loja():
                return redirect(url_for('pedidos.relatorio'))
            if usuario.somente_treino:
                return redirect(url_for('treino.home'))
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


@auth_bp.route('/csrf-token')
@login_required
def csrf_token_novo():
    """Token CSRF novo pra autosave de aba antiga. Historico: o token embutido
    na pagina expirava em 1h (default do Flask-WTF) e a tela deixada aberta
    (ex: cronograma da industria) falhava TODO save via fetch com alert
    criptico. Desde 02/07/2026 `WTF_CSRF_TIME_LIMIT=None` (config.py) — o
    token vale a sessao inteira — mas esta rota continua como rede de
    seguranca: o front detecta `csrf_expirada` (handler JSON em
    app/__init__.py, dispara em sessao trocada/cookie apagado), busca token
    novo aqui e re-tenta o POST."""
    from flask import jsonify
    from flask_wtf.csrf import generate_csrf
    return jsonify(ok=True, token=generate_csrf())


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
    usuario_id = request.args.get('usuario', type=int)
    if 'usuario' in request.args and not usuario_id:
        abort(400)
    selecionado = Usuario.query.get_or_404(usuario_id) if usuario_id else None
    usuarios = ([selecionado] if selecionado else
                Usuario.query.order_by(Usuario.papel, Usuario.nome).all())
    if current_user.is_owner:
        from app.models import DelegacaoFiscalB2B
        delegados_nf = {row[0] for row in db.session.query(DelegacaoFiscalB2B.usuario_id).all()}
    else:
        delegados_nf = set()
    lojas = (Loja.query.filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    registro_pedidos = None
    loja_pedidos = None
    loja_pedidos_configurada = None
    bloqueio_pedidos = None
    edicao_fora_prazo = False
    edicao_fora_prazo_configurada = False
    elegivel_edicao_fora_prazo = False
    edicao_por_perfil = False
    if selecionado and current_user.is_owner:
        from app.models import AcessoPedidosLoja, AppConfig
        from app.services.acesso_pedidos_loja import loja_liberada
        from app.services.pedido_edicao_acesso import (
            elegivel_edicao_fora_prazo as conta_elegivel,
        )
        from app.services.pedido_edicao_acesso import (
            pode_editar_fora_prazo,
            politica_por_perfil_ativa,
        )

        edicao_por_perfil = politica_por_perfil_ativa()
        edicao_fora_prazo = pode_editar_fora_prazo(selecionado)
        elegivel_edicao_fora_prazo = conta_elegivel(selecionado)
        try:
            registro_edicao = json.loads(AppConfig.get(
                f'pedido_edicao_fora_prazo:{selecionado.id}'))
            edicao_fora_prazo_configurada = (
                isinstance(registro_edicao, dict)
                and registro_edicao.get('autorizado') is True)
        except (TypeError, ValueError):
            pass

        registro_pedidos = db.session.get(AcessoPedidosLoja, selecionado.id)
        loja_pedidos = loja_liberada(selecionado)
        if registro_pedidos:
            loja_pedidos_configurada = db.session.get(Loja, registro_pedidos.loja_id)
        if selecionado.papel != 'funcionario' or selecionado.is_dono():
            bloqueio_pedidos = 'Esta liberação individual é destinada a contas com perfil Funcionário.'
        elif not selecionado.funcionario:
            bloqueio_pedidos = 'Vincule esta conta ao cadastro da pessoa no RH para liberar os pedidos.'
        elif not selecionado.funcionario.ativo:
            bloqueio_pedidos = 'O cadastro desta pessoa no RH está inativo. A liberação exige um cadastro ativo.'
    return render_template('auth/usuarios.html', usuarios=usuarios, lojas=lojas,
                           delegados_nf=delegados_nf, selecionado=selecionado,
                           registro_pedidos=registro_pedidos, loja_pedidos=loja_pedidos,
                           loja_pedidos_configurada=loja_pedidos_configurada,
                           bloqueio_pedidos=bloqueio_pedidos,
                           edicao_fora_prazo=edicao_fora_prazo,
                           edicao_fora_prazo_configurada=edicao_fora_prazo_configurada,
                           edicao_por_perfil=edicao_por_perfil,
                           elegivel_edicao_fora_prazo=elegivel_edicao_fora_prazo)


@auth_bp.route('/usuarios/<int:id>/edicao-pedidos-fora-prazo', methods=['POST'])
@login_required
@owner_required
def delegar_edicao_pedidos_fora_prazo(id):
    from app.models import AppConfig, AuditLog
    from app.services.pedido_edicao_acesso import definir_acesso_edicao_fora_prazo

    permitir = request.form.get('permitir')
    if permitir not in ('0', '1'):
        abort(400)
    usuario = Usuario.query.filter_by(id=id).with_for_update().first_or_404()
    antes = AppConfig.get(f'pedido_edicao_fora_prazo:{usuario.id}')
    try:
        antes_json = json.loads(antes) if antes is not None else {'autorizado': False}
    except (TypeError, ValueError):
        antes_json = {'registro_invalido': antes}
    try:
        registro = definir_acesso_edicao_fora_prazo(
            usuario, permitir == '1', current_user)
        db.session.add(AuditLog(
            tabela='pedido_edicao_acesso', registro_id=usuario.id,
            acao='update', usuario_id=current_user.id,
            antes=json.dumps(antes_json), depois=registro.value))
        db.session.commit()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    except Exception:
        db.session.rollback()
        raise
    else:
        if permitir == '1':
            flash(f'Edição fora do prazo liberada para {usuario.nome}. '
                  'Toda edição exige informar o que muda e por quê.', 'success')
        else:
            flash(f'Permissão de edição fora do prazo removida de {usuario.nome}.', 'success')
    return _voltar_ao_usuario(usuario)


@auth_bp.route('/usuarios/<int:id>/pedidos-industria', methods=['POST'])
@login_required
@owner_required
def delegar_pedidos_industria(id):
    from app.services import acesso_pedidos_loja

    usuario = Usuario.query.get_or_404(id)
    acao = request.form.get('acao')
    if acao not in ('liberar', 'revogar'):
        abort(400)
    try:
        if acao == 'revogar':
            acesso_pedidos_loja.revogar(usuario, current_user)
            mensagem = f'Permissão individual de pedidos para a indústria removida de {usuario.nome}.'
        else:
            loja_id = request.form.get('loja_id', type=int)
            if not loja_id:
                raise ValueError('Selecione a loja cujos pedidos a pessoa poderá criar e editar.')
            acesso_pedidos_loja.salvar(usuario, loja_id, current_user)
            mensagem = f'Pedidos para a indústria liberados para {usuario.nome} na loja escolhida.'
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    else:
        flash(mensagem, 'success')
    return _voltar_ao_usuario(usuario)


@auth_bp.route('/usuarios/<int:id>/nf-b2b', methods=['POST'])
@login_required
@owner_required
def delegar_nf_b2b(id):
    from app.models import DelegacaoFiscalB2B
    u = Usuario.query.get_or_404(id)
    permitir = request.form.get('permitir')
    if permitir not in ('0', '1'):
        abort(400)
    if permitir == '1' and (u.papel != 'admin' or u.somente_treino or u.is_dono()):
        abort(400)
    a = db.session.get(DelegacaoFiscalB2B, u.id)
    if permitir == '1' and not a:
        db.session.add(DelegacaoFiscalB2B(usuario_id=u.id, concedida_por_id=current_user.id))
    elif permitir == '0' and a:
        db.session.delete(a)
    db.session.commit()
    flash(f'Permissão para emitir NF B2B de {u.nome} atualizada. Outras permissões não foram alteradas.', 'success')
    return _voltar_ao_usuario(u)


@auth_bp.route('/usuarios/novo', methods=['POST'])
@login_required
@admin_required
def novo_usuario():
    import secrets

    nome = request.form.get('nome', '').strip()
    login_val = request.form.get('login', '').strip()
    email = (request.form.get('email', '') or '').strip() or None
    papel = request.form.get('papel', 'funcionario')
    from app.constants import PAPEIS_VALIDOS
    if papel not in PAPEIS_VALIDOS:
        papel = 'funcionario'

    if not nome or not login_val:
        flash('Preencha nome e login.', 'warning')
        return redirect(url_for('auth.usuarios'))

    if Usuario.query.filter_by(login=login_val).first():
        flash(f'Login "{login_val}" ja existe.', 'warning')
        return redirect(url_for('auth.usuarios'))

    loja_id = None
    if papel == 'relatorio_loja':
        from sqlalchemy import func

        from app.services.acesso_relatorio_loja import loja_operacional
        loja = loja_operacional(request.form.get('loja_id', type=int))
        if not loja or request.form.get('somente_treino'):
            flash('Selecione uma loja ativa para o relatório, sem marcar só treinamento.', 'warning')
            return redirect(url_for('auth.usuarios'))
        # Não criar uma segunda identidade com o mesmo endereço ou login.
        if (not email or len(login_val) > 50 or len(email) > 200
                or '@' not in email):
            flash('Informe um login de até 50 caracteres e um e-mail válido.', 'warning')
            return redirect(url_for('auth.usuarios'))
        if Usuario.query.filter(db.or_(
            func.lower(Usuario.email) == email.lower(),
            func.lower(Usuario.login).in_([login_val.lower(), email.lower()]),
            func.lower(Usuario.email) == login_val.lower(),
        )).first():
            flash('Já existe uma conta com esse login ou e-mail. Confira o cadastro antes de criar outro.', 'warning')
            return redirect(url_for('auth.usuarios'))
        loja_id = loja.id

    # Senha gerada — 10 chars urlsafe, legível o suficiente pra digitar uma
    # vez. O usuário troca no primeiro acesso. NUNCA fica em texto plano além
    # do email/flash (hash imediato via set_senha).
    senha = secrets.token_urlsafe(8)[:10]

    # "Só treinamento" (por pessoa, decisão do dono 23/07/2026): a conta vê só
    # /treino. Senha nasce provisória — força troca no 1º login.
    somente_treino = bool(request.form.get('somente_treino'))
    u = Usuario(nome=nome, login=login_val, email=email, papel=papel,
                loja_id=loja_id,
                senha_provisoria=True, somente_treino=somente_treino)
    u.set_senha(senha)
    db.session.add(u)
    db.session.commit()

    # Envio do email com a senha (best-effort). Sem email cadastrado ou se o
    # Postmark falhar, mostra a senha no flash pra o admin copiar e passar.
    # Conta só-treino não recebe o convite do Chatwoot (não atende cliente).
    if email:
        from app.services import email as email_svc
        res = email_svc.enviar_boas_vindas(
            email, nome, login_val, senha,
            com_chatwoot=(not somente_treino and papel not in ('observador', 'relatorio_loja')))
        if res.get('ok'):
            flash(f'Usuario "{nome}" criado! Senha enviada para {email}.',
                  'success')
        else:
            flash(f'Usuario "{nome}" criado, mas o email falhou '
                  f'({res.get("erro")}). Senha: {senha} — copie e passe '
                  'manualmente.', 'warning')
    else:
        flash(f'Usuario "{nome}" criado! Senha: {senha} — copie e passe '
              '(nenhum email cadastrado).', 'success')
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

    from app.models import AcessoPedidosLoja
    if AcessoPedidosLoja.query.filter(
            (AcessoPedidosLoja.usuario_id == u.id)
            | (AcessoPedidosLoja.concedido_por_id == u.id)
            | (AcessoPedidosLoja.atualizado_por_id == u.id)).first():
        flash('Esta conta possui histórico de permissões de pedidos. '
              'Mantenha o cadastro para preservar esse histórico; '
              'a permissão individual pode ser removida na tela de acesso.', 'warning')
        return _voltar_ao_usuario(u)

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
        return _voltar_ao_usuario(u)

    papel = (request.form.get('papel') or '').strip()
    from app.constants import PAPEIS_VALIDOS
    if papel not in PAPEIS_VALIDOS:
        flash('Papel invalido.', 'warning')
        return _voltar_ao_usuario(u)

    if papel == 'relatorio_loja':
        from app.services.acesso_relatorio_loja import loja_operacional
        loja = loja_operacional(request.form.get('loja_id', type=int))
        if not loja or u.somente_treino or u.id == current_user.id:
            flash('Selecione uma loja ativa e retire a restrição só treinamento. Não é permitido restringir a própria conta.', 'warning')
            return _voltar_ao_usuario(u)
        u.loja_id = loja.id
    u.papel = papel
    db.session.commit()
    flash(f'Papel de "{u.nome}" alterado para {papel}.', 'success')
    return _voltar_ao_usuario(u)


def _voltar_ao_usuario(usuario):
    """Mantém o foco na conta escolhida, sem aceitar uma URL de retorno."""
    if request.form.get('voltar_acesso') == str(usuario.id):
        return redirect(url_for('auth.usuarios', usuario=usuario.id))
    return redirect(url_for('auth.usuarios'))


@auth_bp.route('/usuarios/<int:id>/somente-treino', methods=['POST'])
@login_required
@admin_required
def toggle_somente_treino(id):
    """Liga/desliga o acesso SÓ TREINAMENTO da conta (por pessoa — decisão do
    dono 23/07/2026). Owner nunca é restrito."""
    u = Usuario.query.get_or_404(id)
    if u.is_owner:
        flash('Owner não pode ser restrito a treinamento.', 'warning')
        return _voltar_ao_usuario(u)
    if u.id == current_user.id:
        # Auto-lockout: marcar a si mesmo prenderia você em /treino e você não
        # conseguiria nem se desmarcar (a rota não é treino.*).
        flash('Você não pode restringir a sua própria conta a treinamento.',
              'warning')
        return _voltar_ao_usuario(u)
    if u.is_relatorio_loja():
        flash('O perfil de relatório já é restrito à consulta da loja e não pode receber treinamento.', 'warning')
        return _voltar_ao_usuario(u)
    u.somente_treino = not u.somente_treino
    db.session.commit()
    estado = 'agora vê SÓ treinamento' if u.somente_treino else 'voltou ao acesso normal'
    flash(f'"{u.nome}" {estado}.', 'success')
    return _voltar_ao_usuario(u)


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
    # Admin resetou → é provisória de novo: força o dono da conta a definir a
    # dele no próximo login.
    u.senha_provisoria = True
    db.session.commit()
    flash(f'Senha de "{u.nome}" alterada (ele troca no próximo acesso).',
          'success')
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

        if not _senha_confere(current_user, atual):
            flash('Senha atual incorreta.', 'danger')
            return redirect(url_for('auth.minha_senha'))
        if len(nova) < 8:
            flash('Nova senha precisa ter pelo menos 8 caracteres.', 'warning')
            return redirect(url_for('auth.minha_senha'))
        if nova != confirma:
            flash('Confirmacao nao bate com a nova senha.', 'warning')
            return redirect(url_for('auth.minha_senha'))
        # A nova senha tem que ser DIFERENTE da atual — na troca forçada, isso
        # impede "trocar" pela mesma senha provisória do e-mail (que pode ter
        # sido interceptada) sem rotacionar de fato.
        if nova == atual:
            flash('A nova senha precisa ser diferente da atual.', 'warning')
            return redirect(url_for('auth.minha_senha'))

        current_user.set_senha(nova)
        # Trocou → não é mais provisória: libera o gate global (some_treino/
        # navegação normal). Enquanto provisória, o before_request prendia aqui.
        current_user.senha_provisoria = False
        so_treino = getattr(current_user, 'somente_treino', False)
        db.session.commit()
        # Atualiza a sessão persistente com o estado recém-gravado. Em especial
        # no Safari móvel, evita que a conta continue parecendo provisória.
        login_user(current_user._get_current_object(), remember=True, fresh=True)
        flash('Senha alterada com sucesso.', 'success')
        # Conta só-treino vai direto pro treino (senão o gate rebateria de
        # main.index pra lá num salto extra).
        destino = ('pedidos.relatorio' if current_user.is_relatorio_loja()
                   else 'treino.home' if so_treino else 'main.index')
        return redirect(url_for(destino))

    return render_template('auth/minha_senha.html',
                           forcado=bool(getattr(current_user,
                                                'senha_provisoria', False)))


@auth_bp.route('/painel')
@login_required
@admin_required
def painel():
    """Painel do admin — ver todas as atribuições."""
    atribuicoes = Atribuicao.query.order_by(
        Atribuicao.status, Atribuicao.data_atribuicao.desc()
    ).all()
    return render_template('auth/painel.html', atribuicoes=atribuicoes)
