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
    ctx = {'trilhas': trilhas, 'func': f, 'temp': temp, 'onboarding_ids': set()}
    if f:
        from app.services import treino_onboarding as ob
        ctx['onboarding_ids'] = {t.id for t in ob.onboarding_do_funcionario(f)}
        ctx['progressao'] = ob.progressao(f)
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
    from app.models import Cargo, TreinoTemporada, TreinoTrilhaCargo
    from app.services import treino_pontos as cfg
    trilhas = TreinoTrilha.query.order_by(TreinoTrilha.ordem).all()
    # mapa trilha_id -> conjunto de cargo_ids exigidos (v2 §16.1)
    cargos_por_trilha = {}
    vinculados = set()
    for m in TreinoTrilhaCargo.query.all():
        cargos_por_trilha.setdefault(m.trilha_id, set()).add(m.cargo_id)
        vinculados.add(m.cargo_id)
    # cargos ativos + os já vinculados a alguma trilha (mesmo se desativados) —
    # sem os vinculados, salvar o form de uma trilha apagaria em silêncio o
    # vínculo a um cargo que foi desativado depois (achado da revisão).
    cargos = Cargo.query.filter(db.or_(
        Cargo.ativo.is_(True), Cargo.id.in_(vinculados) if vinculados else
        db.false())).order_by(Cargo.nome).all()
    return render_template(
        'treino/admin.html', trilhas=trilhas, pontos=cfg.todos(),
        temporadas=TreinoTemporada.query.order_by(
            TreinoTemporada.inicio.desc()).all(),
        recompensas=TreinoRecompensa.query.all(),
        cargos=cargos, cargos_por_trilha=cargos_por_trilha,
        funcionarios=Funcionario.query.filter_by(ativo=True).order_by(
            Funcionario.nome).all())


@treino_bp.route('/admin/aplicacoes/<int:id>/estornar', methods=['POST'])
@login_required
@admin_required
def admin_estornar_aplicacao(id):
    a = db.session.get(TreinoAplicacaoPratica, id) or abort(404)
    ap.estornar(a, criado_por_id=current_user.id)
    flash('Aplicação estornada.', 'success')
    return redirect(request.referrer or url_for('treino.admin_home'))


# ── Admin: AUTORIA (criar conteúdo) ─────────────────────────────────────
def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


@treino_bp.route('/admin/temporada', methods=['POST'])
@login_required
@admin_required
def admin_temporada():
    from datetime import date

    from app.models import TreinoTemporada
    try:
        ini = date.fromisoformat(request.form['inicio'])
        fim = date.fromisoformat(request.form['fim'])
    except (KeyError, ValueError):
        flash('Datas inválidas.', 'warning')
        return redirect(url_for('treino.admin_home'))
    t = TreinoTemporada(nome=request.form.get('nome', 'Temporada')[:100],
                        inicio=ini, fim=fim,
                        status=request.form.get('status', 'ATIVA'))
    db.session.add(t)
    db.session.commit()
    flash('Temporada criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha', methods=['POST'])
@login_required
@admin_required
def admin_trilha():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Dê um nome à trilha.', 'warning')
        return redirect(url_for('treino.admin_home'))
    ordem = (db.session.query(db.func.max(TreinoTrilha.ordem)).scalar() or 0) + 1
    db.session.add(TreinoTrilha(
        nome=nome[:150], descricao=request.form.get('descricao') or None,
        ordem=ordem))
    db.session.commit()
    flash('Trilha criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha/<int:id>/video', methods=['POST'])
