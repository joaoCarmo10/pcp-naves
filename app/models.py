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
    item: Mapped[str] = mapped_column(String(20), default="")                # "Item" do Plannix: etapa/bloco da obra
    vol_projeto: Mapped[float | None] = mapped_column(Float, nullable=True)  # volume teórico de projeto (m³)
    fabricada: Mapped[bool] = mapped_column(Boolean, default=False)
    data_fabricacao: Mapped[date | None] = mapped_column(Date, nullable=True)
    origem_fabricacao: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'plannix' (com data) ou 'saiu_lista'
    data_baixa: Mapped[date | None] = mapped_column(Date, nullable=True)                # dia em que saiu da lista "a realizar"
    hora_armacao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hora_corte_dobra: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    traco: Mapped[str] = mapped_column(String(40), default="")                 # traço real do concreto (Plannix)
    taxa_aco_prot: Mapped[float | None] = mapped_column(Float, nullable=True)  # kg/m³ (Plannix)
    modelo_protensao: Mapped[str] = mapped_column(String(60), default="")
    atualizada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    obra: Mapped[Obra] = relationship(back_populates="pecas")


class Importacao(Base):
    __tablename__ = "importacoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    arquivo: Mapped[str] = mapped_column(String(200))
    tipo: Mapped[str] = mapped_column(String(20))                          # 'completa', 'a_realizar', 'programacao'
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resumo: Mapped[dict] = mapped_column(JSON, default=dict)


class RevisaoProg(Base):
    """Uma versão da programação (planilha importada). Os itens ficam congelados para o programado x realizado."""
    __tablename__ = "revisoes_prog"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(200))
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    itens: Mapped[list["ItemProg"]] = relationship(back_populates="revisao", cascade="all, delete-orphan")


class ItemProg(Base):
    __tablename__ = "itens_prog"
    id: Mapped[int] = mapped_column(primary_key=True)
    revisao_id: Mapped[int] = mapped_column(ForeignKey("revisoes_prog.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    nave: Mapped[str] = mapped_column(String(4), default="")
    forma: Mapped[str] = mapped_column(String(80), default="")
    obra: Mapped[str] = mapped_column(String(80), default="")
    peca: Mapped[str] = mapped_column(String(60), default="")
    codigo: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    produto: Mapped[str] = mapped_column(String(40), default="")
    secao: Mapped[str] = mapped_column(String(40), default="")
    comprimento: Mapped[float | None] = mapped_column(Float, nullable=True)
    qtd: Mapped[int] = mapped_column(Integer, default=1)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    tipo: Mapped[str] = mapped_column(String(30), default="")
    obs: Mapped[str] = mapped_column(String(300), default="")
    revisao: Mapped[RevisaoProg] = relationship(back_populates="itens")
    apontamento: Mapped["Apontamento | None"] = relationship(back_populates="item", uselist=False, cascade="all, delete-orphan")


class Apontamento(Base):
    """Apontamento do PCP sobre um item programado: realizado (S/N) e, quando não cumprido, causa e ação."""
    __tablename__ = "apontamentos"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("itens_prog.id", ondelete="CASCADE"), unique=True)
    realizado: Mapped[str | None] = mapped_column(String(1), nullable=True)   # 'S', 'N' ou vazio
    ocorrido: Mapped[str] = mapped_column(String(300), default="")   # o que houve (legado)
    data_execucao: Mapped[date | None] = mapped_column(Date, nullable=True)   # concretagem informada pelo PCP quando o Plannix não tem
    comentario: Mapped[str] = mapped_column(String(500), default="")
    causa: Mapped[str] = mapped_column(String(60), default="")
    acao: Mapped[str] = mapped_column(String(300), default="")
    responsavel: Mapped[str] = mapped_column(String(80), default="")
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    item: Mapped[ItemProg] = relationship(back_populates="apontamento")


class Programacao(Base):  # legado (versão 1); substituída por RevisaoProg/ItemProg
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


class Forma(Base):
    """Fôrma (ou pista) de uma nave. Cada linha é uma versão com vigência: ampliar ou mudar a fôrma a partir de
    uma data encerra a versão anterior na véspera e cria outra com o mesmo código."""
    __tablename__ = "formas"
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30), index=True)          # ex.: N1F2 (igual entre versões)
    nave: Mapped[str] = mapped_column(String(4))                          # N1, N2, N3
    nome: Mapped[str] = mapped_column(String(60))                         # ex.: F2 80x60
    tipo: Mapped[str] = mapped_column(String(10), default="linear")       # linear | unidade | pista
    secao: Mapped[str] = mapped_column(String(40), default="")            # como a fábrica chama, ex.: 80x60
    secoes_aceitas: Mapped[str] = mapped_column(String(120), default="")  # adaptações, ex.: 40x60, 40x50
    produtos: Mapped[str] = mapped_column(String(120), default="")        # restringe por produto, ex.: ESCADA
    comprimento: Mapped[float | None] = mapped_column(Float, nullable=True)   # m úteis (linear e pista)
    capacidade: Mapped[float] = mapped_column(Float, default=1)           # linear/pista: giros por dia; unidade: peças por dia
    ritmo_dias: Mapped[int] = mapped_column(Integer, default=1)           # produz a cada N dias (escada 2 = 2)
    inicio: Mapped[date | None] = mapped_column(Date, nullable=True)      # vigência
    fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    situacao: Mapped[str] = mapped_column(String(12), default="ativa")    # ativa | manutencao | desativada
    obs: Mapped[str] = mapped_column(String(300), default="")
    x: Mapped[float] = mapped_column(Float, default=0)                    # posição no desenho da nave (m)
    y: Mapped[float] = mapped_column(Float, default=0)

    def vigente(self, d: date) -> bool:
        return (self.inicio is None or self.inicio <= d) and (self.fim is None or self.fim >= d) and self.situacao != "desativada"

    @property
    def nome_completo(self) -> str:
        extra = f" ({self.comprimento:g}m)" if self.comprimento else ""
        return f"{self.nave} - {self.nome}{extra}"


