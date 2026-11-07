# CI do O Publicador

O workflow fica em `.github/workflows/ci.yml` e roda em `push` e `pull_request` para a `main`, com `permissions: contents: read` e um único job em `ubuntu-24.04` com Python 3.12.

## Etapas

1. **Baixar o código** (`fetch-depth: 0`): histórico completo, para o gitleaks ver todos os commits.
2. **Instalar Python 3.12**: com cache do pip.
3. **Instalar o projeto**: `pip install -e ".[dev]"`.
4. **Lint** (`ruff check .`): erros de estilo e bugs comuns.
5. **Formatação** (`ruff format --check .`): falha se algum arquivo não estiver formatado.
6. **Tipos** (`mypy src`): modo estrito.
7. **Testes** (`pytest`).
8. **Vulnerabilidades** (`pip-audit --skip-editable`): dependências com falhas conhecidas.
9. **Licenças** (`pip-licenses`): permitidas MIT, BSD, Apache, PSF, ISC e MPL. Falha com GPL, LGPL, AGPL, GNU, licença UNKNOWN ou fora da lista.
10. **Segredos** (gitleaks 8.18.4): baixa o binário, confere o SHA-256 contra o arquivo de checksums da mesma release e roda `gitleaks detect --config .gitleaks.toml --redact` sobre todo o histórico. Usa as regras padrão, sem allowlist.

Nenhuma etapa imprime variáveis de ambiente, usa `set -x` ou envia artefatos. Nenhum segredo é necessário.

## Como provar que o gitleaks barra um segredo

Faça isto só em uma branch descartável. Nunca commite isto na `main`.

1. Crie a branch: `git switch -c teste/gitleaks-segredo-falso`.
2. Crie um arquivo fictício na raiz, por exemplo `segredo_falso.txt`, com:
   ```
   aws_access_key_id = AKIAIOSFODNN7EXAMPLE
   aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
   ```
   São os exemplos oficiais da AWS, sem nenhuma conta real por trás.
3. Faça commit e `push` da branch. Abra um pull request para a `main`.
4. Confirme que a etapa **Varredura de segredos (gitleaks)** falha, e que a saída mostra a regra AWS com o valor redigido.
5. Feche o pull request sem fazer merge e apague a branch remota e local.

Se o gitleaks não acusar o exemplo, a regra pode estar ignorando valores com a palavra `EXAMPLE`. Nesse caso, troque por outro valor fictício no formato AWS e registre a mudança aqui.
