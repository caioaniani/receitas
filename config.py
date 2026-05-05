import os

# Banco de dados: PostgreSQL em produção, SQLite local
DB_DIR = os.path.join(os.path.expanduser('~'), '.padaria')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'padaria.db')

DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DB_PATH}')
# Railway usa 'postgres://' mas SQLAlchemy precisa de 'postgresql://'
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)


class Config:
    import secrets
    SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB upload (atestados)
    VNDA_API_TOKEN = os.environ.get('VNDA_API_TOKEN', '')
    VNDA_SHOP_HOST = os.environ.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    # Coordenadas da loja matriz — origem das rotas de entrega
    ROTA_ORIGEM_LAT = os.environ.get('ROTA_ORIGEM_LAT', '')
    ROTA_ORIGEM_LNG = os.environ.get('ROTA_ORIGEM_LNG', '')
