# Escutaí (ex-OmniScribe AI) — Project Guidelines

- **Git & Merge Policy**: Never commit changes directly to `main` and never merge PRs. Create a semantic branch (`feature/`, `fix/`, `docs/`, `refactor/`), open a PR with `gh pr create` (using `--body-file -` with a single-quoted PowerShell here-string for multi-line bodies), and share the PR link for user approval.
- **Project Handoff**: Keep `HANDOFF.md` updated whenever modules, build scripts, or architecture flows change.
- **Pós-Merge (Atualização do Instalador Windows)**: Sempre que um merge for aprovado e sincronizado na `main`, verifique se o instalador (`C:\Users\willian.faino\Desktop\zoom-transcriber\Escutai_Setup.exe`) ainda não foi recompilado após a alteração e pergunte ao usuário se deseja que ele seja atualizado agora (lembrando de redirecionar `< /dev/null` e definir `timeout` ao invocar `cmd.exe`/PyInstaller via WSL interop).
- **Git Author (Repo Local)**: `Willian Faino <willianfaino@hotmail.com>`.
