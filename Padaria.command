#!/bin/bash
# ═══════════════════════════════════════
#   Padaria — Iniciar Sistema
# ═══════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║   Iniciando sistema da Padaria... ║"
echo "  ╚═══════════════════════════════════╝"
echo ""

# Instalar dependências se necessário
pip3 install -r requirements.txt -q 2>/dev/null

# Abrir navegador depois de 2 segundos
(sleep 2 && open http://localhost:2000) &

echo "  Servidor rodando em: http://localhost:2000"
echo ""
echo "  Para parar, feche esta janela ou pressione Ctrl+C"
echo ""

# Iniciar servidor
python3 run.py
