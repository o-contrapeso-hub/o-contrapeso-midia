# o-contrapeso-midia

## Para que serve este repositório

Repositório **público**: serve por GitHub Pages as imagens e vídeos dos posts (`p/<post>/...`) e os arquivos de verificação das redes. Não edite `p/` à mão.

## Workflows (GitHub Actions)

Os workflows em `.github/workflows/` **operam o repositório privado `o-contrapeso-hub/o-contrapeso-hub`**: cada execução baixa o privado com o secret `GH_PAT`, roda os scripts de lá (`scripts/`) e grava os commits lá. Estão neste repositório só para usar os minutos gratuitos de repositório público; o código continua privado.

- Rodar na mão: aba **Actions** → workflow → **Run workflow**.
- Secrets e a variável `PUBLICACAO_ATIVA` ficam em **Settings → Secrets and variables → Actions** deste repositório (checklist por workflow no README do repositório privado). Sem um secret necessário, o workflow falha cedo com uma mensagem em português.
- **Os logs são públicos.** Nunca adicione `set -x`, `env`/`printenv`, `upload-artifact` nem gatilhos `pull_request`/`pull_request_target`, e nunca imprima tokens ou URLs com `access_token`.
