from flask import Flask

from app.extensions import db, csrf
from config import Config


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    db.init_app(app)
    csrf.init_app(app)

    # Filtros Jinja2
    @app.template_filter('brl')
    def brl_filter(value):
        """Formata valor como moeda brasileira: R$ 1.234,56"""
        if value is None:
            return 'R$ 0,00'
        formatted = f'{value:,.2f}'
        # Troca separadores para padrão brasileiro
        formatted = formatted.replace(',', 'X').replace('.', ',').replace('X', '.')
        return f'R$ {formatted}'

    @app.template_filter('pct')
    def pct_filter(value):
        """Formata valor como porcentagem: 12,5%"""
        if value is None:
            return 'N/A'
        formatted = f'{value:.1f}'.replace('.', ',')
        return f'{formatted}%'

    # Registrar blueprints
    from app.blueprints.main import main_bp
    from app.blueprints.materias_primas import materias_primas_bp
    from app.blueprints.receitas import receitas_bp
    from app.blueprints.relatorios import relatorios_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(materias_primas_bp, url_prefix='/materias-primas')
    app.register_blueprint(receitas_bp, url_prefix='/receitas')
    app.register_blueprint(relatorios_bp, url_prefix='/relatorios')

    with app.app_context():
        db.create_all()
        from app.seed import seed_database
        seed_database()

    return app