@login_required
@admin_required
def admin_video_novo(id):
    t = db.session.get(TreinoTrilha, id) or abort(404)
    ordem = _int(db.session.query(db.func.max(TreinoVideo.ordem)).filter_by(
        trilha_id=t.id).scalar()) + 1
    v = TreinoVideo(trilha_id=t.id, titulo=request.form.get('titulo', 'Aula')[:200],
                    duracao_segundos=_int(request.form.get('duracao')), ordem=ordem)
    db.session.add(v)
    db.session.commit()
    flash('Vídeo criado — suba o arquivo agora.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


@treino_bp.route('/admin/video/<int:id>')
@login_required
@admin_required
def admin_video_editar(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treinamento_stream as ts
    # Duração é AUTORITATIVA do Cloudflare — SÓ enquanto está 0 (recém-subido/
    # processando) consultamos o status e gravamos quando o vídeo fica pronto.
    # Depois de detectada, não bate mais no Cloudflare a cada GET (evita HTTP
    # síncrono à toa). O antifraude depende dela (LIMIAR_TEMPO).
    proc = None
    if v.video_externo_id and v.duracao_segundos == 0:
        try:
            proc = ts.status(v.video_externo_id)
            if proc.get('duracao'):
                v.duracao_segundos = proc['duracao']
                db.session.commit()
        except Exception:
            proc = None
    return render_template('treino/admin_video.html', v=v, proc=proc,
                           stream_ok=ts.configurado(),
                           video_embed=ts.embed_url(v.video_externo_id)
                           if v.video_externo_id else None)


@treino_bp.route('/admin/video/<int:id>/upload-url', methods=['POST'])
@login_required
@admin_required
def admin_video_upload_url(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treinamento_stream as ts
    if not ts.configurado():
        return jsonify(ok=False, erro='Cloudflare não configurado'), 503
    try:
        d = ts.criar_upload_direto(f'treino video {v.id}')
    except ValueError as e:
        return jsonify(ok=False, erro=str(e)), 502
    return jsonify(ok=True, uid=d['uid'], uploadURL=d['uploadURL'])


@treino_bp.route('/admin/video/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def admin_video_salvar(id):
    import re as _re
    v = db.session.get(TreinoVideo, id) or abort(404)
    uid = (request.form.get('uid') or '').strip()
    if not _re.fullmatch(r'[0-9a-f]{32}', uid):
        return jsonify(ok=False, erro='UID inválido'), 400
    from app.services import treinamento_stream as ts
    v.video_externo_id = uid
    v.provedor = 'cloudflare'
    db.session.commit()
    if not ts.subdomain():
        ts.status(uid)
    return jsonify(ok=True)


def _mmss_para_seg(valor):
    """Aceita 'min:seg' (ex '2:30' -> 150) OU segundos crus (ex '150'). Campo
    do checkpoint é o MOMENTO do vídeo, e o dono pensa em min:seg."""
    v = (valor or '').strip()
    if ':' in v:
        partes = v.split(':')
        try:
            m, s = int(partes[0] or 0), int(partes[1] or 0)
            return max(0, m * 60 + s)
        except (ValueError, IndexError):
            return 0
    return _int(v)


@treino_bp.route('/admin/video/<int:id>/checkpoint', methods=['POST'])
@login_required
@admin_required
def admin_checkpoint(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    alts = [a.strip() for a in request.form.getlist('alt[]') if a.strip()]
    if len(alts) < 2:
        flash('Checkpoint precisa de ao menos 2 alternativas.', 'warning')
        return redirect(url_for('treino.admin_video_editar', id=v.id))
    db.session.add(TreinoCheckpoint(
        video_id=v.id, segundo=_mmss_para_seg(request.form.get('segundo')),
        enunciado=request.form.get('enunciado', '')[:500], alternativas=alts,
        indice_correto=_int(request.form.get('correta'))))
    db.session.commit()
    flash('Checkpoint adicionado.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


@treino_bp.route('/admin/video/<int:id>/ia-gerar', methods=['POST'])
@login_required
@admin_required
def admin_video_ia(id):
    """v2 §16.2 no CHECKPOINT: a IA PROPÕE pergunta + alternativas. Duas fontes:
    `fonte=video` usa a TRANSCRIÇÃO com tempo do Cloudflare (a IA também sugere
    o MOMENTO); qualquer outra usa o texto colado (sem momento). Não grava nada
    aqui — o admin revisa, ajusta o momento e salva pelo endpoint de checkpoint."""
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treino_ia_perguntas as ia
    n = request.form.get('n', 3)
    if request.form.get('fonte') == 'video':
        if not v.video_externo_id:
            return jsonify(ok=False, erro='Envie o vídeo primeiro.'), 400
        from app.services import treinamento_stream as ts
        segs = ts.transcricao(v.video_externo_id)
        if not segs:
            # legenda ainda não pronta — dispara a geração e pede pra tentar de
            # novo (o Cloudflare leva ~1-2 min pra transcrever). Se a geração
            # falhar (ex.: vídeo ainda não transcodificado, idioma), mostra o
            # motivo real em vez de "processando" pra sempre.
            res = ts.gerar_legenda(v.video_externo_id)
            msg = ('Legenda automática do vídeo ainda sendo gerada pelo '
                   'Cloudflare. Tente de novo em 1-2 minutos.')
            if res and not res.get('ok') and res.get('erro'):
                msg = f'Não consegui gerar a legenda do vídeo: {res["erro"]}'
            return jsonify(ok=False, processando=True, erro=msg), 409
        r = ia.gerar_com_momento(segs, n)
    else:
        r = ia.gerar(request.form.get('texto', ''), n)
    if 'erro' in r:
        return jsonify(ok=False, erro=r['erro']), 400
    return jsonify(ok=True, perguntas=r['perguntas'])


@treino_bp.route('/admin/quiz', methods=['POST'])
@login_required
@admin_required
def admin_quiz_novo():
    trilha_id = _int(request.form.get('trilha_id')) or None
    video_id = _int(request.form.get('video_id')) or None
    if bool(trilha_id) == bool(video_id):
        flash('Escolha trilha OU vídeo pro quiz.', 'warning')
        return redirect(url_for('treino.admin_home'))
    q = TreinoQuiz(titulo=request.form.get('titulo', 'Quiz')[:200],
                   trilha_id=trilha_id, video_id=video_id,
                   questoes_por_tentativa=_int(request.form.get('por_tent'), 5),
                   cooldown_minutos=_int(request.form.get('cooldown'), 120))
    db.session.add(q)
    db.session.commit()
    flash('Quiz criado — adicione as questões.', 'success')
    return redirect(url_for('treino.admin_quiz_editar', id=q.id))


@treino_bp.route('/admin/quiz/<int:id>')
@login_required
@admin_required
def admin_quiz_editar(id):
    q = db.session.get(TreinoQuiz, id) or abort(404)
    return render_template('treino/admin_quiz.html', q=q,
                           pode=tq.pode_publicar(q))


@treino_bp.route('/admin/quiz/<int:id>/questao', methods=['POST'])
@login_required
@admin_required
def admin_questao(id):
    from app.models import TreinoAlternativa, TreinoQuestao
    q = db.session.get(TreinoQuiz, id) or abort(404)
    alts = request.form.getlist('alt[]')
    correta = _int(request.form.get('correta'))
    pares = [(a.strip(), i == correta) for i, a in enumerate(alts) if a.strip()]
    if not request.form.get('enunciado') or len(pares) < 2:
        flash('Questão precisa de enunciado e ao menos 2 alternativas.',
              'warning')
        return redirect(url_for('treino.admin_quiz_editar', id=q.id))
    quest = TreinoQuestao(quiz_id=q.id, enunciado=request.form['enunciado'])
    db.session.add(quest)
    db.session.flush()
    for texto, cor in pares:
        db.session.add(TreinoAlternativa(questao_id=quest.id, texto=texto,
                                         correta=cor))
    db.session.commit()
    flash('Questão adicionada.', 'success')
    return redirect(url_for('treino.admin_quiz_editar', id=q.id))


@treino_bp.route('/admin/quiz/<int:id>/ia-gerar', methods=['POST'])
@login_required
@admin_required
def admin_quiz_ia(id):
    """v2 §16.2: a IA PROPÕE perguntas a partir do conteúdo colado. Devolve as
    propostas pra revisão humana na tela — NÃO grava nada aqui (o admin edita e
    salva as escolhidas pelo endpoint normal de questão)."""
    db.session.get(TreinoQuiz, id) or abort(404)
    from app.services import treino_ia_perguntas as ia
    r = ia.gerar(request.form.get('texto', ''), request.form.get('n', 5))
    if 'erro' in r:
        return jsonify(ok=False, erro=r['erro']), 400
    return jsonify(ok=True, perguntas=r['perguntas'],
                   modelo=r.get('modelo_usado'))


@treino_bp.route('/admin/recompensa', methods=['POST'])
@login_required
@admin_required
def admin_recompensa():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Dê um nome à recompensa.', 'warning')
        return redirect(url_for('treino.admin_home'))
    est = request.form.get('estoque')
    db.session.add(TreinoRecompensa(
        nome=nome[:150], descricao=request.form.get('descricao') or None,
        custo_pontos=_int(request.form.get('custo'), 100),
        estoque=_int(est) if est else None))
    db.session.commit()
    flash('Recompensa criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/ajuste', methods=['POST'])
@login_required
@admin_required
def admin_ajuste():
    f = db.session.get(Funcionario, _int(request.form.get('funcionario_id')))
    temp = _temp()
    just = (request.form.get('justificativa') or '').strip()
    if not (f and temp and just):
        flash('Funcionário, temporada ativa e justificativa são obrigatórios.',
              'warning')
        return redirect(url_for('treino.admin_home'))
    try:
        ledger.ajuste_manual(f, _int(request.form.get('pontos')), just,
                             criado_por_id=current_user.id, temporada=temp)
    except ValueError as e:
        flash(str(e), 'warning')
    else:
        flash('Ajuste lançado.', 'success')
    return redirect(url_for('treino.admin_home'))


# ── Admin: edição / desativação / exclusão ──────────────────────────────
def _voltar():
    return redirect(request.referrer or url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_trilha_toggle(id):
    t = db.session.get(TreinoTrilha, id) or abort(404)
    t.ativa = not t.ativa
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/trilha/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_trilha_excluir(id):
    t = db.session.get(TreinoTrilha, id) or abort(404)
    if t.videos:
        flash('Trilha com vídeos — desative em vez de excluir.', 'warning')
    else:
        db.session.delete(t)
        db.session.commit()
        flash('Trilha excluída.', 'success')
    return _voltar()


@treino_bp.route('/admin/video/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_video_toggle(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    v.ativo = not v.ativo
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/video/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_video_excluir(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    tid = v.trilha_id
    if v.video_externo_id:
        from app.services import treinamento_stream as ts
        ts.deletar(v.video_externo_id)
    db.session.delete(v)
    db.session.commit()
    flash('Vídeo excluído.', 'success')
    return redirect(url_for('treino.admin_home', _anchor=f't{tid}'))


@treino_bp.route('/admin/checkpoint/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_checkpoint_excluir(id):
    c = db.session.get(TreinoCheckpoint, id) or abort(404)
    db.session.delete(c)
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/quiz/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_quiz_toggle(id):
    q = db.session.get(TreinoQuiz, id) or abort(404)
    if not q.ativo and not tq.pode_publicar(q):
        flash('Banco insuficiente (precisa de 3× as questões por tentativa).',
              'warning')
        return _voltar()
    q.ativo = not q.ativo
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/questao/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_questao_excluir(id):
    from app.models import TreinoQuestao
    quest = db.session.get(TreinoQuestao, id) or abort(404)
    db.session.delete(quest)
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/recompensa/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_recompensa_toggle(id):
    r = db.session.get(TreinoRecompensa, id) or abort(404)
    r.ativa = not r.ativa
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/acessos/<int:func_id>/gerar', methods=['POST'])
@login_required
@admin_required
def admin_gerar_acesso(func_id):
    """Cria/vincula o LOGIN do funcionário (onboarding). Sem esse vínculo a
    pessoa não entra no treinamento. Migrado do módulo antigo."""
    from app.services import treino_acessos as acessos
    f = Funcionario.query.get_or_404(func_id)
    r = acessos.gerar_acesso(f)
    if r['motivo'] == 'sem_email':
        flash(f'{f.nome}: cadastre o e-mail no RH antes de gerar o acesso.',
              'warning')
    elif r['motivo'] == 'ja_tem':
        flash(f'{f.nome} já tem acesso.', 'info')
    elif r['motivo'] == 'conta_de_outro_papel':
        flash(f'O e-mail de {f.nome} já é de uma conta de acesso administrativo '
              '— não vinculei (seria a conta errada). Use outro e-mail no RH.',
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
    return _voltar()


@treino_bp.route('/admin/trilha/<int:id>/cargos', methods=['POST'])
@login_required
@admin_required
def admin_trilha_cargos(id):
    """v2 §16.1: liga a trilha a cargos (onboarding automático por cargo)."""
    from app.services import treino_onboarding as ob
    db.session.get(TreinoTrilha, id) or abort(404)
    ob.definir_cargos_da_trilha(id, request.form.getlist('cargo_ids'))
    flash('Cargos da trilha atualizados.', 'success')
    return _voltar()


@treino_bp.route('/gestor/progressao')
@login_required
@gestor_required
def gestor_progressao():
    """v2 §16.3: quem está apto a progredir de cargo (concluiu as trilhas
    exigidas do cargo)."""
    from app.services import treino_onboarding as ob
    gestor = _func()
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    if is_admin:
        equipe = Funcionario.query.filter_by(ativo=True).order_by(
            Funcionario.nome).all()
    else:
        unidade = ledger.unidade_do_funcionario(gestor) if gestor else None
        equipe = [f for f in (unidade.funcionarios if unidade else [])
                  if f.ativo]
    progs = ob.progressao_lote(equipe)   # 2 queries, sem N+1
    linhas = [{'func': f, 'prog': progs[f.id]} for f in equipe]
    return render_template('treino/gestor_progressao.html', linhas=linhas)


@treino_bp.route('/admin/temporada/<int:id>/status', methods=['POST'])
@login_required
@admin_required
def admin_temporada_status(id):
    from app.models import TreinoTemporada
    t = db.session.get(TreinoTemporada, id) or abort(404)
    novo = request.form.get('status')
    if novo in ('PLANEJADA', 'ATIVA', 'ENCERRADA'):
        t.status = novo
        db.session.commit()
    return _voltar()