class Armacao(Base):
    """Armação marcada no app. Vale para a peça (código), não para a linha da programação: se a peça muda de dia
    ou de revisão, continua armada. Lotes sem código (ex.: NJ da planilha) ficam pelo item."""
    __tablename__ = "armacoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    armado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    obs: Mapped[str] = mapped_column(String(200), default="")


class Divulgacao(Base):
    """Lista de armação divulgada ao armador para um dia (foto da programação no momento da divulgação)."""
    __tablename__ = "divulgacoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    dia: Mapped[date] = mapped_column(Date, index=True)
    revisao_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    itens: Mapped[list] = mapped_column(JSON, default=list)   # [{id, codigo, obra, peca, forma, nave, secao, comp, qtd, produto}]


class Perfil(Base):
    """Perfil de acesso: nível por módulo (nenhum, ver, editar)."""
    __tablename__ = "perfis"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60), unique=True)
    permissoes: Mapped[dict] = mapped_column(JSON, default=dict)
    sistema: Mapped[bool] = mapped_column(Boolean, default=False)   # Administrador: tudo, não editável
    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="perfil")


class Usuario(Base):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(60), unique=True)
    nome: Mapped[str] = mapped_column(String(120), default="")
    senha_hash: Mapped[str] = mapped_column(String(200))
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfis.id"))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    trocar_senha: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    ultimo_acesso: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    perfil: Mapped[Perfil] = relationship(back_populates="usuarios")


class Auditoria(Base):
    """Quem alterou o quê (toda gravação feita no app)."""
    __tablename__ = "auditoria"
    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    login: Mapped[str] = mapped_column(String(60), default="")
    acao: Mapped[str] = mapped_column(String(300), default="")


class CadastroPeca(Base):
    """Características do tipo de peça (obra + nome), lidas da prancha de detalhamento. Todas as unidades do
    Plannix com esse nome na obra herdam este cadastro."""
    __tablename__ = "cadastro_pecas"
    __table_args__ = (UniqueConstraint("obra_id", "peca", "etapas"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    obra_id: Mapped[int] = mapped_column(ForeignKey("obras.id"), index=True)
    peca: Mapped[str] = mapped_column(String(40))
    etapas: Mapped[str] = mapped_column(String(120), default="")     # itens (etapas/blocos) do Plannix a que a prancha se aplica
    prancha: Mapped[str] = mapped_column(String(20), default="")
    revisao: Mapped[str] = mapped_column(String(10), default="")
    arquivo: Mapped[str] = mapped_column(String(300), default="")      # caminho relativo do PDF
    nome_arquivo: Mapped[str] = mapped_column(String(200), default="")
    fonte: Mapped[str] = mapped_column(String(10), default="texto")    # texto | ocr
    quantidade: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_un: Mapped[float | None] = mapped_column(Float, nullable=True)
    peso: Mapped[float | None] = mapped_column(Float, nullable=True)
    area: Mapped[float | None] = mapped_column(Float, nullable=True)
    fck: Mapped[float | None] = mapped_column(Float, nullable=True)
    fcj: Mapped[float | None] = mapped_column(Float, nullable=True)
    cobrimento: Mapped[float | None] = mapped_column(Float, nullable=True)
    aco_passivo: Mapped[float | None] = mapped_column(Float, nullable=True)   # kg por peça
    aco_ativo: Mapped[float | None] = mapped_column(Float, nullable=True)     # kg por peça
    cordoalhas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cord_diam: Mapped[str] = mapped_column(String(20), default="")
    forca: Mapped[float | None] = mapped_column(Float, nullable=True)         # kgf por cordoalha
    contraflecha: Mapped[float | None] = mapped_column(Float, nullable=True)
    icamento: Mapped[str] = mapped_column(String(120), default="")
    neoprene: Mapped[str] = mapped_column(String(120), default="")
    bitolas: Mapped[dict] = mapped_column(JSON, default=dict)                 # {"12.5": kg}
    itens: Mapped[list] = mapped_column(JSON, default=list)
    alertas: Mapped[list] = mapped_column(JSON, default=list)
    situacao: Mapped[str] = mapped_column(String(12), default="ok")           # ok | revisar | conferido
    historico: Mapped[list] = mapped_column(JSON, default=list)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    obra: Mapped["Obra"] = relationship()

    @property
    def protensao(self) -> str:
        return f"{self.cordoalhas}×Ø{self.cord_diam}" if self.cordoalhas else ""


class Config(Base):
    """Valores soltos do sistema (ex.: hash do token da sincronização do Plannix, último contato)."""
    __tablename__ = "config"
    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(String(500), default="")
