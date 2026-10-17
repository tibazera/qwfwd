# Pesquisa de reutilização — extensão UDP

Data: 2026-09-30. Pesquisa e inspeção estática; projetos externos não foram instalados nem executados.

## Candidatos

| Projeto | Evidência examinada | Decisão |
|---|---|---|
| [GoogleChrome/chrome-extensions-samples](https://github.com/GoogleChrome/chrome-extensions-samples/tree/main/api-samples/nativeMessaging) | Manifest V3, connectNative, eventos de mensagem/desconexão, scripts de registro Windows por usuário. Host de exemplo em Python. | Base de referência para extensão e registro; adaptar ao helper .NET existente. |
| [NetAsmCom/Socketify](https://github.com/NetAsmCom/Socketify) | Página → extensão → host Go → UDP/TCP. Manifest V2, background persistente, permissões para todas as páginas; último push informado pela API em 2018-10-28. MIT. | Mais próximo funcionalmente, mas não copiar integralmente. Referência de arquitetura. |
| [alexwiese/Lyre](https://github.com/alexwiese/Lyre) | Host C#, testes, limite de mensagem, Newtonsoft.Json. MIT; último push informado em 2025-08-07. | Referência de framing e testes, não adotar sem corrigir leitura parcial do cabeçalho. |
| [ba32107/dotnet-chrome-native-messaging](https://github.com/ba32107/dotnet-chrome-native-messaging) | Bibliotecas .NET de host e instalador; MIT; último push informado em 2023-05-31. README/metadados examinados, implementação ainda não auditada. | Alternativa, sem vantagem demonstrada para adicionar dependência. |
| [simov/native-messaging](https://github.com/simov/native-messaging) | Host Node.js, exemplos Chrome/Firefox; MIT; último push informado em 2023-06-03. | Referência, exigiria outro runtime ou empacotamento. |
| [guest271314/sockets](https://github.com/guest271314/sockets) | README combina extensão, Native Messaging, Node e Isolated Web App; instalação exemplificada em Linux. | Complexidade desnecessária para medição QW no Windows. |

Datas de push são do repositório inteiro, não prova de atualização do componente nem de manutenção ativa.

## Por que não copiar indiscriminadamente

- Socketify expõe sockets genéricos a páginas; precisamos somente de medição QW com destinos/volume limitados.
- Em `Lyre/NativeMessagingHost.cs`, `ReadInt32` repete `ReadAsync` com offset zero mesmo após leitura parcial. Um cabeçalho entregue em partes pode sobrescrever os bytes já lidos. Achado estático, não reproduzido contra o pacote distribuído.
- O exemplo oficial usa `innerHTML` para mostrar mensagens. Nossa UI deve usar texto seguro.
- A raiz do repositório oficial apresenta Apache-2.0, enquanto `main.js` do exemplo conserva aviso BSD. Antes de copiar trechos literalmente, resolver a licença aplicável ao arquivo e preservar avisos; não assumir a licença da raiz para todos os arquivos.

## Base escolhida para a implementação

1. Seguir o fluxo Manifest V3/Native Messaging do exemplo oficial.
2. Reutilizar o `QwPing.cs` do projeto, já testado com UDP local, num host sem interface .NET.
3. Usar as APIs de streams/JSON da biblioteca padrão .NET; leitura exata do cabeçalho e corpo, tamanho limitado e saída stdout reservada ao protocolo.
4. Site autorizado → service worker → `connectNative` → helper. Medir o RTT dentro do helper, excluindo tempo de IPC.
5. Installer por usuário com manifest do host e ID próprio da extensão. Não copiar a chave/ID do exemplo.
6. Testar mensagens fragmentadas, UTF-8, EOF, tamanho inválido, origem recusada, helper ausente/desconectado e UDP sem resposta.

Não foi encontrado, nos candidatos examinados, um projeto pronto que una Manifest V3, instalador Windows e métricas QW de RTT/jitter/perda. A combinação acima reutiliza o transporte documentado e o código de domínio já existente, sem importar uma API genérica de sockets.
