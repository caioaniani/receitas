"""Pré-cadastro de funcionário por QR (23/07/2026, pedido do dono).

O funcionário abre um formulário público (QR) e informa nome, sobrenome,
e-mail e telefone. Isso vira uma `PreCadastroFuncionario`; o admin revisa no
RH e PROMOVE pra `Funcionario` de verdade (informando o CPF que falta).

Validação leve, reusando o validador de celular BR do portal Wi-Fi.
"""
import re
from datetime import timedelta

from app.extensions import db
from app.models import Funcionario, PreCadastroFuncionario
from app.utils import agora, normalizar_telefone

_RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')
# PII: poda pré-cadastros JÁ PROCESSADOS (viraram funcionário) mais antigos que
# isso — o dado vive na ficha do Funcionário, aqui é redundante (LGPD).
_PODAR_PROCESSADOS_DIAS = 180


def _telefone_valido(telefone):
    """Celular BR (reusa a regra do portal Wi-Fi via import tardio pra não
    acoplar no import). Aceita com/sem o 55."""
    try:
        from app.services.wifi_portal import _whatsapp_valido
        return _whatsapp_valido(telefone)
    except Exception:  # noqa: BLE001 — fallback: 10-11 dígitos plausíveis
        d = normalizar_telefone(telefone)
        if d.startswith('55') and len(d) in (12, 13):
            d = d[2:]
        return len(d) in (10, 11)


def validar(nome, sobrenome, email, telefone):
    """Devolve (dados_normalizados, erro). `erro` None = ok."""
    nome = (nome or '').strip()
    sobrenome = (sobrenome or '').strip()
    email = (email or '').strip().lower()
    telefone = (telefone or '').strip()
    if len(nome) < 2:
        return None, 'Informe seu nome.'
    if len(sobrenome) < 2:
        return None, 'Informe seu sobrenome.'
    if not _RE_EMAIL.match(email):
        return None, 'E-mail inválido — confira e tente de novo.'
    if not _telefone_valido(telefone):
        return None, 'Telefone inválido — use um celular com DDD.'
    return {'nome': nome[:100], 'sobrenome': sobrenome[:100],
            'email': email[:150], 'telefone': telefone[:30]}, None


def _podar_processados():
    """Apaga pré-cadastros já processados mais velhos que o prazo (PII/LGPD).
    Best-effort — nunca deixa a poda derrubar o cadastro em si."""
    try:
        limite = agora() - timedelta(days=_PODAR_PROCESSADOS_DIAS)
        (PreCadastroFuncionario.query
         .filter(PreCadastroFuncionario.processado_em.isnot(None),
                 PreCadastroFuncionario.processado_em < limite)
         .delete(synchronize_session=False))
    except Exception:  # noqa: BLE001 — poda oportunista, não bloqueia o insert
        db.session.rollback()


def criar(dados):
    """Cria (ou atualiza um pendente do MESMO e-mail) o pré-cadastro. Evita
    duplicata quando a mesma pessoa reenvia. Retorna a linha."""
    _podar_processados()
    pend = (PreCadastroFuncionario.query
            .filter(db.func.lower(PreCadastroFuncionario.email) == dados['email'],
                    PreCadastroFuncionario.processado_em.is_(None))
            .first())
    if pend:
        pend.nome = dados['nome']
        pend.sobrenome = dados['sobrenome']
        pend.telefone = dados['telefone']
        pend.criado_em = agora()
        db.session.commit()
        return pend
    pre = PreCadastroFuncionario(**dados)
    db.session.add(pre)
    db.session.commit()
    return pre


def pendentes():
    return (PreCadastroFuncionario.query
            .filter(PreCadastroFuncionario.processado_em.is_(None))
            .order_by(PreCadastroFuncionario.criado_em.desc()).all())


def promover(pre, cpf):
    """Cria o `Funcionario` a partir do pré-cadastro (o admin informa o CPF).
    Marca o pré-cadastro como processado e liga ao funcionário. `cadastro_
    pendente=True` — o admin completa cargo/salário no RH depois.

    Retorna (funcionario, erro). CPF vazio ou já usado = erro (nada criado)."""
    cpf = (cpf or '').strip()
    if not cpf:
        return None, 'Informe o CPF pra criar o funcionário.'
    if Funcionario.query.filter_by(cpf=cpf).first():
        return None, f'Já existe funcionário com o CPF {cpf}.'
    func = Funcionario(
        nome=pre.nome_completo, cpf=cpf,
        telefone=pre.telefone, email=pre.email,
        ativo=True, cadastro_pendente=True,
        observacao='Veio do pré-cadastro por QR — completar cargo/salário/CPF.')
    db.session.add(func)
    db.session.flush()
    pre.funcionario_id = func.id
    pre.processado_em = agora()
    db.session.commit()
    return func, None


def descartar(pre):
    """Descarta um pré-cadastro (spam/duplicata) sem criar funcionário."""
    db.session.delete(pre)
    db.session.commit()
