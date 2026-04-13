import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'chave-secreta-padaria-2024')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///padaria.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
