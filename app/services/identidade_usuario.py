"""Identificadores aceitos no login e apresentados nas instruções de acesso."""
from app.extensions import db
from app.models import Usuario


def usuario_por_identificador(valor):
    """Resolve login ou e-mail, preservando a prioridade dos logins antigos."""
    exato = Usuario.query.filter_by(login=valor).first()
    if exato:
        return exato

    normalizado = valor.lower()
    por_login = Usuario.query.filter(
        db.func.lower(Usuario.login) == normalizado
    ).all()
    if len(por_login) == 1:
        return por_login[0]

    por_email = Usuario.query.filter(
        db.func.lower(Usuario.email) == normalizado
    ).all()
    return por_email[0] if len(por_email) == 1 else None


def identificador_acesso(usuario):
    """Prefere o e-mail atual quando a conta foi criada com login por e-mail.

    Nicknames continuam visíveis. A mesma resolução do formulário de login
    impede sugerir um e-mail legado duplicado ou que pertença a outra conta.
    Não renomeia a conta nem modifica senha, permissões ou vínculos.
    """
    email = (usuario.email or '').strip().lower()
    if '@' in usuario.login and email:
        destino = usuario_por_identificador(email)
        if destino is not None and destino.id == usuario.id:
            return email
    return usuario.login
