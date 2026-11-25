-- PCP Naves: "all data" lido direto do banco do Plannix (PrecastControl), só leitura.
-- Uma linha por peça física (código de controle), com os mesmos nomes de coluna da exportação do Plannix.
-- {SIGLAS} é trocado pelo script pela lista das obras ativas no app (ex.: '8243','8249').
-- Versão 3 (24/11): conferida com a exportação. Volume = M3UNIT do item do lançamento final (100%);
-- taxa protendida = aço protendido da peça / volume de projeto (96,5%), e quando a peça não tem aço
-- protendido no projeto usa o lançado na fabricação; sem peças com status D.
-- Versão 4 (25/11): + Etapa (estrutura/bloco da obra no Plannix), para separar peças de mesmo nome.
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

WITH obras AS (
    SELECT o.CODOBRA, o.SIGLA, o.NOMEOBRA
    FROM POBRAS o
    WHERE LTRIM(RTRIM(o.SIGLA)) IN ({SIGLAS}) OR LTRIM(RTRIM(o.CODOBRAEXTERNO)) IN ({SIGLAS})
),
fab AS (            -- último registro de fabricação de cada peça
    SELECT f.CODIGOCONTROLE, f.DATA, f.CODTRACO, f.CODSUBFORMA, f.STATUS, f.VOLUMEREAL,
           ROW_NUMBER() OVER (PARTITION BY f.CODIGOCONTROLE ORDER BY f.DATA DESC, f.CODREGFABRICACAO DESC) AS n
    FROM PREGFABRICACAO f JOIN obras ob ON ob.CODOBRA = f.CODOBRA
),
arm AS (
    SELECT a.CODIGOCONTROLE, MAX(a.DATAHORA) AS DATAHORA
    FROM FBARMACAO a JOIN obras ob ON ob.CODOBRA = a.CODOBRA GROUP BY a.CODIGOCONTROLE
),
cdo AS (
    SELECT c.CODIGOCONTROLE, MAX(c.DATAHORA) AS DATAHORA
    FROM FBCORTEDOBRA c JOIN obras ob ON ob.CODOBRA = c.CODOBRA GROUP BY c.CODIGOCONTROLE
),
aco AS (            -- aço do projeto da peça: frouxo e protendido separados
    SELECT ap.CODPECA,
           SUM(CASE WHEN ISNULL(a.PROTENDIDA, 'N') IN ('S', '1') THEN 0 ELSE ap.PESO END) AS FROUXO,
           SUM(CASE WHEN ISNULL(a.PROTENDIDA, 'N') IN ('S', '1') THEN ap.PESO ELSE 0 END) AS PROTENDIDO
    FROM PJACOPECA ap JOIN obras ob ON ob.CODOBRA = ap.CODOBRA
    LEFT JOIN PJACO a ON a.CODACO = ap.CODACO
    GROUP BY ap.CODPECA
),
fa AS (             -- aço protendido lançado na fabricação (reserva quando o projeto não tem)
    SELECT x.CODIGOCONTROLE, SUM(CASE WHEN ISNULL(a.PROTENDIDA, 'N') IN ('S', '1') THEN x.PESO ELSE 0 END) AS PROT
    FROM FBACOPECA x JOIN obras ob ON ob.CODOBRA = x.CODOBRA
    LEFT JOIN PJACO a ON a.CODACO = x.CODIGOACO
    GROUP BY x.CODIGOCONTROLE
)
SELECT
    pp.NOMEPECA                                         AS [Peça],
    rp.CODIGOCONTROLE                                   AS [Código],
    CASE WHEN ob.NOMEOBRA LIKE LTRIM(RTRIM(ob.SIGLA)) + '-%' THEN ob.NOMEOBRA
         ELSE LTRIM(RTRIM(ob.SIGLA)) + '-' + ob.NOMEOBRA END AS [Nome Obra],
    LTRIM(RTRIM(ob.SIGLA))                              AS [Sigla Obra],
    it.ORDEMITEM                                        AS [Item],
    g.GRUPOPRODUTO                                      AS [Produto],
    s.SECAO                                             AS [Seção],
    1                                                   AS [Qtde de Peças (Lanç. Final)],
    it.M3UNIT                                           AS [Volume (m³) Total de Peças (Lanç. Final)],
    CONVERT(date, f.DATA)                               AS [Data Fabricação],
    f.VOLUMEREAL                                        AS [Vol. (m³) Real da Peça Fabricada],
    aco.FROUXO                                          AS [Peso Aço Frouxo(kg)],
    CAST(COALESCE(NULLIF(aco.PROTENDIDO, 0), fa.PROT, 0) / NULLIF(pp.VOLUME, 0) AS decimal(12, 2)) AS [Taxa Aço Protendido(kg/m³)],
    pp.COMPRIMENTO                                      AS [Comprimento (m)],
    t.NOMETRACO                                         AS [Traço Real],
    arm.DATAHORA                                        AS [Hora da Armação],
    cdo.DATAHORA                                        AS [Hora do Corte e Dobra],
    mp.DESCRICAO                                        AS [Modelo de Protensão],
    pp.VOLUME                                           AS [Volume Teórico de Projeto],
    sf.NOMESUBFORMA                                     AS [Fôrma de Fabricação],
    rp.STATUS                                           AS [Status Projeto],
    LTRIM(RTRIM(e.NOMEETAPA))                           AS [Etapa]
FROM PJREGPROJETO rp
JOIN obras ob             ON ob.CODOBRA = rp.CODOBRA
JOIN PPECAS pp            ON pp.CODPECA = rp.CODPECA
LEFT JOIN GPRODUTOS gp    ON gp.CODPRODUTO = pp.CODPRODUTO
LEFT JOIN PGRUPOPRODUTO g ON g.CODGRUPOPRODUTO = gp.CODGRUPO
LEFT JOIN PSECAO s        ON s.CODSECAO = gp.CODSECAO
LEFT JOIN PITENSOBRASFINAL it ON it.CODITEM = pp.CODITEM
LEFT JOIN fab f           ON f.CODIGOCONTROLE = rp.CODIGOCONTROLE AND f.n = 1
LEFT JOIN PTRACO t        ON t.CODTRACO = f.CODTRACO
LEFT JOIN PJMODELOPROTENCAO mp ON mp.CODIGOMODELOPROTENCAO = pp.CODIGOMODELOPROTENCAO
LEFT JOIN PSUBFORMAS sf   ON sf.CODSUBFORMA = f.CODSUBFORMA
LEFT JOIN arm             ON arm.CODIGOCONTROLE = rp.CODIGOCONTROLE
LEFT JOIN cdo             ON cdo.CODIGOCONTROLE = rp.CODIGOCONTROLE
LEFT JOIN aco             ON aco.CODPECA = pp.CODPECA
LEFT JOIN fa              ON fa.CODIGOCONTROLE = rp.CODIGOCONTROLE
LEFT JOIN PETAPAS e       ON e.CODETAPA = COALESCE(rp.CODETAPA, pp.CODETAPA)
WHERE ISNULL(rp.STATUS, '') <> 'D'      -- D: peça desativada no projeto (não sai na exportação)
ORDER BY ob.SIGLA, pp.NOMEPECA, rp.CODIGOCONTROLE;
