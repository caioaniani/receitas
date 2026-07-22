"""Módulo de treinamento gamificado (24/07/2026, pedido do dono).

- ADMIN: cria o treinamento, sobe o vídeo (self-host no volume /data), monta
  o quiz com nota de corte; gera o acesso dos funcionários (por e-mail) e vê
  os elegíveis ao sorteio/bônus.
- FUNCIONÁRIO: assiste (marca como assistido), responde o quiz, pontua.
  Completar = assistir + passar; elegível = completou TODOS os ativos.

O vídeo é servido com HTTP Range pela MESMA origem — nada de terceiro, o
funcionário nunca sai do site. Regras em app/services/treinamento.py.
"""
from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.treinamento import treinamento_bp
from app.decorators import admin_required
from app.extensions import csrf, db
from app.models import (
    Funcionario,
    Treinamento,
    TreinamentoOpcao,
    TreinamentoPergunta,
)
from app.services import treinamento as svc
from app.services import treinamento_video as tv
from app.utils import agora


def _ativos():
    return Treinamento.query.filter(Treinamento.apagado_em.is_(None))


# ── Admin: autoria ──────────────────────────────────────────────────────
@treinamento_bp.route('/admin')
@login_required
@admin_required
def admin_lista():
    treinos = _ativos().order_by(Treinamento.ordem, Treinamento.id).all()
    return render_template('treinamento/admin_lista.html', treinos=treinos)


@treinamento_bp.route('/admin/novo', methods=['POST'])
@login_required
@admin_required
def admin_novo():
    titulo = (request.form.get('titulo') or '').strip()[:200]
    if not titulo:
        flash('Dê um título ao treinamento.', 'warning')
        return redirect(url_for('treinamento.admin_lista'))
    ordem = (db.session.query(db.func.max(Treinamento.ordem)).scalar() or 0) + 1
    # Nasce RASCUNHO (ativo=False): não aparece pro funcionário nem entra na
    # elegibilidade até o admin subir vídeo/quiz e publicar (marcar "ativo").
    t = Treinamento(titulo=titulo, criado_por_id=current_user.id, ordem=ordem,
                    ativo=False)
    db.session.add(t)
    db.session.commit()
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/<int:id>')
@login_required
@admin_required
def admin_editar(id):
    t = _ativos().filter_by(id=id).first_or_404()
    return render_template('treinamento/admin_editar.html', t=t)


