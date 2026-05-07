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
    # Endereco textual da matriz — usado como origem dos links do Google Maps
    ROTA_ORIGEM_ENDERECO = os.environ.get('ROTA_ORIGEM_ENDERECO', '')
    # Chave da API do Google Maps Platform (Geocoding + Directions)
    GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

    # Seru (PDV) — credenciais OAuth2 client_credentials
    # Documentacao: https://integration.plataformaseru.com.br/v1/docs
    SERU_CLIENT_ID = os.environ.get('SERU_CLIENT_ID', '')
    SERU_CLIENT_SECRET = os.environ.get('SERU_CLIENT_SECRET', '')

    # Token para integracao com bots externos (n8n / WhatsApp).
    # Gere com: python -c "import secrets; print(secrets.token_urlsafe(32))"
    BOT_API_TOKEN = os.environ.get('BOT_API_TOKEN', '')

    # Dropbox para fotos de comprovante de entrega
    # Token: https://www.dropbox.com/developers/apps -> escopo files.content.write + sharing.write
    DROPBOX_ACCESS_TOKEN = os.environ.get('DROPBOX_ACCESS_TOKEN', '')
    DROPBOX_PASTA_BASE = os.environ.get('DROPBOX_PASTA_BASE', '/Apps/Receitas-Entregas')
    # Refresh token flow (recomendado: token nao expira)
    # Setup: /entregas/dropbox/setup (apenas admin)
    DROPBOX_APP_KEY = os.environ.get('DROPBOX_APP_KEY', '')
    DROPBOX_APP_SECRET = os.environ.get('DROPBOX_APP_SECRET', '')
    DROPBOX_REFRESH_TOKEN = os.environ.get('DROPBOX_REFRESH_TOKEN', '')
