# Assistente da Igreja — WhatsApp + LLM + Departamentos

Quando um número qualquer manda mensagem para o contato da igreja no WhatsApp,
o assistente:

1. Recebe a mensagem via **Evolution API** (webhook).
2. Usa uma **LLM (Ollama)** para classificar a mensagem no departamento certo
   e gerar uma resposta para o membro.
3. Encaminha a mensagem para o **grupo do departamento** correspondente
   (ex.: Louvor, Juventude, Crianças) e responde o membro de volta.

O **board** (frontend React + Tailwind) serve para configurar o contato principal,
a LLM, os departamentos com seus grupos, e acompanhar o log de mensagens.

> Este repositório contém o **backend**. O frontend vive em um repositório
> separado (`frontend/`).

## Arquitetura

```
[WhatsApp] <-> [Evolution API] <-> [Backend FastAPI] <-> [Ollama (LLM)]
                                        |
                                   [Postgres] + [Board React]
```

- `backend/` — FastAPI + SQLAlchemy + Postgres (este repositório)
- `frontend/` — React + Vite + Tailwind CSS (repositório separado)

## Subir tudo com Docker (recomendado)

Um único `docker compose` sobe Postgres, Redis, Evolution API, Ollama e o backend:

```bash
cd backend
cp .env.example .env        # edite EVOLUTION_API_KEY com a chave da sua instância
docker compose up -d --build
docker compose exec ollama ollama pull llama3.1    # baixa o modelo
```

O backend sobe em `http://localhost:8000` (docs em `/docs`) usando Postgres
(bancos `evolution` e `church` no mesmo container Postgres).

Depois:

1. Crie a instância `church` na Evolution
   (`POST /instance/create` com `"instanceName": "church"` e o `AUTHENTICATION_API_KEY`)
   ou escaneie o QR direto pelo board.
2. Aponte o webhook da Evolution para
   `http://backend:8000/webhook/evolution` (já configurado no compose,
   serviço `WEBHOOK_GLOBAL_URL`; troque por IP público se precisar de acesso externo).

Comandos úteis:

```bash
make up      # docker compose up -d --build
make logs    # docker compose logs -f
make down    # docker compose down
```

## Backend sem Docker (desenvolvimento)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # ajuste os valores
make dev               # uvicorn main:app --reload  -> :8000
```

Sem Docker o default é SQLite (`church.db`). Para usar Postgres local,
altere `DATABASE_URL` no `.env` (ex.: `postgresql+psycopg://postgres:postgres@localhost:5432/church`).

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `DATABASE_URL` | URL do banco (SQLite local / Postgres no Docker) |
| `EVOLUTION_BASE_URL` | URL da Evolution API (Docker: `http://evolution:8080`) |
| `EVOLUTION_API_KEY` | A global `AUTHENTICATION_API_KEY` da Evolution |
| `EVOLUTION_INSTANCE` | Nome da instância (padrão `church`) |
| `WEBHOOK_TOKEN` | Token opcional para validar o webhook |
| `LLM_BASE_URL` | URL do Ollama (Docker: `http://ollama:11434`) |
| `LLM_MODEL` | Modelo (ex.: `llama3.1`) |
| `LLM_API_KEY` | API key opcional para provedores OpenAI-compatíveis |
| `LLM_TEMPERATURE` | Temperatura da LLM |

## Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxy /api -> :8000)
```

Build de produção: `npm run build` (gera `dist/`).

## Evolution API

O webhook da Evolution deve apontar para:

```
POST http://<seu-backend>:8000/webhook/evolution
```

No painel da instância, em **Webhooks**, habilite (pelo menos) o evento
**MESSAGES UPSERT** com essa URL. Se definir `WEBHOOK_TOKEN`, configure o mesmo
valor no campo de API Key do webhook na Evolution.

Para descobrir o JID de um grupo (necessário no board): entre no grupo, envie
qualquer mensagem e veja o `remoteJid` (termina em `@g.us`) nas mensagens
recebidas da instância.

## Fluxo de funcionamento

1. Alguém manda WhatsApp para o contato da igreja.
2. O webhook da Evolution entrega a mensagem ao backend.
3. A LLM classifica a mensagem entre os departamentos cadastrados e gera uma
   resposta.
4. O backend encaminha a mensagem ao grupo do departamento escolhido e envia a
   resposta da LLM de volta ao membro.
5. Tudo fica registrado no log de mensagens do board.
