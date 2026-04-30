# pyACDC RF - Uso em modo backend, TUI e Web

Este documento descreve como operar o sistema com arquitetura cliente-servidor:

- Backend no Raspberry Pi (controle dos instrumentos)
- Frontend TUI em outro computador
- Frontend Web em outro computador

## 1) Visao geral da arquitetura

- O Raspberry Pi executa o backend REST e conversa com os instrumentos via GPIB.
- A medicao **nao inicia automaticamente**.
- A medicao inicia somente por comando `start` enviado por um frontend (TUI ou Web).
- O backend disponibiliza endpoints REST para status, start, stop e configuracao.

## 2) Configuracao do token (autenticacao)

A autenticacao e feita por token simples via header HTTP `X-Auth-Token`.

No arquivo `config.ini`, use:

```ini
[Security]
token = SEU_TOKEN_AQUI
```

Observacao:

- Se `token` estiver vazio, a autenticacao fica desabilitada.
- Recomenda-se sempre definir token em ambiente de rede.

## 3) Como gerar um token

Use um token aleatorio com bom tamanho (ex.: 32 bytes).

Opcao recomendada (Linux/macOS):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Alternativa com OpenSSL:

```bash
openssl rand -hex 32
```

Copie o valor gerado para `config.ini` em `[Security]/token`.

## 4) Instalar dependencias

Dependencias obrigatorias:

```bash
pip install -r requirements.txt
```

Opcional (BME280):

```bash
pip install -r requirements-bme280.txt
```

## 5) Subir o backend no Raspberry Pi

No Raspberry (maquina de medicao):

```bash
python3 pyacdc.py --mode backend --host 0.0.0.0 --port 8000
```

Backend REST disponivel em `http://IP_DO_RPI:8000`.

## 6) Rodar frontend TUI em outro computador

```bash
python3 pyacdc.py --mode tui --server http://IP_DO_RPI:8000 --token SEU_TOKEN_AQUI
```

Comandos disponiveis na TUI:

- `start`
- `stop`
- `status`
- `help`
- `quit`

## 7) Rodar frontend Web em outro computador

O frontend web roda localmente no segundo computador e atua como cliente/proxy do backend.

```bash
python3 pyacdc.py --mode web --host 0.0.0.0 --port 8080 --server http://IP_DO_RPI:8000 --token SEU_TOKEN_AQUI
```

Abra no navegador:

```text
http://IP_DO_PC2:8080
```

## 8) Endpoints REST do backend

Todos exigem token quando `[Security]/token` nao estiver vazio.

- `GET /status` - estado completo da medicao e dados de interface
- `GET /commands` - lista de comandos
- `POST /start` - inicia medicao
- `POST /stop` - interrompe medicao
- `GET /config` - le configuracao ativa
- `POST /config` - atualiza configuracao (bloqueado se medicao em execucao)

Header de autenticacao:

```text
X-Auth-Token: SEU_TOKEN_AQUI
```

## 9) Campos editaveis pela interface Web

- Modelo do voltimetro STD e DUT
- Tensao
- Frequencias
- `r_dut`, `r_std`
- `repeticoes`, `wait_time`, `aquecimento`
- `delta_max_ppm`
- `measurement_cycle` (`RF-AC-RF-AC-RF` ou `AC-RF-AC`)

## 10) Dicas operacionais

- Inicie sempre o backend primeiro.
- Verifique conectividade de rede entre PC cliente e Raspberry.
- Se houver erro de autenticacao, confira token no `config.ini` e no argumento `--token`.
- Nao altere configuracao durante medicao (o backend bloqueia `POST /config` nesse estado).
