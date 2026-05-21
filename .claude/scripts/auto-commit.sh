#!/bin/bash
# Auto-commit + push after Claude edits a file.
# Hooked as PostToolUse on Write|Edit. Receives the tool JSON on stdin.
# Falhas sao engolidas — o Stop hook global pega qualquer estado nao
# commitado/nao pushado que sobre e devolve pro Claude tratar.

input=$(cat)

file_path=$(printf '%s' "$input" | jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[[ -z "$file_path" ]] && exit 0

repo_root=$(git -C "$(dirname "$file_path")" rev-parse --show-toplevel 2>/dev/null) || exit 0
rel_path=$(realpath --relative-to="$repo_root" "$file_path" 2>/dev/null) || exit 0

cd "$repo_root" || exit 0

# Never auto-commit changes under .git or outside the working tree.
case "$rel_path" in
  ..*|/*|.git/*) exit 0 ;;
esac

git add -- "$rel_path" 2>/dev/null || exit 0

# Nothing actually changed (e.g., Edit was a no-op).
git diff --cached --quiet -- "$rel_path" && exit 0

# Lint pra arquivos Python — evita commit quebrando CI.
# Estrategia em 3 fases:
#  1) ruff --fix em silencio (organiza imports, remove nao-usados)
#  2) ruff check (sem --fix) pra detectar bugs REAIS que sobraram
#     (F821 var nao definida, etc) — imprime no stderr pro hook do
#     Claude Code propagar pro usuario
#  3) Se sobrar erro, ABORTA o commit. Melhor o Claude saber agora
#     (e consertar) do que descobrir 3 min depois no CI do GitHub.
case "$rel_path" in
  *.py)
    if command -v ruff >/dev/null 2>&1; then
      ruff check --fix --quiet "$rel_path" >/dev/null 2>&1
      git add -- "$rel_path" 2>/dev/null || true
      if ! ruff check "$rel_path" >&2; then
        echo "auto-commit: ruff acusou erro nao-fixable em $rel_path" >&2
        echo "auto-commit: commit ABORTADO — corrigir antes de tentar de novo" >&2
        exit 2
      fi
    fi
    ;;
esac

tool_name=$(printf '%s' "$input" | jq -r '.tool_name // "edit"')
case "$tool_name" in
  Write) action="add" ;;
  *) action="update" ;;
esac

git commit -m "auto: ${action} ${rel_path}" >/dev/null 2>&1 || exit 0

branch=$(git symbolic-ref --short HEAD 2>/dev/null) || exit 0
[[ -z "$branch" ]] && exit 0
git remote get-url origin >/dev/null 2>&1 || exit 0

git push -u origin "$branch" >/dev/null 2>&1 || true

exit 0
