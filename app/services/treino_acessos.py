"""Onboarding do treinamento — cria/vincula o LOGIN do funcionário.

Migrado do módulo antigo `treinamento` (24/07/2026) pra cá quando aquele módulo
foi removido: o treinamento gamificado (`treino`) resolve o Funcionario a partir
do Usuario logado (`usuario.funcionario`), então dar acesso = ligar o Funcionario
do RH a uma conta `Usuario` papel 'funcionario'. Sem esse vínculo a pessoa abre
/treino e vê "conta não vinculada a um funcionário".
"""
import re
import secrets

from app.extensions import db
from app.models import Funcionario, Usuario

# Uma conta administrativa também pode pertencer a um funcionário do RH
# (casos reais: líderes que já usavam o sistema antes do módulo de treino).
# O dono continua protegido separadamente por `is_owner` em todos os fluxos.
PAPEIS_VINCULAVEIS = {
    'admin', 'funcionario', 'gerente', 'producao', 'padeiro', 'rh',
}
_RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')


def _email_normalizado(email):
    email = (email or '').strip().lower()
    return email if _RE_EMAIL.match(email) else None


def contas_sem_vinculo():
    """Contas de login (Usuario) que AINDA não estão ligadas a nenhum
    funcionário — candidatas a vínculo manual. Inclui administradores comuns,
    mas exclui o dono. Ordenadas por nome pra o admin achar pelo nome."""
    vinculados = {r[0] for r in db.session.query(Funcionario.usuario_id).filter(
        Funcionario.usuario_id.isnot(None)).all()}
    q = Usuario.query.filter(
        Usuario.is_owner.is_(False),
        Usuario.papel.in_(sorted(PAPEIS_VINCULAVEIS)),
    )
    if vinculados:
        q = q.filter(~Usuario.id.in_(vinculados))
    return q.order_by(Usuario.nome).all()


def sincronizar_email(funcionario, email, usuario=None):
    """Salva o e-mail na ficha e, quando há conta, também nela.

    O login antigo nunca muda. Recusa e-mail de outro funcionário ou usado
    como login por outra conta para não criar uma identidade ambígua.
    """
    email = _email_normalizado(email)
    if not email:
        return {'ok': False, 'motivo': 'email_invalido'}
    outro_func = Funcionario.query.filter(
        Funcionario.id != funcionario.id,
        db.func.lower(Funcionario.email) == email,
    ).first()
    if outro_func:
        return {'ok': False, 'motivo': 'email_de_outro_funcionario',
                'funcionario': outro_func}
    outra_conta = Usuario.query.filter(
        db.or_(db.func.lower(Usuario.login) == email,
               db.func.lower(Usuario.email) == email),
        Usuario.id != (usuario.id if usuario else 0),
    ).first()
    if outra_conta:
        return {'ok': False, 'motivo': 'email_de_outra_conta',
                'usuario': outra_conta}
    funcionario.email = email
    if usuario is not None:
        usuario.email = email
    db.session.commit()
    return {'ok': True, 'motivo': 'email_salvo', 'email': email}


def vincular_conta(funcionario, usuario, email=None):
    """Liga um funcionário a uma conta de login JÁ EXISTENTE (sem criar/gerar
    nada) — pro caso de quem já tem cadastro mas sem e-mail. Idempotente e
    seguro: não rouba conta de outro funcionário nem liga à conta do dono."""
    if funcionario.usuario_id:
        return {'ok': False, 'motivo': 'ja_tem'}
    if usuario is None:
        return {'ok': False, 'motivo': 'sem_usuario'}
    if getattr(usuario, 'is_owner', False):
        return {'ok': False, 'motivo': 'owner'}
    if usuario.papel not in PAPEIS_VINCULAVEIS:
        return {'ok': False, 'motivo': 'papel_invalido'}
    outro = getattr(usuario, 'funcionario', None)
    if outro is not None and outro.id != funcionario.id:
        return {'ok': False, 'motivo': 'conta_em_uso'}
    if email is not None:
        email = _email_normalizado(email)
        if not email:
            return {'ok': False, 'motivo': 'email_invalido'}
        outro_func = Funcionario.query.filter(
            Funcionario.id != funcionario.id,
            db.func.lower(Funcionario.email) == email,
        ).first()
        if outro_func:
            return {'ok': False, 'motivo': 'email_de_outro_funcionario',
                    'funcionario': outro_func}
        outra_conta = Usuario.query.filter(
            db.or_(db.func.lower(Usuario.login) == email,
                   db.func.lower(Usuario.email) == email),
            Usuario.id != usuario.id,
        ).first()
        if outra_conta:
            return {'ok': False, 'motivo': 'email_de_outra_conta',
                    'usuario': outra_conta}
        funcionario.email = email
        usuario.email = email
    funcionario.usuario_id = usuario.id
    db.session.commit()
    return {'ok': True, 'motivo': 'vinculado', 'usuario': usuario}


