import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
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
