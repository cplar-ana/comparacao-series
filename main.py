import calendar
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Literal

import pandas as pd
import plotly.graph_objects as go  # type: ignore
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

st.set_page_config(layout="wide")

st.header("Comparação entre Séries Históricas")


class TipoDeVariavel(StrEnum):
    CHUVA = "Chuva"
    VAZAO = "Vazao"
    COTA = "Cota"


TABELAS_DADOS_HIDRO = {
    TipoDeVariavel.CHUVA: "Chuvas",
    TipoDeVariavel.COTA: "Cotas",
    TipoDeVariavel.VAZAO: "Vazoes",
}


CAMPOS_HIDROINFOANA = {
    TipoDeVariavel.CHUVA: "HORCHUVA",
    TipoDeVariavel.COTA: "HORNIVELADOTADO",
    TipoDeVariavel.VAZAO: "HORVAZAO",
}


type DatabaseUrl = Literal["DATABASE_HIDRO_URL", "DATABASE_HIDROINFOANA_URL"]
type NivelDeConsistencia = Literal[1, 2]


@st.cache_resource
def connection(env_var: DatabaseUrl) -> Engine:
    load_dotenv()
    connection_string = os.getenv(env_var, default="")
    connection_url = URL.create(
        "mssql+pyodbc", query={"odbc_connect": connection_string}
    )
    engine = create_engine(connection_url)
    return engine


@dataclass
class PropsDaEstacao:
    codigo: int
    nome: str
    tipo_estacao: Literal[1, 2]
    area_drenagem: float


@st.cache_data
def get_props_estacao(
    _hidro_engine: Engine, codigos_estacoes: list[int]
) -> list[PropsDaEstacao]:
    sql = f"""
        SELECT Codigo AS codigo,
               Nome AS nome,
               TipoEstacao AS tipo_estacao,
               AreaDrenagem AS area_drenagem
        FROM Estacao
        WHERE Importado + Temporario + Removido + ImportadoRepetido = 0
        AND Codigo IN ({", ".join(str(c) for c in codigos_estacoes)})
    """
    with _hidro_engine.connect() as conn:
        result = conn.execute(text(sql))  # type: ignore
        return [PropsDaEstacao(**row._mapping) for row in result]  # type: ignore



@st.cache_data
def get_serie_hidro(
    _hidro_engine: Engine,
    codigo_estacao: int,
    tipo_de_dado: TipoDeVariavel,
    nivel_consistencia: NivelDeConsistencia = 2,
) -> pd.DataFrame:
    colunas = ["Data"] +  [tipo_de_dado + f"{i:02}" for i in range(1, 32)]
    
    sql = f"""
        SELECT {",".join(colunas)}
        FROM {TABELAS_DADOS_HIDRO[tipo_de_dado]}
        WHERE EstacaoCodigo = {codigo_estacao} 
        AND NivelConsistencia = {nivel_consistencia}
        AND Importado = 0 AND Temporario = 0 
        AND Removido = 0 AND ImportadoRepetido = 0
        ORDER BY Data
    """
    
    with _hidro_engine.connect() as conn:
        result = conn.execute(text(sql))  # type: ignore
        dados_series: list[pd.Series] = []
        for row in result:
            data = row[0]
            year = data.year
            month = data.month
            dias_no_mes = calendar.monthrange(year, month)[1]
            datas = pd.date_range(start=data, periods=dias_no_mes, freq='D')
            dados_serie = pd.Series(row[1:dias_no_mes + 1], index=datas)
            dados_series.append(dados_serie)
    
    df = pd.concat(dados_series)
    df.name = f"estacao_{codigo_estacao}"
    return df.to_frame()