def gerar_acesso(funcionario, *, somente_treino=False):
    """Cria a conta de login (Usuario papel 'funcionario') do RH pelo E-MAIL,
    liga via `funcionario.usuario_id` e manda a senha por e-mail. Idempotente:
    se já tem conta, não recria; se já existe Usuario com aquele e-mail, só
    liga. Retorna dict com o desfecho (motivo in ja_tem/sem_email/
    conta_de_outro_papel/email_em_uso/vinculado/criado)."""
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
    # Senha nasce provisória: força a troca no 1º login (o e-mail manda a atual).
    u = Usuario(nome=funcionario.nome, login=email, email=email,
                papel='funcionario', senha_provisoria=True,
                somente_treino=bool(somente_treino))
    u.set_senha(senha)
    db.session.add(u)
    db.session.flush()
    funcionario.usuario_id = u.id
    db.session.commit()

    from app.services import email as email_svc
    # Conta de TREINAMENTO: sem o bloco do Chatwoot (nem todo funcionário
    # atende cliente — decisão do dono 23/07/2026).
    res = email_svc.enviar_boas_vindas(email, funcionario.nome, email, senha,
                                       com_chatwoot=False)
    return {'ok': True, 'motivo': 'criado', 'usuario': u, 'senha': senha,
            'email_ok': res.get('ok'), 'email_erro': res.get('erro')}


def reenviar_acesso(funcionario):
    """Troca a senha da conta vinculada e envia um novo acesso por e-mail.

    A senha atual só existe como hash e, portanto, não pode ser recuperada.
    Para não bloquear a pessoa quando o Postmark recusa o envio, a nova senha
    só é confirmada no banco depois que o provedor aceita a mensagem.
    """
    usuario = getattr(funcionario, 'usuario', None)
    if usuario is None or not funcionario.usuario_id:
        return {'ok': False, 'motivo': 'sem_conta'}
    if getattr(usuario, 'is_owner', False):
        return {'ok': False, 'motivo': 'owner'}

    email = (_email_normalizado(funcionario.email)
             or _email_normalizado(usuario.email))
    if not email:
        return {'ok': False, 'motivo': 'sem_email'}

    senha = secrets.token_urlsafe(8)[:10]
    usuario.set_senha(senha)
    usuario.senha_provisoria = True
    usuario.email = email
    try:
        db.session.flush()
    except Exception as exc:  # pragma: no cover - falha do banco
        db.session.rollback()
        return {'ok': False, 'motivo': 'erro_banco', 'erro': str(exc)}

    from app.services import email as email_svc
    res = email_svc.enviar_boas_vindas(
        email, funcionario.nome, usuario.login, senha, com_chatwoot=False)
    if not res.get('ok'):
        db.session.rollback()
        return {'ok': False, 'motivo': 'email_falhou',
                'email_erro': res.get('erro')}

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - falha rara depois do envio
        db.session.rollback()
        return {'ok': False, 'motivo': 'confirmacao_falhou',
                'email_id': res.get('id'), 'erro': str(exc)}
    return {'ok': True, 'motivo': 'reenviado', 'usuario': usuario,
            'email': email, 'email_id': res.get('id')}


def sugerir_contas(funcionarios, contas=None):
    """Sugere, sem vincular, uma conta livre por e-mail ou nome.

    E-mail exato tem prioridade. Por nome, exige correspondência forte e sem
    empate. A confirmação continua sendo humana na tela do RH.
    """
    from app.utils import normalizar_busca

    contas = list(contas if contas is not None else contas_sem_vinculo())
    sugestoes = {}
    por_email = {}
    for usuario in contas:
        for candidato in (usuario.email, usuario.login):
            email = _email_normalizado(candidato)
            if email:
                por_email.setdefault(email, []).append(usuario)
    for funcionario in funcionarios:
        if funcionario.usuario_id:
            continue
        email = _email_normalizado(funcionario.email)
        candidatas = por_email.get(email, []) if email else []
        if len(candidatas) == 1:
            sugestoes[funcionario.id] = {
                'usuario': candidatas[0], 'motivo': 'mesmo e-mail'}
            continue
        alvo = set(normalizar_busca(funcionario.nome or '').split())
        melhor, melhor_score, empate = None, 0.0, False
        for usuario in contas:
            tokens = set(normalizar_busca(usuario.nome or '').split())
            if not alvo or not tokens:
                continue
            score = len(alvo & tokens) / len(alvo)
            if score > melhor_score:
                melhor, melhor_score, empate = usuario, score, False
            elif score == melhor_score and melhor is not None:
                empate = True
        if melhor is not None and melhor_score >= 0.75 and not empate:
            sugestoes[funcionario.id] = {
                'usuario': melhor, 'motivo': 'nome semelhante'}
    return sugestoes