@treinamento_bp.route('/admin/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def admin_salvar(id):
    t = _ativos().filter_by(id=id).first_or_404()
    titulo = (request.form.get('titulo') or '').strip()[:200]
    if titulo:
        t.titulo = titulo
    t.descricao = (request.form.get('descricao') or '').strip() or None
    try:
        nm = int(request.form.get('nota_minima') or 70)
    except (TypeError, ValueError):
        nm = 70
    t.nota_minima = max(0, min(100, nm))
    t.ativo = request.form.get('ativo') == '1'
    db.session.commit()
    flash('Treinamento salvo.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/<int:id>/video', methods=['POST'])
@login_required
@admin_required
@csrf.exempt
def admin_video(id):
    """Upload de vídeo por XHR: o navegador manda o arquivo como CORPO BRUTO
    (não multipart) com a barra de progresso, e o token do CSRF vai na QUERY
    (?csrf=). Isso evita o parse de formulário — que, sob o teto de 25 MB do
    resto do app, estourava o vídeo ("sessão expirada"). Responde com código
    HTTP + texto curto; o front recarrega ou mostra o erro."""
    request.max_content_length = current_app.config['TREINAMENTO_MAX_VIDEO']
    # CSRF na mão, a partir da QUERY (não há form pra ler o token).
    if current_app.config.get('WTF_CSRF_ENABLED', True):
        from flask_wtf.csrf import validate_csrf
        try:
            validate_csrf(request.args.get('csrf'))
        except Exception:  # noqa: BLE001 — token ausente/inválido
            return ('Sessão de segurança expirada — recarregue a página.', 400)
    t = _ativos().filter_by(id=id).first_or_404()
    nome = request.args.get('nome', '')
    try:
        ref = tv.salvar_stream(request.stream, t.id, nome)
    except ValueError as e:
        return (str(e), 400)
    # Troca o vídeo: apaga o arquivo antigo (se era self-host) e aponta o novo.
    if t.video_tipo == 'arquivo' and t.video_ref:
        tv.remover_video(t.video_ref)
    t.video_tipo = 'arquivo'
    t.video_ref = ref
    db.session.commit()
    return ('', 204)


@treinamento_bp.route('/admin/<int:id>/video/chunk', methods=['POST'])
@login_required
@admin_required
@csrf.exempt
def admin_video_chunk(id):
    """Upload de vídeo por PEDAÇOS (chunked). O navegador fatia o arquivo e
    manda cada pedaço numa request pequena (corpo bruto, ~4 MB). Cada request
    fica MUITO abaixo do teto de 25 MB, sobe em segundos (não estoura o timeout
    do worker) e passa por qualquer limite de proxy — é assim que vídeo grande
    (5-10 min) sobe de forma confiável. Query: ?csrf=&upload=<token hex>&i=
    <índice>&n=<total>&nome=<arquivo>. Responde 204 a cada pedaço; no último,
    finaliza (renomeia, troca o vídeo antigo, grava no banco)."""
    if current_app.config.get('WTF_CSRF_ENABLED', True):
        from flask_wtf.csrf import validate_csrf
        try:
            validate_csrf(request.args.get('csrf'))
        except Exception:  # noqa: BLE001 — token ausente/inválido
            return ('Sessão de segurança expirada — recarregue a página.', 400)
    t = _ativos().filter_by(id=id).first_or_404()
    token = request.args.get('upload', '')
    nome = request.args.get('nome', '')
    try:
        i = int(request.args.get('i', ''))
        n = int(request.args.get('n', ''))
    except (TypeError, ValueError):
        return ('Parâmetros de upload inválidos.', 400)
    if i < 0 or n < 1 or i >= n:
        return ('Parâmetros de upload inválidos.', 400)
    if i == 0:
        tv.limpar_parciais()   # varre restos de uploads abandonados
    try:
        tv.anexar_chunk(request.stream, t.id, token, i, nome,
                        current_app.config['TREINAMENTO_MAX_VIDEO'])
    except ValueError as e:
        return (str(e), 400)
    if i < n - 1:
        return ('', 204)                     # ainda faltam pedaços
    # Último pedaço: fecha o arquivo e aponta o treinamento pra ele.
    try:
        ref = tv.finalizar_chunk(t.id, token, nome)
    except ValueError as e:
        return (str(e), 400)
    if t.video_tipo == 'arquivo' and t.video_ref:
        tv.remover_video(t.video_ref)
    t.video_tipo = 'arquivo'
    t.video_ref = ref
    db.session.commit()
    return ('', 204)


@treinamento_bp.route('/admin/<int:id>/pergunta', methods=['POST'])
@login_required
@admin_required
def admin_add_pergunta(id):
    t = _ativos().filter_by(id=id).first_or_404()
    enunciado = (request.form.get('enunciado') or '').strip()
    try:
        correta_slot = int(request.form.get('correta'))
    except (TypeError, ValueError):
        correta_slot = -1
    # A correta é o ÍNDICE DO SLOT (do radio), não da lista filtrada — se um
    # slot ficar vazio, a marcação não pode "escorregar" pra outra opção.
    # Guarda (texto, correta) por slot NÃO-vazio, correta = slot marcado.
    pares = [(o.strip(), i == correta_slot)
             for i, o in enumerate(request.form.getlist('opcao[]'))
             if o.strip()]
    if not enunciado or len(pares) < 2 or not any(c for _, c in pares):
        flash('A pergunta precisa de enunciado, ao menos 2 opções e a '
              'correta marcada (num slot preenchido).', 'warning')
        return redirect(url_for('treinamento.admin_editar', id=t.id))
    ordem = (db.session.query(db.func.max(TreinamentoPergunta.ordem))
             .filter_by(treinamento_id=t.id).scalar() or 0) + 1
    p = TreinamentoPergunta(treinamento_id=t.id, enunciado=enunciado,
                            ordem=ordem)
    db.session.add(p)
    db.session.flush()
    for i, (texto, correta) in enumerate(pares):
        db.session.add(TreinamentoOpcao(
            pergunta_id=p.id, texto=texto[:500], correta=correta, ordem=i))
    db.session.commit()
    flash('Pergunta adicionada.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/pergunta/<int:pid>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_del_pergunta(pid):
    p = TreinamentoPergunta.query.get_or_404(pid)
    tid = p.treinamento_id
    db.session.delete(p)
    db.session.commit()
    flash('Pergunta removida.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=tid))


@treinamento_bp.route('/admin/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_excluir(id):
    t = _ativos().filter_by(id=id).first_or_404()
    t.apagado_em = agora()
    db.session.commit()
    flash('Treinamento arquivado.', 'success')
    return redirect(url_for('treinamento.admin_lista'))


# ── Vídeo (serve com Range; admin preview + funcionário) ────────────────
@treinamento_bp.route('/video/<int:id>')
@login_required
def video(id):
    t = _ativos().filter_by(id=id).first_or_404()
    # Só serve o vídeo de treinamento ATIVO (publicado). O admin pode ver
    # rascunho pra pré-visualizar. Sem esse gate, funcionário adivinharia o id
    # e baixaria treinamento não publicado (achado da revisão 24/07).
    if not (t.ativo or current_user.is_admin()):
        abort(404)
    if t.video_tipo != 'arquivo' or not t.video_ref:
        abort(404)
    return tv.resposta_range(t.video_ref)


def _ativo_visivel(id):
    return (_ativos().filter(Treinamento.ativo.is_(True))
            .filter_by(id=id).first_or_404())


# ── Funcionário: assistir + quiz ────────────────────────────────────────
@treinamento_bp.route('/')
@login_required
def index():
    prog = svc.progresso(current_user)
    return render_template(
        'treinamento/aluno_lista.html', prog=prog,
        pontos=sum(p['melhor_pontos'] for p in prog),
        completos=sum(1 for p in prog if p['completo']), total=len(prog))


@treinamento_bp.route('/<int:id>/assistir')
@login_required
def assistir(id):
    t = _ativo_visivel(id)
    c = svc.conclusao_de(current_user.id, t.id)
    return render_template('treinamento/aluno_assistir.html', t=t,
                           assistido=bool(c and c.assistido_em),
                           aprovado=bool(c and c.aprovado_em))


@treinamento_bp.route('/<int:id>/assistido', methods=['POST'])
@login_required
def assistido(id):
    t = _ativo_visivel(id)
    svc.marcar_assistido(t, current_user)
    return redirect(url_for('treinamento.assistir', id=t.id))


@treinamento_bp.route('/<int:id>/quiz', methods=['POST'])
@login_required
def responder(id):
    t = _ativo_visivel(id)
    c = svc.conclusao_de(current_user.id, t.id)
    if not (c and c.assistido_em):
        flash('Assista o vídeo e marque como assistido antes do quiz.',
              'warning')
        return redirect(url_for('treinamento.assistir', id=t.id))
    respostas = {}
    for p in t.perguntas:
        v = request.form.get(f'pergunta_{p.id}')
        if v:
            try:
                respostas[p.id] = int(v)
            except ValueError:
                pass
    res = svc.corrigir_e_registrar(t, current_user, respostas)
    return render_template('treinamento/aluno_resultado.html', t=t, res=res)


# ── Admin: acessos dos funcionários + elegíveis ─────────────────────────
@treinamento_bp.route('/admin/acessos')
@login_required
@admin_required
def admin_acessos():
    funcs = (Funcionario.query.filter_by(ativo=True)
             .order_by(Funcionario.nome).all())
    return render_template('treinamento/admin_acessos.html', funcs=funcs)


@treinamento_bp.route('/admin/acessos/<int:func_id>/gerar', methods=['POST'])
@login_required
@admin_required
def admin_gerar_acesso(func_id):
    f = Funcionario.query.get_or_404(func_id)
    r = svc.gerar_acesso(f)
    if r['motivo'] == 'sem_email':
        flash(f'{f.nome}: cadastre o e-mail no RH antes de gerar o acesso.',
              'warning')
    elif r['motivo'] == 'ja_tem':
        flash(f'{f.nome} já tem acesso.', 'info')
    elif r['motivo'] == 'conta_de_outro_papel':
        flash(f'O e-mail de {f.nome} já é de uma conta de admin/gerente — '
              'não vinculei (seria a conta errada). Use outro e-mail no RH.',
              'danger')
    elif r['motivo'] == 'email_em_uso':
        flash(f'O e-mail de {f.nome} já está vinculado a outro funcionário — '
              'confira o cadastro.', 'danger')
    elif r['motivo'] == 'vinculado':
        flash(f'{f.nome} vinculado a uma conta existente.', 'success')
    elif r['motivo'] == 'criado':
        if r.get('email_ok'):
            flash(f'Acesso de {f.nome} criado — senha enviada por e-mail.',
                  'success')
        else:
            flash(f'Acesso de {f.nome} criado, mas o e-mail falhou '
                  f'({r.get("email_erro")}). Senha: {r.get("senha")} — '
                  'passe manualmente.', 'warning')
    return redirect(url_for('treinamento.admin_acessos'))


@treinamento_bp.route('/admin/elegiveis')
@login_required
@admin_required
def admin_elegiveis():
    return render_template('treinamento/admin_elegiveis.html',
                           elegiveis=svc.elegiveis(),
                           n_ativos=len(svc.treinamentos_ativos()))
