from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Obra(Base):
    __tablename__ = "obras"
    id: Mapped[int] = mapped_column(primary_key=True)
    sigla: Mapped[str] = mapped_column(String(10), unique=True)          # código Plannix, ex.: 8246
    nome_plannix: Mapped[str] = mapped_column(String(200))
    nome: Mapped[str] = mapped_column(String(80))                         # nome curto, editável
    prioridade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    pecas: Mapped[list["Peca"]] = relationship(back_populates="obra")
    plantas: Mapped[list["Planta"]] = relationship(back_populates="obra", order_by="Planta.ordem")


class Peca(Base):
    __tablename__ = "pecas"
    id: Mapped[int] = mapped_column(primary_key=True)
    obra_id: Mapped[int] = mapped_column(ForeignKey("obras.id"), index=True)
    codigo: Mapped[str] = mapped_column(String(30), unique=True)          # código Plannix (único por peça física)
    peca: Mapped[str] = mapped_column(String(60), index=True)             # nome no projeto, ex.: VR13
    produto: Mapped[str] = mapped_column(String(40))
    secao: Mapped[str] = mapped_column(String(40), default="")
    comprimento: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    fabricada: Mapped[bool] = mapped_column(Boolean, default=False)
    data_fabricacao: Mapped[date | None] = mapped_column(Date, nullable=True)
    origem_fabricacao: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'plannix' (com data) ou 'saiu_lista'
    hora_armacao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    atualizada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    obra: Mapped[Obra] = relationship(back_populates="pecas")


class Importacao(Base):
    __tablename__ = "importacoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    arquivo: Mapped[str] = mapped_column(String(200))
    tipo: Mapped[str] = mapped_column(String(20))                          # 'completa', 'a_realizar', 'programacao'
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resumo: Mapped[dict] = mapped_column(JSON, default=dict)


class Programacao(Base):
    __tablename__ = "programacao"
    __table_args__ = (UniqueConstraint("codigo", name="uq_prog_codigo"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30), index=True)
    data: Mapped[date | None] = mapped_column(Date, nullable=True)
    forma: Mapped[str] = mapped_column(String(60), default="")
    revisao: Mapped[str] = mapped_column(String(80), default="")


class Planta(Base):
    __tablename__ = "plantas"
    id: Mapped[int] = mapped_column(primary_key=True)
    obra_id: Mapped[int] = mapped_column(ForeignKey("obras.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(120))                      # ex.: Pavimento 02
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    arquivo: Mapped[str] = mapped_column(String(200))                     # caminho relativo em DATA_DIR
    largura: Mapped[float] = mapped_column(Float, default=0)
    altura: Mapped[float] = mapped_column(Float, default=0)
    rotacao: Mapped[int] = mapped_column(Integer, default=0)
    marcas: Mapped[list] = mapped_column(JSON, default=list)              # [{"peca": "VR13", "r": [x0,y0,x1,y1]}, ...]
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    obra: Mapped[Obra] = relationship(back_populates="plantas")
