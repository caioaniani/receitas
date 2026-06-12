"""Sincronização servidor da loja ↔ nuvem (Railway).

O servidor local roda o MESMO código com SYNC_NUVEM_URL/SYNC_API_TOKEN/
SYNC_LOJA_ID definidos. Um loop em background:

- desce o catálogo da nuvem (lojas, receitas, produtos, preços por loja,
  setores de comanda) — a nuvem é a dona dos cadastros e os IDs locais
  espelham os da nuvem;
- sobe as vendas finalizadas (paga/cancelada) que ainda não subiram.
  A chave global é Venda.uuid: reenviar é inofensivo (a nuvem ignora
  o que já tem), então queda de internet no meio do envio não duplica.

Endpoints do lado da nuvem: app/blueprints/pdv/sync_api.py.
Setup do servidor da loja: docs/servidor-local.md.
"""
import logging
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

TIMEOUT = (10, 60)
CATALOGO_A_CADA = 10  # ciclos (com intervalo 60s = a cada ~10 min)

# Estado pro painel do caixa (por processo)
_estado = {
    'ultimo_ok': None,
    'ultimo_catalogo': None,
    'ultimo_erro': None,
}
_loop_iniciado = False


def _cfg(app):
    url = (app.config.get('SYNC_NUVEM_URL') or '').strip().rstrip('/')
    token = (app.config.get('SYNC_API_TOKEN') or '').strip()
    try:
        loja_id = int(app.config.get('SYNC_LOJA_ID') or 0)
    except (TypeError, ValueError):
        loja_id = 0
    return url, token, loja_id


def modo_loja(app):
    return bool((app.config.get('SYNC_NUVEM_URL') or '').strip())


def loja_id_local(app):
    """Loja deste servidor (SYNC_LOJA_ID) ou None."""
    return _cfg(app)[2] or None


def _headers(token):
    return {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}


