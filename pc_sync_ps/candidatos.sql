-- PCP Naves: CONSULTA DE TESTE, usada só pelo comparar.bat (o sincronismo de hora em hora usa consulta.sql).
-- Igual à consulta.sql, mais colunas candidatas "Coluna~origem" para descobrir de onde a exportação tira
-- o Volume do Lançamento Final e a Taxa de Aço Protendido. Pode apagar depois que as colunas baterem.
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
itp AS (            -- item: soma pelas peças de projeto (x quantidade)
    SELECT pp.CODITEM,
           SUM(ISNULL(aco.PROTENDIDO, 0) * ISNULL(NULLIF(pp.QUANTIDADE, 0), 1)) AS PROT,
           SUM(COALESCE(pp.VOLUMELF, pp.VOLUME) * ISNULL(NULLIF(pp.QUANTIDADE, 0), 1)) AS VLF,
           SUM(pp.VOLUME * ISNULL(NULLIF(pp.QUANTIDADE, 0), 1)) AS V,
           SUM(ISNULL(NULLIF(pp.QUANTIDADE, 0), 1)) AS N
    FROM PPECAS pp JOIN obras ob ON ob.CODOBRA = pp.CODOBRA
    LEFT JOIN aco ON aco.CODPECA = pp.CODPECA
    GROUP BY pp.CODITEM
),
itr AS (            -- item: soma pelas peças físicas ativas
    SELECT pp.CODITEM, SUM(ISNULL(aco.PROTENDIDO, 0)) AS PROT,
           SUM(COALESCE(pp.VOLUMELF, pp.VOLUME)) AS VLF, SUM(pp.VOLUME) AS V, COUNT(*) AS N
    FROM PJREGPROJETO rp JOIN obras ob ON ob.CODOBRA = rp.CODOBRA
    JOIN PPECAS pp ON pp.CODPECA = rp.CODPECA
    LEFT JOIN aco ON aco.CODPECA = pp.CODPECA
    WHERE ISNULL(rp.STATUS, '') <> 'D'
    GROUP BY pp.CODITEM
),
fa AS (             -- aço protendido lançado na fabricação (por código)
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
    COALESCE(pp.VOLUMELF, pp.VOLUME)                    AS [Volume (m³) Total de Peças (Lanç. Final)],
    CONVERT(date, f.DATA)                               AS [Data Fabricação],
    f.VOLUMEREAL                                        AS [Vol. (m³) Real da Peça Fabricada],
    aco.FROUXO                                          AS [Peso Aço Frouxo(kg)],
    CAST(ISNULL(aco.PROTENDIDO, 0) / NULLIF(COALESCE(pp.VOLUMELF, pp.VOLUME), 0) AS decimal(12, 2)) AS [Taxa Aço Protendido(kg/m³)],
    pp.COMPRIMENTO                                      AS [Comprimento (m)],
    t.NOMETRACO                                         AS [Traço Real],
    arm.DATAHORA                                        AS [Hora da Armação],
    cdo.DATAHORA                                        AS [Hora do Corte e Dobra],
    mp.DESCRICAO                                        AS [Modelo de Protensão],
    pp.VOLUME                                           AS [Volume Teórico de Projeto],
    sf.NOMESUBFORMA                                     AS [Fôrma de Fabricação],
    rp.STATUS                                           AS [Status Projeto],
    CAST(it.M3UNIT AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~item M3UNIT],
    CAST(it.M3TOTAL / NULLIF(it.QTDEITEM, 0) AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~item M3TOTAL/QTDE],
    CAST(ii.M3UNIT AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~item inicial M3UNIT],
    CAST(pp.VOLUMETOTAL AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~peça VOLUMETOTAL],
    CAST(pp.VOLUMETOTAL / NULLIF(pp.QUANTIDADE, 0) AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~peça VOLUMETOTAL/QTDE],
    CAST(pp.VOLUMELF AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~peça VOLUMELF],
    CAST(pp.VOLUME AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~peça VOLUME],
    CAST(itp.VLF / NULLIF(itp.N, 0) AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~média projeto do item LF],
    CAST(itr.VLF / NULLIF(itr.N, 0) AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~média física do item LF],
    CAST(itr.V / NULLIF(itr.N, 0) AS decimal(14, 4)) AS [Volume (m³) Total de Peças (Lanç. Final)~média física do item],
    CAST(it.TAXAPROTENDIDA AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item final],
    CAST(ii.TAXAPROTENDIDA AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item inicial],
    CAST(pp.TAXAACO AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~peça TAXAACO],
    CAST(ISNULL(aco.PROTENDIDO, 0) / NULLIF(pp.VOLUME, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~prot/VOLUME],
    CAST(ISNULL(aco.PROTENDIDO, 0) / NULLIF(it.M3UNIT, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~prot/item M3UNIT],
    CAST(itp.PROT / NULLIF(itp.VLF, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item projeto prot/VLF],
    CAST(itp.PROT / NULLIF(itp.V, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item projeto prot/V],
    CAST(itr.PROT / NULLIF(itr.VLF, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item físico prot/VLF],
    CAST(itr.PROT / NULLIF(itr.V, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item físico prot/V],
    CAST(itr.PROT / NULLIF(itr.N * it.M3UNIT, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~item físico prot/M3UNIT],
    CAST(fa.PROT / NULLIF(pp.VOLUME, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~fabricação prot/VOLUME],
    CAST(fa.PROT / NULLIF(f.VOLUMEREAL, 0) AS decimal(14, 4)) AS [Taxa Aço Protendido(kg/m³)~fabricação prot/vol real]
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
LEFT JOIN PITENSOBRAS ii  ON ii.CODITEM = it.CODLANCINICIAL
LEFT JOIN itp             ON itp.CODITEM = pp.CODITEM
LEFT JOIN itr             ON itr.CODITEM = pp.CODITEM
LEFT JOIN fa              ON fa.CODIGOCONTROLE = rp.CODIGOCONTROLE
WHERE ISNULL(rp.STATUS, '') <> 'D'      -- D: peça desativada no projeto (não sai na exportação)
ORDER BY ob.SIGLA, pp.NOMEPECA, rp.CODIGOCONTROLE;
