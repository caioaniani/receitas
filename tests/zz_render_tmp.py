import re


def test_dump(app, admin_user, loja):
    from app.extensions import db
    from app.models import EstoqueLoja, Receita

    def rec(n):
        r = Receita(nome=n, categoria='Fornadas Especiais', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r); db.session.flush(); return r

    with app.app_context():
        cal = rec('Danish de Calabresa')
        alho = rec('Danish de alho poró')
        muc = rec('Danish de Muçarela de Búfala')
        maca = rec('Danish de Maçã')
        for q in (1, 3, 1):
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=cal.id, estado=None, quantidade=q))
        for q in (1, 1, 2):
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=alho.id, estado=None, quantidade=q))
        for q in (6, 1):
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=muc.id, estado=None, quantidade=q))
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=maca.id, estado=None, quantidade=1))
        rec('Danish de Calabresa')  # cadastro homonimo
        db.session.commit()

    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id); s['_fresh'] = True
    html = c.get('/pedidos/estoque-loja/saude').get_data(as_text=True)
    i = html.find('Itens duplicados no estoque')
    chunk = html[i - 40:i + 2600]
    txt = re.sub(r'<[^>]*>', ' ', chunk)
    txt = re.sub(r'[ \t]+', ' ', txt)
    txt = re.sub(r'\n\s*\n+', '\n', txt)
    print('\n=====RENDER=====')
    for line in txt.splitlines():
        if line.strip():
            print(line.strip())
    print('=====END=====')