def _parse_dt(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


# ── Catálogo: nuvem → loja ──

def puxar_catalogo(app):
    """Espelha o catálogo da nuvem no banco local (upsert por id da nuvem).

    Receitas vêm só com os campos necessários pra vender (a ficha técnica
    completa fica na nuvem). PrecoLojaReceita é substituída por inteiro.
    """
    from app.extensions import db
    from app.models import Loja, Receita, Produto, PrecoLojaReceita

    url, token, _ = _cfg(app)
    r = requests.get(f'{url}/pdv/api/sync/catalogo',
                     headers=_headers(token), timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'catálogo HTTP {r.status_code}: {r.text[:200]}')
    cat = r.json()

    for lj in cat.get('lojas') or []:
        loja = db.session.get(Loja, lj['id']) or Loja(id=lj['id'], nome=lj['nome'])
        loja.nome = lj['nome']
        loja.endereco = lj.get('endereco')
        loja.telefone = lj.get('telefone')
        loja.ativa = bool(lj.get('ativa', True))
        db.session.add(loja)

    for rc in cat.get('receitas') or []:
        rec = db.session.get(Receita, rc['id']) or Receita(id=rc['id'])
        rec.nome = rc['nome']
        rec.categoria = rc.get('categoria')
        rec.setor = rc.get('setor')
        rec.preco_venda = rc.get('preco_venda')
        rec.preco_loja = rc.get('preco_loja')
        rec.preco_site = rc.get('preco_site')
        rec.rendimento_qtd = rc.get('rendimento_qtd') or 1
        rec.rendimento_unidade = rc.get('rendimento_unidade') or 'un'
        rec.peso_base = rc.get('peso_base') or 1
        db.session.add(rec)

    for pd in cat.get('produtos') or []:
        prod = db.session.get(Produto, pd['id']) or Produto(id=pd['id'])
        prod.nome = pd['nome']
        prod.categoria = pd.get('categoria')
        prod.setor = pd.get('setor')
        prod.descricao = pd.get('descricao')
        prod.preco_atacado = pd.get('preco_atacado')
        prod.preco_loja = pd.get('preco_loja')
        prod.preco_site = pd.get('preco_site')
        prod.ativo = bool(pd.get('ativo', True))
        db.session.add(prod)

    PrecoLojaReceita.query.delete()
    for pl in cat.get('precos_loja') or []:
        db.session.add(PrecoLojaReceita(loja_id=pl['loja_id'],
                                        receita_id=pl['receita_id'],
                                        preco=pl['preco']))
    db.session.commit()
    _estado['ultimo_catalogo'] = datetime.utcnow().isoformat()
    return {'lojas': len(cat.get('lojas') or []),
            'receitas': len(cat.get('receitas') or []),
            'produtos': len(cat.get('produtos') or [])}


# ── Vendas: loja → nuvem ──

def _venda_doc(v):
    return {
        'uuid': v.uuid,
        'code': v.code,
        'loja_id': v.loja_id,
        'operador': v.operador or (v.usuario.nome if v.usuario else None),
        'status': v.status,
        'subtotal': v.subtotal,
        'desconto': v.desconto,
        'total': v.total,
        'observacao': v.observacao,
        'criado_em': v.criado_em.isoformat() if v.criado_em else None,
        'finalizado_em': v.finalizado_em.isoformat() if v.finalizado_em else None,
        'itens': [{
            'receita_id': i.receita_id,
            'produto_id': i.produto_id,
            'descricao': i.descricao,
            'setor': i.setor,
            'quantidade': i.quantidade,
            'preco_unitario': i.preco_unitario,
            'subtotal': i.subtotal,
        } for i in v.itens],
        'pagamentos': [{
            'metodo': p.metodo,
            'valor': p.valor,
            'valor_recebido': p.valor_recebido,
            'troco': p.troco,
            'status': p.status,
            'capturado_via': p.capturado_via,
            'clover_external_id': p.clover_external_id,
            'clover_payment_id': p.clover_payment_id,
            'erro': p.erro,
            'criado_em': p.criado_em.isoformat() if p.criado_em else None,
        } for p in v.pagamentos],
    }


def vendas_pendentes_query():
    from app.models import Venda
    return Venda.query.filter(Venda.sincronizada_em.is_(None),
                              Venda.status != 'aberta')


def enviar_vendas(app, limite=100):
    """Sobe um lote de vendas finalizadas. Retorna quantas subiram."""
    from app.extensions import db

    url, token, _ = _cfg(app)
    vendas = vendas_pendentes_query().order_by('id').limit(limite).all()
    if not vendas:
        return 0
    docs = [_venda_doc(v) for v in vendas]
    r = requests.post(f'{url}/pdv/api/sync/vendas',
                      headers=_headers(token),
                      json={'vendas': docs}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'vendas HTTP {r.status_code}: {r.text[:200]}')
    body = r.json()
    aceitas = set(body.get('aceitas') or [])
    agora = datetime.utcnow()
    n = 0
    for v in vendas:
        if v.uuid in aceitas:
            v.sincronizada_em = agora
            n += 1
    db.session.commit()
    for e in body.get('erros') or []:
        logger.warning('sync: nuvem recusou venda %s: %s', e.get('uuid'), e.get('erro'))
    return n


# ── Loop de background (servidor da loja) ──

def ciclo(app, com_catalogo=True):
    """Um ciclo de sincronização. Retorna dict com o que aconteceu."""
    resultado = {}
    with app.app_context():
        if com_catalogo:
            resultado['catalogo'] = puxar_catalogo(app)
        resultado['vendas_enviadas'] = enviar_vendas(app)
        resultado['pendentes'] = vendas_pendentes_query().count()
    _estado['ultimo_ok'] = datetime.utcnow().isoformat()
    _estado['ultimo_erro'] = None
    return resultado


def status(app):
    with app.app_context():
        try:
            pendentes = vendas_pendentes_query().count()
        except Exception:
            pendentes = None
    return {
        'modo_loja': modo_loja(app),
        'pendentes': pendentes,
        'ultimo_ok': _estado['ultimo_ok'],
        'ultimo_catalogo': _estado['ultimo_catalogo'],
        'ultimo_erro': _estado['ultimo_erro'],
    }


def iniciar_loop(app):
    """Liga o loop (1x por processo). Chamado no create_app em modo loja."""
    global _loop_iniciado
    if _loop_iniciado or not modo_loja(app):
        return
    _loop_iniciado = True
    try:
        intervalo = max(int(app.config.get('SYNC_INTERVALO') or 60), 10)
    except (TypeError, ValueError):
        intervalo = 60

    def _run():
        time.sleep(5)  # deixa o servidor terminar de subir
        n = 0
        while True:
            try:
                ciclo(app, com_catalogo=(n % CATALOGO_A_CADA == 0))
            except Exception as e:
                # Internet fora é esperado — guarda o erro e tenta de novo depois
                _estado['ultimo_erro'] = f'{type(e).__name__}: {str(e)[:200]}'
                logger.warning('sync: ciclo falhou: %s', _estado['ultimo_erro'])
            n += 1
            time.sleep(intervalo)

    threading.Thread(target=_run, daemon=True, name='sync-loja').start()
    logger.info('sync: loop iniciado (intervalo %ss)', intervalo)
