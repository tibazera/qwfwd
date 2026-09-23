# qwfwd player ping app (protótipo)

App (Avalonia, cross-platform) que mede seu ping UDP real (protocolo QW
nativo, `getchallenge`) até os servidores conhecidos pelo coletor qwfwd, e
pede ao backend a melhor rota calculada com esses dados.

Protótipo: sem instalador, sem assinatura de código.

## Requisitos

- .NET 8 SDK (Windows, Linux ou macOS)
- Um coletor `collector/collector.py` rodando (local ou remoto) — ver
  `docs/superpowers/plans/2026-09-20-player-ping-backend.md`

## Build e execução

```
cd player-ping-app
dotnet build
dotnet run
```

Abre uma janela: "Find best route" mede o ping até os servidores
conhecidos e mostra a melhor rota. Testado com `dotnet build` +
`dotnet publish -r linux-x64 --self-contained false` no Windows; rodar de
fato em Linux/macOS ainda não verificado neste ambiente (sem WSL com GUI
X11/Wayland disponível aqui) — validar manualmente antes de distribuir.

## Configuração

A URL do backend está fixa em `MainWindow.axaml.cs` (`BackendBaseUrl`) —
hoje aponta pro coletor público via túnel Cloudflare. Se esse túnel mudar
ou você quiser testar contra um coletor local, troque essa constante e
rebuilde.

## Testar junto com o site (bridge local)

O site público (gh-pages) tenta usar o ping real deste app automaticamente
quando ele está aberto na sua máquina — sem precisar configurar nada no
site. Como funciona:

1. Rode o app (`dotnet run`, ou o binário publicado) e deixe a janela
   aberta. Ele sobe um servidor HTTP local em `127.0.0.1:5757`
   (`LocalPingServer.cs`) só com o endpoint `GET /ping?target=ip:port`.
2. Abra o site normalmente no navegador.
3. Ao clicar num servidor no mapa, o site tenta primeiro `fetch()` nesse
   endpoint local (timeout ~400ms). Se responder, usa o RTT real medido
   pelo app e mostra "medido via app". Sem o app aberto, cai pro STUN e
   depois pra estimativa geográfica — como já fazia antes, sem popup nem
   erro visível.
4. Pra checar a ponte manualmente sem o site: com o app aberto,
   `curl "http://127.0.0.1:5757/ping?target=<ip>:<porta>"` deve responder
   `{"rtt_ms": ...}` (ou `{"error":"timeout"}` se o alvo não respondeu).

Se a porta 5757 já estiver em uso (outra instância do app, outro
processo), o app loga e segue funcionando normalmente — só a ponte com o
site fica indisponível, o scan manual continua igual.

Detalhes de arquitetura: `docs/superpowers/specs/2026-09-21-site-app-local-bridge-design.md`.

## Escopo (v1)

- Sem integração automática com ezQuake.
- Sem autenticação (identidade é um UUID local, não uma conta).
- Rota calculada é pessoal — não alimenta a malha compartilhada.

Ver spec completo:
`docs/superpowers/specs/2026-09-20-player-ping-app-design.md`.