@st.cache_data
def get_serie_hidroinfoana(
    _hidroinfoana_engine: Engine, codigo_estacao: int, tipo_de_dado: TipoDeVariavel
) -> pd.DataFrame:
    sql = f"""
        SELECT
            CAST(HORDATAHORA AS DATE) AS Data,
            AVG({CAMPOS_HIDROINFOANA[tipo_de_dado]}) AS estacao_{codigo_estacao}
        FROM HidroInfoANA.dbo.HORARIA
        INNER JOIN HidroInfoANA.dbo.ESTACAO AS hest ON hest.ESTCODIGO = HORESTACAO
        INNER JOIN Hidro.dbo.Estacao AS est ON est.Codigo = hest.ESTCODIGOADICIONAL
        WHERE est.Codigo = {codigo_estacao}
        GROUP BY CAST(HORDATAHORA AS DATE)
        ORDER BY Data
    """
    return pd.read_sql(
        sql, con=_hidroinfoana_engine, index_col="Data", parse_dates=["Data"]
    )


type FonteDeDados = Literal["Hidro", "HidroInfoANA"]
type FuncaoFonteDeDados = dict[
    FonteDeDados, Callable[[Engine, int, TipoDeVariavel], pd.DataFrame]
]
FONTE_DE_DADOS: FuncaoFonteDeDados = {
    "Hidro": get_serie_hidro,
    "HidroInfoANA": get_serie_hidroinfoana,
}


@st.cache_data
def get_joined_dataframes(
    _engine: Engine,
    codigos_estacoes: list[int],
    fonte_dados: FonteDeDados,
    tipo_dado: TipoDeVariavel,
) -> pd.DataFrame | None:
    dataframes = [
        FONTE_DE_DADOS[fonte_dados](_engine, codigo, tipo_dado)
        for codigo in codigos_estacoes
        if codigo
    ]
    print("Gerando dataframes do", fonte_dados)
    if all([df.empty for df in dataframes]):
        return None

    return pd.concat(dataframes, axis=1)


def plot_series(df: pd.DataFrame, fonte_dados: FonteDeDados) -> go.Figure:
    fig = go.Figure()
    for estacao in df.columns:
        fig.add_trace(  # type:  ignore
            go.Scatter(
                x=df.index, y=df[estacao], mode="lines", name=estacao.split("_")[1]
            )
        )
    fig.update_layout(title_text=f"Série(s) Histórica(s). Fonte dos Dados: {fonte}")  # type: ignore
    return fig


def plot_scattter_matrixes_series(
    df: pd.DataFrame, fonte_dados: FonteDeDados
) -> go.Figure:
    fig = go.Figure()

    fig = go.Figure(
        data=go.Splom(
            dimensions=[
                dict(label=col.split("_")[1], values=df[col]) for col in df.columns
            ],
        )
    )
    fig.update_layout(  # type: ignore
        title_text=f"Gráfico de Dispersão das Série(s) Histórica(s). Fonte dos Dados: {fonte}"
    )  # type: ignore
    return fig


def plot_matriz_de_correlacao(df: pd.DataFrame, fonte_dados: FonteDeDados) -> go.Figure:
    columns = {col: f"est. {col.split('_')[1]}" for col in df.columns}
    df_new = df.rename(columns=columns)
    fig = go.Figure(
        data=go.Heatmap(
            z=df_new.corr().values,
            x=df_new.columns,
            y=df_new.columns,
            colorscale="Viridis",
            zmin=0,
            zmax=1,
        )
    )
    fig.update_layout(  # type: ignore
        title_text=f"Matriz de Correlação das Série(s) Histórica(s). Fonte dos Dados: {fonte}"
    )  # type: ignore
    return fig


hidro_engine = connection("DATABASE_HIDRO_URL")

with st.form("form"):
    st.text("Tipo de Dado:")
    dado_de_chuva = st.checkbox(label=TipoDeVariavel.CHUVA)
    dado_de_cota = st.checkbox(label=TipoDeVariavel.COTA)
    dado_de_vazao = st.checkbox(label=TipoDeVariavel.VAZAO)
    st.text("Fonte dos dados da(s) série(s):")
    fonte_hidro = st.checkbox("Hidro")
    fonte_hidroinfoana = st.checkbox("HidroInfoANA")
    str_codigos_estacoes = st.text_input(
        label="Insira o(s) código(s) da(s) estação(ões) separadas por ponto-vírgual (;):",
        placeholder="00000000; 00000000; ...",
    )

    st.form_submit_button("processa", type="primary")

