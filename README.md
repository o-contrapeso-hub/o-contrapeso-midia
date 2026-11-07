# O Publicador

O Publicador é uma plataforma open source para automatizar, gerenciar e integrar
a publicação de posts em **Instagram**, **TikTok** e **YouTube**.

## Como funciona

- **Notion é a fonte da verdade.** Todos os dados dos posts (textos, datas,
  estágios e status) vivem em um banco do Notion. O sistema lê e escreve nele.
- **Artes em repositório privado separado.** Os arquivos de imagem e vídeo não
  ficam neste repositório. Eles são mantidos em um repositório privado próprio.
- **Publicação agendada via GitHub Actions.** Um job agendado verifica os posts
  prontos e os publica nas plataformas no horário definido.
- **Regra de segurança:** um post só é publicado quando estiver no estágio
  "confirmado", com data e hora definidas.

## Status

Em construção. Ainda não há versão utilizável.

## Licença

Distribuído sob a licença MIT. Veja o arquivo [LICENSE](LICENSE).

## Segurança e aviso importante

- Este repositório é **público**.
- **Nenhum segredo vai para o repositório.** Tokens, senhas, chaves e
  credenciais de qualquer serviço devem ficar fora do código, em variáveis de
  ambiente ou no cofre de segredos do GitHub Actions.
- Arquivos como `.env` e chaves privadas estão listados no `.gitignore`. Se um
  segredo for commitado por engano, ele deve ser revogado imediatamente, mesmo
  depois de removido.
