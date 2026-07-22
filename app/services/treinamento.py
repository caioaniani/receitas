"""Regras do módulo de treinamento (24/07/2026): correção do quiz, progresso do
funcionário, elegibilidade a sorteio/bônus e geração de acesso pelo RH.

Completar um treinamento = ASSISTIR (marcado explicitamente) E passar no quiz
(percentual >= nota mínima). Elegível ao sorteio/bônus = completou TODOS os
treinamentos ativos. Pontuação = acertos acumulados (melhor tentativa por
treinamento). O sorteio/bônus em si é gesto manual do dono — nada de folha
automática.
"""
import secrets

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Treinamento,
    TreinamentoConclusao,
    TreinamentoTentativa,
    Usuario,
)
from app.utils import agora


def treinamentos_ativos():
    return (Treinamento.query
            .filter(Treinamento.apagado_em.is_(None),
                    Treinamento.ativo.is_(True))
            .order_by(Treinamento.ordem, Treinamento.id).all())


def _conclusao(usuario_id, treinamento_id):
    """Pega ou cria a linha de rollup (usuário, treinamento). O INSERT roda num
    SAVEPOINT: numa corrida do mesmo usuário (duplo submit / assistido+quiz
    quase juntos) o unique constraint barra a 2ª linha e a gente refaz a
    leitura, sem 500 e sem perder a transação de fora (a tentativa)."""
    c = TreinamentoConclusao.query.filter_by(
        usuario_id=usuario_id, treinamento_id=treinamento_id).first()
    if c is not None:
        return c
    try:
        with db.session.begin_nested():
            c = TreinamentoConclusao(
                usuario_id=usuario_id, treinamento_id=treinamento_id)
            db.session.add(c)
        return c
    except IntegrityError:
        return TreinamentoConclusao.query.filter_by(
            usuario_id=usuario_id, treinamento_id=treinamento_id).first()


def conclusao_de(usuario_id, treinamento_id):
    """Leitura (sem criar)."""
    return TreinamentoConclusao.query.filter_by(
        usuario_id=usuario_id, treinamento_id=treinamento_id).first()


def marcar_assistido(treinamento, usuario):
    c = _conclusao(usuario.id, treinamento.id)
    if c.assistido_em is None:
        c.assistido_em = agora()
        c.atualizado_em = agora()
        db.session.commit()
    return c


def corrigir_e_registrar(treinamento, usuario, respostas):
    """`respostas` = {pergunta_id: opcao_id}. Corrige, grava a TENTATIVA e
    atualiza a CONCLUSÃO (melhor pontuação + aprovação). Retorna o resumo."""
    perguntas = treinamento.perguntas
    total = len(perguntas)
    acertos = 0
    for p in perguntas:
        correta = next((o.id for o in p.opcoes if o.correta), None)
        escolhida = respostas.get(p.id)
        if correta is not None and escolhida == correta:
            acertos += 1
    percentual = round(100 * acertos / total) if total else 0
    # Compara SEM arredondar (69,5% não vira 70): acertos*100 >= nota*total.
    aprovado = total > 0 and acertos * 100 >= treinamento.nota_minima * total
    pontos = acertos

    db.session.add(TreinamentoTentativa(
        treinamento_id=treinamento.id, usuario_id=usuario.id,
        acertos=acertos, total=total, pontos=pontos, aprovado=aprovado))
    c = _conclusao(usuario.id, treinamento.id)
    if pontos > (c.melhor_pontos or 0):
        c.melhor_pontos = pontos
    if aprovado and c.aprovado_em is None:
        c.aprovado_em = agora()
    c.atualizado_em = agora()
    db.session.commit()
    return {'acertos': acertos, 'total': total, 'percentual': percentual,
            'aprovado': aprovado, 'pontos': pontos,
            'nota_minima': treinamento.nota_minima}