tipos_selecionados: dict[TipoDeVariavel, bool] = {
    TipoDeVariavel.CHUVA: dado_de_chuva,
    TipoDeVariavel.COTA: dado_de_cota,
    TipoDeVariavel.VAZAO: dado_de_vazao,
}

fontes_selecionadas: tuple[tuple[FonteDeDados, bool], ...] = (
    ("Hidro", fonte_hidro),
    ("HidroInfoANA", fonte_hidroinfoana),
)


for tipo_dado in tipos_selecionados:
    if tipos_selecionados[tipo_dado]:
        codigos_estacoes = [int(codigo) for codigo in str_codigos_estacoes.split(";")]
        st.subheader(f"Dados de {tipo_dado}:")

        dfs: dict[FonteDeDados, pd.DataFrame] = {}
        if fonte_hidro and fonte_hidroinfoana:
            for idx, col in enumerate(st.columns(2)):
                fonte, _ = fontes_selecionadas[idx]
                with col:
                    st.text(f"Fonte dos Dados: {fonte}")
                    joint_dataframes = get_joined_dataframes(
                        hidro_engine, codigos_estacoes, fonte, tipo_dado
                    )
                    if joint_dataframes is not None:
                        st.dataframe(joint_dataframes, width="content")  # type: ignore
                        dfs[fonte] = joint_dataframes
                    else:
                        st.warning(
                            f"Não existem dados de {tipo_dado} para as estações selecionadas!"
                        )
        else:
            for fonte_dado in fontes_selecionadas:
                fonte, selecionado = fonte_dado
                if selecionado:
                    st.text(f"Fonte dos Dados: {fonte}")
                    joint_dataframes = get_joined_dataframes(
                        hidro_engine, codigos_estacoes, fonte, tipo_dado
                    )
                    if joint_dataframes is not None:
                        st.dataframe(joint_dataframes, width="content")  # type: ignore
                        dfs[fonte] = joint_dataframes
                    else:
                        st.warning(
                            f"Não existem dados de {tipo_dado} para as estações selecionadas!"
                        )

        for fonte, df in dfs.items():
            st.plotly_chart(plot_series(df, fonte))
            st.plotly_chart(plot_scattter_matrixes_series(df, fonte))
            st.plotly_chart(plot_matriz_de_correlacao(df, fonte), width="content")

            if len(df.columns) == 2:
                # Avaliação dos LAGs
                # Lag de 1 dia
                props_estacoes = get_props_estacao(hidro_engine, codigos_estacoes)
                # Sort list of PropsDaEstacao by area_drenagem
                props_estacoes.sort(key=lambda x: x.area_drenagem)
                cod_est_menor_area = props_estacoes[0].codigo
                cod_est_maior_area = props_estacoes[1].codigo
                df_lag_1dia = pd.DataFrame(
                    {
                        f"estacao_{cod_est_menor_area}": df[
                            f"estacao_{cod_est_menor_area}"
                        ],
                        f"lag1dia_{cod_est_maior_area}": df[
                            f"estacao_{cod_est_maior_area}"
                        ].shift(-1),
                    }
                )

                st.markdown("### Gráfico de Dispersão com Lag de 1 dia:")
                st.plotly_chart(plot_series(df_lag_1dia, fonte))
                st.plotly_chart(plot_scattter_matrixes_series(df_lag_1dia, fonte))
                st.plotly_chart(
                    plot_matriz_de_correlacao(df_lag_1dia, fonte), width="content"
                )

                df_lag_2dia = pd.DataFrame(
                    {
                        f"estacao_{cod_est_menor_area}": df[
                            f"estacao_{cod_est_menor_area}"
                        ],
                        f"lag2dia_{cod_est_maior_area}": df[
                            f"estacao_{cod_est_maior_area}"
                        ].shift(-2),
                    }
                )

                st.markdown("### Gráfico de Dispersão com Lag de 2 dias:")
                st.plotly_chart(plot_series(df_lag_2dia, fonte))
                st.plotly_chart(plot_scattter_matrixes_series(df_lag_2dia, fonte))
                st.plotly_chart(
                    plot_matriz_de_correlacao(df_lag_2dia, fonte), width="content"
                )
