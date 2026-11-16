# PCP Naves

Sistema de acompanhamento da produção da fábrica de pré-fabricados. Primeiro módulo: **avanço físico das obras sobre as plantas de montagem**.

## O que já faz

- Importa as exportações do Plannix (`.xls`): completa (com data de fabricação) e lista "a realizar". Peça que sai da lista "a realizar" passa a contar como fabricada.
- Importa a programação do PCP (`.xlsx`, abas `N1 + N3` e `N2`) para mostrar o que está programado e para quando.
- Recebe o PDF da planta de montagem, localiza a etiqueta de cada peça e pinta conforme o status: fabricada, fabricada em parte, programada, sem programação.
- Mostra a planta com zoom e a tabela de peças ao lado (busca, filtro por status, clique na peça para localizar).
- Gera o PDF em **A2 paisagem** para impressão, com a planta marcada e a tabela de peças.
- Painel de avanço por obra (peças e m³) e por produto.
- **Programação**: cada planilha importada vira uma revisão congelada. Tela do dia agrupada por nave e fôrma, com apontamento S/N, causa, ação e responsável; quadro da quinzena por fôrma.
- **Programado x realizado**: PPC e realizado por dia, semana, nave, obra e fôrma, gráfico diário, causas de não cumprimento e lista das peças não realizadas ou atrasadas para a reunião. Aceita a planilha do PCP e a do engenheiro anterior.

## Como publicar no Railway (uma vez)

1. Em [railway.com](https://railway.com), **New Project → Deploy from GitHub repo** e escolha `pcp-naves`.
2. No mesmo projeto, **New → Database → PostgreSQL**. O Railway cria a variável `DATABASE_URL`.
3. No serviço `pcp-naves`, aba **Variables**, crie:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `APP_PASSWORD` = senha de acesso ao sistema
   - `SECRET_KEY` = um texto longo qualquer (assina o login)
   - `DATA_DIR` = `/data`
4. No serviço, **Settings → Volumes → Add Volume**, ponto de montagem `/data` (guarda as plantas e os arquivos importados).
5. **Settings → Networking → Generate Domain** para ter o endereço do sistema.
6. Na conta: **Usage → Usage limits**, coloque limite de **US$ 5**. O Railway desliga os serviços ao atingir o limite e não cobra além.

Cada envio para a branch `main` publica de novo sozinho.

## Rodar localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
APP_PASSWORD=teste uvicorn app.main:app --reload
pytest
```

Sem `DATABASE_URL`, usa SQLite em `./data/pcp.db`.

## Estrutura

```
app/
  main.py      rotas e telas
  models.py    obras, peças, importações, programação, plantas
  plannix.py   leitura das exportações do Plannix e da programação
  status.py    status e avanço por peça de projeto
  plantas.py   extração das etiquetas, imagem para a tela e PDF A2
  programacao.py  revisões da programação, classificação programado x realizado
  templates/   telas (Jinja)
  static/      estilo
```

## Próximos módulos

1. Motor de programação dentro do sistema (alocação por fôrma, regras e premissas).
2. Pátio e expedição.
