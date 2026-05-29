# pyACDC RF

Programa para medição da diferença RF-AC de conversores térmicos (TVCs),
controlando instrumentos via GPIB.

## Funcionalidades

- Medição automática da diferença RF-AC em múltiplas frequências
- Ciclos de medição configuráveis: `RF-AC-RF-AC-RF` ou `AC-RF-AC`
- Critério de exclusão por Delta configurável
- Suporte a sensor BME280 (temperatura, umidade, pressão) — opcional
- Dois voltímetros configuráveis (2182A, 182A e genéricos)
- Fontes AC/RF: modo compartilhado (33600A, 2 canais) ou separado (5700A + 33600A)
- Registro em CSV com todas as leituras, dados ambientais e informações dos TVCs
- Autenticação por token REST

## Arquitetura de execução

O sistema funciona em três modos:

| Modo | Descrição | Onde roda |
|------|-----------|-----------|
| `backend` | Serviço de medição com API REST | Raspberry Pi (conectado aos instrumentos) |
| `tui` | Interface de terminal com painéis | Qualquer computador na rede |
| `web` | Interface web responsiva | Qualquer computador com navegador |

A medição **só inicia** mediante comando enviado por um frontend.

```
┌────────────┐   HTTP/REST    ┌──────────────┐
│  Backend   │◄──────────────►│  TUI / Web   │
│  (RPi)     │                │  (PC remoto) │
└────────────┘                └──────────────┘
```

## Instalação

```bash
pip install -r requirements.txt
```

Para BME280:

```bash
pip install -r requirements-bme280.txt
```

## Uso rápido

### Backend (Raspberry Pi)

```bash
python3 pyacdc.py --mode backend --host 0.0.0.0 --port 8000
```

### TUI (qualquer PC na rede)

```bash
python3 pyacdc.py --mode tui --server http://IP_DO_RPI:8000
```

### Web (qualquer PC na rede)

```bash
python3 pyacdc.py --mode web --host 0.0.0.0 --port 8080 --server http://IP_DO_RPI:8000
```

Abra `http://IP_DO_PC2:8080` no navegador.

## Configuração

Arquivo `config.ini` com seções:

- `[Instruments]` — modelos dos voltímetros
- `[Sources]` — modo e modelos das fontes
- `[GPIB]` — endereços GPIB
- `[Measurement Config]` — parâmetros de medição
- `[Misc]` — observações e BME280
- `[TVC]` — dados dos conversores térmicos
- `[Security]` — token de autenticação

### Geração de token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Documentação detalhada

Consulte [USO_BACKEND_TUI_WEB.md](USO_BACKEND_TUI_WEB.md) para instruções
completas de instalação, configuração e operação dos modos backend, TUI e Web.

## Dependências

- numpy
- pyvisa + pyvisa-py
- rich
- flask
- requests
- smbus2 + RPi.bme280 (opcional)

## Licença

Este projeto é distribuído sob a licença GNU General Public License v2.0 (GPLv2).
Consulte o arquivo `LICENSE` para o texto completo.
