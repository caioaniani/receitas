"""Sistema de treinamento GAMIFICADO (spec v1.0) — rotas §12.

Funcionário (/treino), gestor (/treino/gestor), admin (/treino/admin) e a rota
pública de verificação de certificado. Tudo keyed em Funcionario (resolvido do
Usuario de login). Toda a lógica de pontos/anti-fraude vive nos serviços
treino_*; aqui é só orquestração + gate de papel.
"""
from functools import wraps

from flask import (
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.treino import treino_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (
    Funcionario,
    TreinoAplicacaoPratica,
    TreinoCheckpoint,
    TreinoQuiz,
    TreinoRecompensa,
    TreinoResgate,
    TreinoSelo,
    TreinoTentativaQuiz,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_aplicacao as ap
from app.services import treino_certificado as cert
from app.services import treino_ledger as ledger
from app.services import treino_quiz as tq
from app.services import treino_ranking as rk
from app.services import treino_recompensa as rc
from app.services import treino_trilha as tt
from app.services import treino_video as tv


def _func():
    """Funcionario do usuário logado (None se a conta não é de funcionário)."""
    return tv.resolver_funcionario(current_user)


def _func_obrigatorio():
    f = _func()
    if f is None:
        abort(403)
    return f


def gestor_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if ledger.papel_treino(current_user) not in ('GESTOR', 'ADMIN'):
            abort(403)
        return fn(*a, **kw)
    return wrapper


def _temp():
    return ledger.temporada_ativa()


# ── Funcionário ─────────────────────────────────────────────────────────
@treino_bp.route('/')
@login_required
def home():
    f = _func()
    temp = _temp()
    trilhas = TreinoTrilha.query.filter_by(ativa=True).order_by(
        TreinoTrilha.ordem).all()
    ctx = {'trilhas': trilhas, 'func': f, 'temp': temp}
    if f and temp:
        ctx['pos'] = rk.posicao_individual(f, temp.id)
        ctx['progresso'] = {
            t.id: tt.progresso_trilha(f, t, temp) for t in trilhas}
    return render_template('treino/home.html', **ctx)


@treino_bp.route('/trilha/<int:id>')
@login_required
def trilha(id):
    t = TreinoTrilha.query.filter_by(id=id, ativa=True).first_or_404()
    f = _func()
    temp = _temp()
    est = tt.progresso_trilha(f, t, temp) if (f and temp) else None
    quizzes = TreinoQuiz.query.filter_by(
        trilha_id=t.id, ativo=True).all()
    return render_template('treino/trilha.html', t=t, est=est, quizzes=quizzes)


@treino_bp.route('/video/<int:id>')
@login_required
def video(id):
    v = TreinoVideo.query.filter_by(id=id, ativo=True).first_or_404()
    from app.services import treinamento_stream as ts
    embed = ts.embed_url(v.video_externo_id) if v.video_externo_id else None
    quizzes = TreinoQuiz.query.filter_by(video_id=v.id, ativo=True).all()
    return render_template('treino/video.html', v=v, video_embed=embed,
                           checkpoints=v.checkpoints, quizzes=quizzes)


@treino_bp.route('/api/videos/<int:id>/heartbeat', methods=['POST'])
@login_required
def api_heartbeat(id):
    v = TreinoVideo.query.filter_by(id=id, ativo=True).first_or_404()
    f = _func_obrigatorio()
    try:
        pos = float(request.form.get('t') or 0)
        vel = float(request.form.get('v') or 1.0)
    except (TypeError, ValueError):
        pos, vel = 0.0, 1.0
    return jsonify(ok=True, **tv.heartbeat(f, v, pos, vel))


@treino_bp.route('/api/checkpoints/<int:id>/resposta', methods=['POST'])
@login_required
def api_checkpoint(id):
    cp = db.session.get(TreinoCheckpoint, id) or abort(404)
    f = _func_obrigatorio()
    try:
        idx = int(request.form.get('indice'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='índice inválido'), 400
    return jsonify(ok=True, **tv.responder_checkpoint(f, cp, idx))


@treino_bp.route('/api/quiz/<int:id>/iniciar', methods=['POST'])
@login_required
def api_quiz_iniciar(id):
    quiz = TreinoQuiz.query.filter_by(id=id, ativo=True).first_or_404()
    f = _func_obrigatorio()
    try:
        t = tq.iniciar_tentativa(f, quiz)
    except tq.CooldownError as e:
        return jsonify(ok=False, erro='cooldown',
                       liberado_em=e.liberado_em.isoformat()), 429
    return jsonify(ok=True, tentativa_id=t.id, questoes=t.questoes_sorteadas,
                   nota_minima=float(quiz.nota_minima))


@treino_bp.route('/api/tentativas/<int:id>/responder', methods=['POST'])
@login_required
def api_responder(id):
    t = db.session.get(TreinoTentativaQuiz, id) or abort(404)
    f = _func_obrigatorio()
    if t.funcionario_id != f.id:
        abort(403)
    return jsonify(**tq.responder(
        t, request.form.get('questao_id'), request.form.get('alternativa_id'),
        request.form.get('segundos', 0)))


@treino_bp.route('/api/tentativas/<int:id>/finalizar', methods=['POST'])
@login_required
def api_finalizar(id):
    t = db.session.get(TreinoTentativaQuiz, id) or abort(404)
    f = _func_obrigatorio()
    if t.funcionario_id != f.id:
        abort(403)
    quiz = db.session.get(TreinoQuiz, t.quiz_id)
    res = tq.finalizar(t, quiz)
    # tenta fechar a trilha (pode emitir selo)
    temp = _temp()
    if temp and quiz.trilha_id:
        trilha_obj = db.session.get(TreinoTrilha, quiz.trilha_id)
        tt.verificar_conclusao(f, trilha_obj, temp)
    return jsonify(ok=True, **res)


@treino_bp.route('/ranking')
@login_required
def ranking():
    temp = _temp()
    f = _func()
    unidades = rk.ranking_unidades(temp.id) if temp else []
    pos = rk.posicao_individual(f, temp.id) if (f and temp) else None
    return render_template('treino/ranking.html', unidades=unidades, pos=pos)


@treino_bp.route('/recompensas')
@login_required
def recompensas():
    f = _func()
    temp = _temp()
    lista = TreinoRecompensa.query.filter_by(ativa=True).all()
    saldo = ledger.saldo(f.id, temp.id) if (f and temp) else 0
    return render_template('treino/recompensas.html', recompensas=lista,
                           saldo=saldo)


@treino_bp.route('/api/resgates', methods=['POST'])
@login_required
def api_resgatar():
    f = _func_obrigatorio()
    temp = _temp()
    rec = db.session.get(TreinoRecompensa, request.form.get('recompensa_id', 0))
    if rec is None or temp is None:
        return jsonify(ok=False, erro='indisponível'), 400
    try:
        rc.solicitar(f, rec, temp)
    except rc.ResgateError as e:
        return jsonify(ok=False, erro=str(e)), 400
    flash('Resgate solicitado — aguarde a aprovação do gestor.', 'success')
    return jsonify(ok=True)


@treino_bp.route('/extrato')
@login_required
def extrato():
    f = _func_obrigatorio()
    temp = _temp()
    from app.models import TreinoEventoPontos
    eventos = (TreinoEventoPontos.query.filter_by(funcionario_id=f.id)
               .order_by(TreinoEventoPontos.criado_em.desc()).limit(200).all()) \
        if f else []
    saldo = ledger.saldo(f.id, temp.id) if (f and temp) else 0
    return render_template('treino/extrato.html', eventos=eventos, saldo=saldo)


@treino_bp.route('/certificado/<int:selo_id>.pdf')
@login_required
def certificado(selo_id):
    selo = db.session.get(TreinoSelo, selo_id) or abort(404)
    f = _func()
    # dono do selo, ou gestor/admin
    if not (f and selo.funcionario_id == f.id) and \
            ledger.papel_treino(current_user) not in ('GESTOR', 'ADMIN'):
        abort(403)
    pdf = cert.gerar_pdf(selo, base_url=request.host_url.rstrip('/'))
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'inline; filename=certificado-{selo_id}.pdf'})


# ── Rota pública de verificação (§11) ───────────────────────────────────
@treino_bp.route('/verificar/<codigo>')
def verificar(codigo):
    selo = cert.por_codigo(codigo)
    dados = cert.dados_certificado(selo) if selo else None
    return render_template('treino/verificar.html', selo=selo, dados=dados)


# ── Gestor ──────────────────────────────────────────────────────────────
@treino_bp.route('/gestor/')
@login_required
@gestor_required
def gestor_home():
    gestor = _func()
    unidade = ledger.unidade_do_funcionario(gestor) if gestor else None
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    if is_admin:
        equipe = Funcionario.query.filter_by(ativo=True).order_by(
            Funcionario.nome).all()
    else:
        equipe = [f for f in (unidade.funcionarios if unidade else [])
                  if f.ativo]
    trilhas = TreinoTrilha.query.filter_by(ativa=True).all()
    return render_template('treino/gestor.html', equipe=equipe, trilhas=trilhas,
                           unidade=unidade)


@treino_bp.route('/gestor/api/aplicacao', methods=['POST'])
@login_required
@gestor_required
def gestor_aplicacao():
    gestor = _func_obrigatorio()
    temp = _temp()
    f = db.session.get(Funcionario, request.form.get('funcionario_id', 0))
    trilha_obj = db.session.get(TreinoTrilha, request.form.get('trilha_id', 0))
    if not (f and trilha_obj and temp):
        return jsonify(ok=False, erro='dados incompletos'), 400
    itens = request.form.getlist('itens_ok')
    try:
        ap.registrar(gestor, f, trilha_obj, temp, itens,
                     request.form.get('evidencia', ''),
                     criado_por_id=current_user.id,
                     is_admin=ledger.papel_treino(current_user) == 'ADMIN')
    except ap.AplicacaoError as e:
        return jsonify(ok=False, erro=str(e)), 400
    # pode fechar a trilha
    tt.verificar_conclusao(f, trilha_obj, temp)
    return jsonify(ok=True)


@treino_bp.route('/gestor/resgates')
@login_required
@gestor_required
def gestor_resgates():
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    gestor = _func()
    q = TreinoResgate.query.filter_by(status='SOLICITADO')
    pendentes = q.order_by(TreinoResgate.solicitado_em).all()
    if not is_admin and gestor is not None:
        unidade = ledger.unidade_do_funcionario(gestor)
        ids = {f.id for f in (unidade.funcionarios if unidade else [])}
        pendentes = [r for r in pendentes if r.funcionario_id in ids]
    return render_template('treino/gestor_resgates.html', pendentes=pendentes)


@treino_bp.route('/gestor/api/resgates/<int:id>/decidir', methods=['POST'])
@login_required
@gestor_required
def gestor_decidir(id):
    r = db.session.get(TreinoResgate, id) or abort(404)
    aprovar = request.form.get('acao') == 'aprovar'
    try:
        if aprovar:
            rc.aprovar(r, decidido_por_id=current_user.id)
        else:
            rc.recusar(r, decidido_por_id=current_user.id)
    except rc.ResgateError as e:
        return jsonify(ok=False, erro=str(e)), 400
    return jsonify(ok=True)


# ── Admin (autoria + gestão) ────────────────────────────────────────────
@treino_bp.route('/admin/')
@login_required
@admin_required
def admin_home():
    trilhas = TreinoTrilha.query.order_by(TreinoTrilha.ordem).all()
    from app.services import treino_pontos as cfg
    return render_template('treino/admin.html', trilhas=trilhas,
                           pontos=cfg.todos())


@treino_bp.route('/admin/aplicacoes/<int:id>/estornar', methods=['POST'])
@login_required
@admin_required
def admin_estornar_aplicacao(id):
    a = db.session.get(TreinoAplicacaoPratica, id) or abort(404)
    ap.estornar(a, criado_por_id=current_user.id)
    flash('Aplicação estornada.', 'success')
    return redirect(request.referrer or url_for('treino.admin_home'))
