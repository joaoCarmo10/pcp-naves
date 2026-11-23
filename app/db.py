import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# no Railway, se houver volume montado, os dados vão para ele mesmo sem configurar DATA_DIR
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data").resolve()
# sem volume e sem Postgres no Railway, tudo some a cada deploy
DADOS_EFEMEROS = bool(os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT")) and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH") and not os.getenv("DATABASE_URL")
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "plantas").mkdir(exist_ok=True)
(DATA_DIR / "importacoes").mkdir(exist_ok=True)
(DATA_DIR / "cache").mkdir(exist_ok=True)


def _url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return f"sqlite:///{DATA_DIR / 'pcp.db'}"
    # Railway entrega postgres://; o SQLAlchemy 2 com psycopg 3 usa postgresql+psycopg://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


URL = _url()
engine = create_engine(URL, pool_pre_ping=True, connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrar():
    """Ajustes de esquema que o create_all não faz (colunas novas em tabelas existentes)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "pecas" in insp.get_table_names() and "data_baixa" not in {c["name"] for c in insp.get_columns("pecas")}:
        with engine.begin() as cx:
            cx.execute(text("ALTER TABLE pecas ADD COLUMN data_baixa DATE"))
    if "apontamentos" in insp.get_table_names() and "ocorrido" not in {c["name"] for c in insp.get_columns("apontamentos")}:
        with engine.begin() as cx:
            cx.execute(text("ALTER TABLE apontamentos ADD COLUMN ocorrido VARCHAR(300) DEFAULT ''"))
    if "apontamentos" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("apontamentos")}
        with engine.begin() as cx:
            if "data_execucao" not in cols:
                cx.execute(text("ALTER TABLE apontamentos ADD COLUMN data_execucao DATE"))
            if "comentario" not in cols:
                cx.execute(text("ALTER TABLE apontamentos ADD COLUMN comentario VARCHAR(500) DEFAULT ''"))
    if "plantas" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("plantas")}
        with engine.begin() as cx:
            for col in ("eixos", "area"):
                if col not in cols:
                    cx.execute(text(f"ALTER TABLE plantas ADD COLUMN {col} JSON"))
    if "desenhos_autodoc" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("desenhos_autodoc")}
        with engine.begin() as cx:
            for col, tipo in (("url", "VARCHAR(500) DEFAULT ''"), ("hash", "VARCHAR(64) DEFAULT ''"), ("hash_baixado", "VARCHAR(64) DEFAULT ''")):
                if col not in cols:
                    cx.execute(text(f"ALTER TABLE desenhos_autodoc ADD COLUMN {col} {tipo}"))
    if "cadastro_pecas" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("cadastro_pecas")}
        with engine.begin() as cx:
            for col, tipo in (("etapa2_indicios", "JSON"), ("etapa2", "VARCHAR(4) DEFAULT ''"), ("etapa2_obs", "VARCHAR(120) DEFAULT ''")):
                if col not in cols:
                    cx.execute(text(f"ALTER TABLE cadastro_pecas ADD COLUMN {col} {tipo}"))
    if "formas" in insp.get_table_names() and "produtos" not in {c["name"] for c in insp.get_columns("formas")}:
        with engine.begin() as cx:
            cx.execute(text("ALTER TABLE formas ADD COLUMN produtos VARCHAR(120) DEFAULT ''"))
    if "premissas" in insp.get_table_names() and "limite" not in {c["name"] for c in insp.get_columns("premissas")}:
        with engine.begin() as cx:
            cx.execute(text("ALTER TABLE premissas ADD COLUMN limite INTEGER"))
    if "revisoes_prog" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("revisoes_prog")}
        with engine.begin() as cx:
            if "rascunho" not in cols:
                cx.execute(text("ALTER TABLE revisoes_prog ADD COLUMN rascunho BOOLEAN DEFAULT FALSE"))
            if "relatorio" not in cols:
                cx.execute(text("ALTER TABLE revisoes_prog ADD COLUMN relatorio JSON"))
    if "pecas" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("pecas")}
        with engine.begin() as cx:
            if "item" not in cols:
                cx.execute(text("ALTER TABLE pecas ADD COLUMN item VARCHAR(20) DEFAULT ''"))
            if "vol_projeto" not in cols:
                cx.execute(text("ALTER TABLE pecas ADD COLUMN vol_projeto FLOAT"))
            for col, tipo in (("hora_corte_dobra", "TIMESTAMP"), ("traco", "VARCHAR(40) DEFAULT ''"),
                              ("taxa_aco_prot", "FLOAT"), ("modelo_protensao", "VARCHAR(60) DEFAULT ''")):
                if col not in cols:
                    cx.execute(text(f"ALTER TABLE pecas ADD COLUMN {col} {tipo}"))
