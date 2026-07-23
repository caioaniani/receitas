"""Onboarding do treinamento — cria/vincula o LOGIN do funcionário.

Migrado do módulo antigo `treinamento` (24/07/2026) pra cá quando aquele módulo
foi removido: o treinamento gamificado (`treino`) resolve o Funcionario a partir
do Usuario logado (`usuario.funcionario`), então dar acesso = ligar o Funcionario
do RH a uma conta `Usuario` papel 'funcionario'. Sem esse vínculo a pessoa abre
/treino e vê "conta não vinculada a um funcionário".
"""
import secrets

from app.extensions import db
from app.models import Funcionario, Usuario


def contas_sem_vinculo():
    """Contas de login (Usuario) que AINDA não estão ligadas a nenhum
    funcionário — candidatas a vínculo manual. Exclui o dono. Ordenadas por
    nome pra o admin achar pelo nome."""
    vinculados = {r[0] for r in db.session.query(Funcionario.usuario_id).filter(
        Funcionario.usuario_id.isnot(None)).all()}
    q = Usuario.query.filter(Usuario.is_owner.is_(False))
    if vinculados:
        q = q.filter(~Usuario.id.in_(vinculados))
    return q.order_by(Usuario.nome).all()


def vincular_conta(funcionario, usuario):
    """Liga um funcionário a uma conta de login JÁ EXISTENTE (sem criar/gerar
    nada) — pro caso de quem já tem cadastro mas sem e-mail. Idempotente e
    seguro: não rouba conta de outro funcionário nem liga à conta do dono."""
    if funcionario.usuario_id:
        return {'ok': False, 'motivo': 'ja_tem'}
    if usuario is None:
        return {'ok': False, 'motivo': 'sem_usuario'}
    if getattr(usuario, 'is_owner', False):
        return {'ok': False, 'motivo': 'owner'}
    outro = getattr(usuario, 'funcionario', None)
    if outro is not None and outro.id != funcionario.id:
        return {'ok': False, 'motivo': 'conta_em_uso'}
    funcionario.usuario_id = usuario.id
    db.session.commit()
    return {'ok': True, 'motivo': 'vinculado', 'usuario': usuario}


def gerar_acesso(funcionario):
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
    u = Usuario(nome=funcionario.nome, login=email, email=email,
                papel='funcionario')
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
