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

A URL do backend está fixa em `TrayApp.cs` (`BackendBaseUrl`,
`http://127.0.0.1:8730` por padrão) — ajuste antes de distribuir pra
outros testadores apontando pro coletor real.

## Escopo (v1)

- Sem integração automática com ezQuake.
- Sem autenticação (identidade é um UUID local, não uma conta).
- Rota calculada é pessoal — não alimenta a malha compartilhada.

Ver spec completo:
`docs/superpowers/specs/2026-09-20-player-ping-app-design.md`.
