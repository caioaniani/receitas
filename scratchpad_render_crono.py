"""Renderiza /telaindustriateste com um cenário enviado+difere e salva o HTML
com Bootstrap LOCAL (a CDN é bloqueada no sandbox). Só pra validação visual —
não faz parte da suíte."""
import os

os.environ['PYTEST_RUNNING'] = '1'
os.environ.setdefault('SECRET_KEY', 'dev')

from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import (
    Loja,
    PedidoItem,
    PedidoLoja,
    Receita,
    Usuario,
)
from app.services.cronograma_edit import alternar_dia_fechado, editar_celula
from app.services.producao import enviar_plano_do_dia
from app.utils import hoje

app = create_app()
with app.app_context():
    db.create_all()
    # usuário admin
    u = Usuario.query.filter_by(login='vis').first()
    if not u:
        u = Usuario(nome='vis', login='vis', papel='admin')
        u.set_senha('123')
        db.session.add(u)
        db.session.commit()
    # várias receitas com pedido → grid cheio
    cats = ['ACOMPANHAMENTOS', 'CREMES', 'PAES', 'CROISSANTS', 'DOCES']
    loja = Loja.query.filter_by(nome='Loja Vis').first()
    if not loja:
        loja = Loja(nome='Loja Vis', ativa=True)
        db.session.add(loja)
        db.session.flush()
    d_amanha = hoje() + timedelta(days=1)
    recs = []
    for i in range(10):
        nome = 'Receita Vis %d' % i
        r = Receita.query.filter_by(nome=nome).first()
        if not r:
            r = Receita(nome=nome, categoria=cats[i % len(cats)],
                        rendimento_qtd=1, rendimento_unidade='un',
                        peso_base=1000.0)
            db.session.add(r)
            db.session.flush()
            p = PedidoLoja(loja_id=loja.id, status='pendente',
                           data_entrega=d_amanha, data_pedido=hoje())
            db.session.add(p)
            db.session.flush()
            db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                      quantidade=20 + i * 5))
        recs.append(r)
    db.session.commit()
    # envia hoje e amanhã, depois edita o grid pra gerar "difere"
    for d in (hoje(), d_amanha):
        enviar_plano_do_dia(d, u.id, horizonte_dias=7)
    editar_celula(recs[0].id, hoje().isoformat(), 999, horizonte_dias=7)
    editar_celula(recs[1].id, d_amanha.isoformat(), 888, horizonte_dias=7)
    # fecha um dia (mostra o 🔒)
    alternar_dia_fechado(hoje(), u.id)

    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    html = c.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)

# aponta pro Bootstrap local
base = '/tmp/claude-0/-home-user-receitas/d7bf97ce-f0c4-5682-9c27-fff6cfa2a810/scratchpad/package/dist'
html = html.replace(
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css',
    'file://%s/css/bootstrap.min.css' % base)
html = html.replace(
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css',
    '')
html = html.replace(
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
    'file://%s/js/bootstrap.bundle.min.js' % base)

out = '/tmp/claude-0/-home-user-receitas/d7bf97ce-f0c4-5682-9c27-fff6cfa2a810/scratchpad/crono.html'
with open(out, 'w') as f:
    f.write(html)
print('HTML salvo:', out)
