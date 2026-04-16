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
