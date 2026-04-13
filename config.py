import os

# Banco de dados salvo na pasta pessoal (~/.padaria/), fora do repositório git
DB_DIR = os.path.join(os.path.expanduser('~'), '.padaria')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'padaria.db')


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'chave-secreta-padaria-2024')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', f'sqlite:///{DB_PATH}')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