def progresso(usuario):
    """Lista o estado de CADA treinamento ativo pro funcionário."""
    ativos = treinamentos_ativos()
    conclusoes = {c.treinamento_id: c for c in
                  TreinamentoConclusao.query.filter_by(usuario_id=usuario.id).all()}
    out = []
    for t in ativos:
        c = conclusoes.get(t.id)
        assistido = bool(c and c.assistido_em)
        aprovado = bool(c and c.aprovado_em)
        # Treinamento sem quiz (só vídeo) completa ao ASSISTIR — senão um
        # ativo sem perguntas nunca "aprova" e travaria a elegibilidade toda.
        sem_quiz = (t.total_perguntas == 0)
        out.append({
            'treinamento': t,
            'assistido': assistido,
            'aprovado': aprovado,
            'completo': assistido and (aprovado or sem_quiz),
            'melhor_pontos': (c.melhor_pontos if c else 0),
        })
    return out


def elegiveis():
    """Funcionários (usuários) que completaram TODOS os treinamentos ativos —
    ordenados por pontos. Base do sorteio/bônus (gesto manual do dono)."""
    ativos = treinamentos_ativos()
    if not ativos:
        return []
    ids = {t.id for t in ativos}
    sem_quiz = {t.id for t in ativos if t.total_perguntas == 0}
    completos = {}     # usuario_id -> set(treinamento_id concluídos)
    pontos = {}        # usuario_id -> soma dos melhores pontos
    for c in (TreinamentoConclusao.query
              .filter(TreinamentoConclusao.treinamento_id.in_(ids)).all()):
        pontos[c.usuario_id] = pontos.get(c.usuario_id, 0) + (c.melhor_pontos or 0)
        # Completo = assistido E (aprovado OU treinamento sem quiz).
        if c.assistido_em and (c.aprovado_em or c.treinamento_id in sem_quiz):
            completos.setdefault(c.usuario_id, set()).add(c.treinamento_id)
    out = []
    for uid, feitos in completos.items():
        if ids.issubset(feitos):
            u = db.session.get(Usuario, uid)
            if u:
                out.append({'usuario': u, 'pontos': pontos.get(uid, 0),
                            'funcionario': getattr(u, 'funcionario', None)})
    out.sort(key=lambda x: (-x['pontos'], x['usuario'].nome.lower()))
    return out


def gerar_acesso(funcionario):
    """Cria a conta de login (Usuario papel 'funcionario') do RH pelo E-MAIL,
    liga via `funcionario.usuario_id` e manda a senha por e-mail. Idempotente:
    se já tem conta, não recria; se já existe Usuario com aquele e-mail, só
    liga. Retorna dict com o desfecho."""
    if funcionario.usuario_id and funcionario.usuario:
        return {'ok': False, 'motivo': 'ja_tem', 'usuario': funcionario.usuario}
    email = (funcionario.email or '').strip() or None
    if not email:
        return {'ok': False, 'motivo': 'sem_email'}

    # Login = e-mail. Se já existir uma conta com esse login, só vincula se for
    # SEGURO: precisa ser papel 'funcionario' e não estar ligada a OUTRO
    # funcionário — senão o e-mail coincidir com um admin/owner (ou com outra
    # pessoa) faria o progresso/elegibilidade operar sobre a conta errada
    # (achado da revisão 24/07). Nesses casos recusa com aviso.
    existente = Usuario.query.filter(
        db.func.lower(Usuario.login) == email.lower()).first()
    if existente:
        if existente.papel != 'funcionario' or getattr(existente, 'is_owner', False):
            return {'ok': False, 'motivo': 'conta_de_outro_papel',
                    'usuario': existente}
        outro = getattr(existente, 'funcionario', None)
        if outro is not None and outro.id != funcionario.id:
            return {'ok': False, 'motivo': 'email_em_uso'}
        funcionario.usuario_id = existente.id
        db.session.commit()
        return {'ok': True, 'motivo': 'vinculado', 'usuario': existente}

    senha = secrets.token_urlsafe(8)[:10]
    u = Usuario(nome=funcionario.nome, login=email, email=email,
                papel='funcionario')
    u.set_senha(senha)
    db.session.add(u)
    db.session.flush()
    funcionario.usuario_id = u.id
    db.session.commit()

    from app.services import email as email_svc
    res = email_svc.enviar_boas_vindas(email, funcionario.nome, email, senha)
    return {'ok': True, 'motivo': 'criado', 'usuario': u, 'senha': senha,
            'email_ok': res.get('ok'), 'email_erro': res.get('erro')}
