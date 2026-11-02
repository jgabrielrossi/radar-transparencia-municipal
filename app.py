from __future__ import annotations

import html
import hmac
import io
import os
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

APP_TITLE = "Radar da Transparência Municipal"
IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
SICONFI_URL = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/rreo"
PNCP_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
REQUEST_TIMEOUT = 20
PRECO_RAIO_X = 29.00

st.set_page_config(page_title=APP_TITLE, page_icon="🏛️", layout="wide")
st.title("🏛️ Radar da Transparência Municipal")
st.write("Consulte dados públicos municipais, confira as fontes e solicite um relatório explicativo.")
st.caption("Ferramenta informativa. Os dados disponíveis não constituem, por si só, prova de irregularidade, fraude ou ilegalidade.")


def secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        value = os.getenv(name, default)
    return str(value or default).strip()


def get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any] | None:
    try:
        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "RadarTransparencia/0.2 (consulta de dados públicos)"},
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Falha de consulta em {url}: {exc}")
        return None


@st.cache_data(ttl=86400, show_spinner="Carregando municípios...")
def carregar_municipios() -> list[dict[str, str]]:
    # Lista local de referencia para a interface abrir sem depender de rede.
    # Fonte: https://github.com/kelvins/municipios-brasileiros (codigos IBGE).
    try:
        from pathlib import Path
        arquivo = Path(__file__).resolve().parent / "municipios.csv"
        if arquivo.is_file():
            tabela = pd.read_csv(arquivo, dtype={"codigo_ibge": str, "nome": str, "uf": str})
            tabela = tabela.dropna(subset=["codigo_ibge", "nome", "uf"])
            municipios_locais = [
                {"id": str(linha.codigo_ibge), "nome": str(linha.nome), "uf": str(linha.uf)}
                for linha in tabela.itertuples(index=False)
            ]
            if municipios_locais:
                return sorted(municipios_locais, key=lambda m: (m["nome"], m["uf"]))
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        print(f"Falha ao ler lista local de municipios: {exc}")

    # Contingencia apenas se o arquivo local nao estiver presente ou for invalido.
    data = get_json(IBGE_URL, {"orderBy": "nome"})
    if not isinstance(data, list):
        return []
    municipios: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id") or not item.get("nome"):
            continue
        uf = None
        imediato = item.get("regiao-imediata")
        if isinstance(imediato, dict):
            intermediario = imediato.get("regiao-intermediaria")
            if isinstance(intermediario, dict) and isinstance(intermediario.get("UF"), dict):
                uf = intermediario["UF"].get("sigla")
        if not uf:
            micro = item.get("microrregiao")
            if isinstance(micro, dict):
                meso = micro.get("mesorregiao")
                if isinstance(meso, dict) and isinstance(meso.get("UF"), dict):
                    uf = meso["UF"].get("sigla")
        if uf:
            municipios.append({"id": str(item["id"]), "nome": str(item["nome"]), "uf": str(uf)})
    return municipios


def carregar_siconfi(ibge_id: str, ano: int) -> tuple[pd.DataFrame, str]:
    params = {
        "an_exercicio": ano,
        "nr_periodo": 6,
        "co_tipo_demonstrativo": "RREO",
        "no_anexo": "RREO-Anexo 02",
        "id_ente": ibge_id,
    }
    data = get_json(SICONFI_URL, params)
    if data is None:
        return pd.DataFrame(), "Não foi possível acessar o SICONFI neste momento. Tente novamente e confira o portal oficial."
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return pd.DataFrame(), "A resposta do SICONFI veio em formato inesperado. Não é possível concluir se existem ou não dados."
    frame = pd.DataFrame(data["items"])
    if frame.empty:
        return frame, "A consulta não retornou linhas para os parâmetros utilizados. Isso não comprova ausência de despesas ou de prestação de contas."
    required = {"coluna", "conta", "valor"}
    if not required.issubset(frame.columns):
        return frame, "O demonstrativo retornou campos diferentes dos esperados. Os totais foram omitidos para evitar interpretação incorreta."
    return frame, "Consulta exploratória ao RREO/SICONFI. Antes de interpretar totais, confirme demonstrativo, período, unidade e classificação no portal oficial."


def processar_resumo(df: pd.DataFrame) -> tuple[pd.DataFrame, float | None, str]:
    import unicodedata
    if df.empty:
        return pd.DataFrame(), None, "A consulta ao SICONFI não retornou linhas. Isso não significa que não existam despesas."
    needed = {"coluna", "conta", "valor"}
    if not needed.issubset(df.columns):
        return pd.DataFrame(), None, "Campos recebidos da API: " + ", ".join(map(str, df.columns[:15]))
    def norm(x):
        return " ".join(unicodedata.normalize("NFKD", str(x)).encode("ascii", "ignore").decode("ascii").upper().split())
    work = df.copy()
    headers = work["coluna"].map(norm)
    work = work[headers.str.contains("LIQUIDAD", na=False) & headers.str.contains("ATE O BIMESTRE", na=False)].copy()
    if work.empty:
        return pd.DataFrame(), None, "Não foi localizada a coluna de despesas liquidadas até o bimestre. Colunas recebidas: " + ", ".join(map(str, df["coluna"].dropna().unique()[:8]))
    work["valor"] = pd.to_numeric(work["valor"], errors="coerce")
    work = work.dropna(subset=["valor", "conta"])
    work = work[~work["conta"].astype(str).str.contains("TOTAL", case=False, na=False)]
    if work.empty:
        return pd.DataFrame(), None, "Não restaram linhas de despesas liquidadas para exibição."
    summary = work[["conta", "valor"]].drop_duplicates().sort_values("valor", ascending=False)
    summary["percentual"] = 0.0
    note = "Despesas liquidadas até o bimestre, RREO Anexo 02. Funções e subfunções podem se sobrepor; não some as linhas nem interprete os percentuais como participação no total."
    return summary, None, note


def buscar_pncp(ibge_id: str, nome_municipio: str, ano: int, mes: int, escopo: str = "Prefeitura / Administração Direta") -> tuple[pd.DataFrame, str, pd.DataFrame]:
    """Pesquisa limitada com identificacao conservadora e diagnostico dos orgaos."""
    import unicodedata
    import calendar

    def normalizar(valor: object) -> str:
        texto = unicodedata.normalize("NFKD", str(valor or ""))
        return " ".join(texto.encode("ascii", "ignore").decode("ascii").upper().split())

    municipio_normalizado = normalizar(nome_municipio)
    nomes_prefeitura = {
        f"{prefixo} {prep} {municipio_normalizado}"
        for prefixo in ("MUNICIPIO", "PREFEITURA", "PREFEITURA MUNICIPAL")
        for prep in ("DE", "DO", "DA")
    }
    ampliada = escopo == "Administração Municipal Ampliada"
    # CNPJ da prefeitura de Mossoró para identificação adicional da entidade.
    cnpj_prefeitura = {"2408003": "08348971000139"}.get(str(ibge_id))
    modalidades = {6: "Pregão eletrônico", 8: "Dispensa de licitação"}
    max_paginas_por_modalidade = 8
    por_pagina = 50
    linhas = []
    diagnostico = []
    examinados = 0
    excluidos = 0
    consultas_validas = 0
    paginas_consultadas = 0
    falhas = []
    truncado = False

    for codigo, modalidade in modalidades.items():
        for pagina in range(1, max_paginas_por_modalidade + 1):
            params = {
                "dataInicial": f"{ano}{mes:02d}01",
                "dataFinal": f"{ano}{mes:02d}{calendar.monthrange(ano, mes)[1]:02d}",
                "codigoModalidadeContratacao": codigo,
                "codigoMunicipioIbge": ibge_id,
                "pagina": pagina,
                "tamanhoPagina": por_pagina,
            }
            try:
                resposta = requests.get(
                    PNCP_URL, params=params, timeout=REQUEST_TIMEOUT,
                    headers={"Accept": "application/json", "User-Agent": "RadarTransparencia/0.4"},
                )
                if resposta.status_code == 204:
                    consultas_validas += 1
                    paginas_consultadas += 1
                    break
                resposta.raise_for_status()
                dados = resposta.json()
                if not isinstance(dados, dict) or not isinstance(dados.get("data"), list):
                    falhas.append(f"{modalidade}, página {pagina}: formato inesperado")
                    break
            except requests.HTTPError as exc:
                http = exc.response.status_code if exc.response is not None else "?"
                falhas.append(f"{modalidade}, página {pagina}: HTTP {http}")
                print(f"Falha PNCP: {modalidade}, pagina {pagina}: {exc}")
                break
            except (requests.RequestException, ValueError) as exc:
                falhas.append(f"{modalidade}, página {pagina}: conexão ou JSON inválido")
                print(f"Falha PNCP: {modalidade}, pagina {pagina}: {exc}")
                break

            consultas_validas += 1
            paginas_consultadas += 1
            itens = dados["data"]
            for item in itens:
                if not isinstance(item, dict):
                    continue
                examinados += 1
                orgao = item.get("orgaoEntidade") or {}
                if not isinstance(orgao, dict):
                    excluidos += 1
                    continue
                razao = normalizar(orgao.get("razaoSocial") or orgao.get("nome"))
                unidade = item.get("unidadeOrgao") or {}
                if not isinstance(unidade, dict):
                    unidade = {}
                nome_unidade = normalizar(unidade.get("nomeUnidade"))
                cnpj = "".join(c for c in str(orgao.get("cnpj") or "") if c.isdigit())
                cnpj_confirmado = bool(cnpj_prefeitura and cnpj == cnpj_prefeitura)
                esfera = normalizar(orgao.get("esferaId") or item.get("esferaId"))
                poder = normalizar(orgao.get("poderId") or item.get("poderId"))
                # O valor 'N' para poder apareceu no CSV real de Mossoró.
                # Não rejeitar um órgão municipal apenas por esse valor.
                esfera_invalida = bool(esfera and esfera != "M")
                poder_invalido = bool(poder and poder not in ("E", "N"))
                prefeitura = razao in nomes_prefeitura or cnpj_confirmado
                secretaria = (
                    razao.startswith("SECRETARIA MUNICIPAL ")
                    or (razao.startswith("SECRETARIA DE ") and municipio_normalizado in razao)
                )
                unidade_fundo = nome_unidade.startswith(("FUNDO MUNICIPAL ", "FUNDO DE ", "FUNDO MUNICIPAL DE "))
                unidade_indireta = any(
                    termo in nome_unidade
                    for termo in ("PREVIDENCIA", "INSTITUTO DE PREVIDENCIA", "AUTARQUIA", "FUNDACAO MUNICIPAL")
                )
                if ampliada:
                    aceito = esfera == "M" and poder in ("", "E", "N")
                else:
                    aceito = (prefeitura or secretaria) and not unidade_fundo and not unidade_indireta
                motivo = (
                    "Esfera ou poder incompatível"
                    if esfera_invalida or poder_invalido
                    else ("Incluído" if aceito else "Identificação fora do escopo")
                )
                diagnostico.append({
                    "Órgão declarado": orgao.get("razaoSocial") or orgao.get("nome") or "Não informado",
                    "CNPJ": orgao.get("cnpj") or "Não informado",
                    "Unidade administrativa": unidade.get("nomeUnidade") or "Não informada",
                    "Esfera": esfera or "Não informada",
                    "Poder": poder or "Não informado",
                    "Situação": motivo,
                })
                if esfera_invalida or poder_invalido or not aceito:
                    excluidos += 1
                    continue
                linhas.append({
                    "Órgão": orgao.get("razaoSocial") or orgao.get("nome") or "Não informado",
                    "CNPJ do órgão": orgao.get("cnpj") or "Não informado",
                    "Unidade administrativa": unidade.get("nomeUnidade") or "Não informada",
                    "Objeto": item.get("objetoCompra") or "Descrição não informada",
                    "Modalidade": item.get("modalidadeNome") or modalidade,
                    "Valor estimado (R$)": pd.to_numeric(item.get("valorTotalEstimado"), errors="coerce"),
                    "Data de publicação": str(item.get("dataPublicacaoPncp") or "")[:10],
                    "Identificador PNCP": item.get("numeroControlePNCP") or "Não informado",
                })

            total_paginas = dados.get("totalPaginas")
            if isinstance(total_paginas, int) and pagina >= total_paginas:
                break
            if len(itens) == 0 or (total_paginas is None and len(itens) < por_pagina):
                break
            if pagina == max_paginas_por_modalidade:
                truncado = True

    descricao = (
        f"Escopo: {escopo}. Amostra de {mes:02d}/{ano} (mês completo), pregão eletrônico "
        "e dispensa de licitação. "
        f"Consultadas {paginas_consultadas} páginas (até {max_paginas_por_modalidade} "
        f"por modalidade); examinados {examinados} registros e excluídos {excluidos} "
        "pelo filtro de entidade. "
        "A pesquisa é limitada por período, modalidade e paginação; não é "
        "levantamento anual ou de todas as contratações. "
    )
    if ampliada:
        descricao += (
            "A opção ampliada considera órgãos municipais do Executivo e "
            "pode incluir previdência, fundos e autarquias, exibidos com suas "
            "razões sociais e CNPJs próprios. A classificação depende dos "
            "dados preenchidos pelos órgãos no PNCP. "
        )
    else:
        descricao += (
            "Administração Direta considera o órgão Município/Prefeitura "
            "e secretarias municipais, usando razão social, CNPJ e unidade. "
            "Exclui unidades explicitamente identificadas como fundos e "
            "entidades previdenciárias. Classificação sujeita à qualidade "
            "dos dados informados no PNCP. "
        )
    if truncado:
        descricao += "Limite de paginação atingido; podem existir mais registros. "
    if falhas:
        descricao += "Falhas: " + "; ".join(falhas) + ". "
    diagnostico_df = pd.DataFrame(diagnostico).drop_duplicates() if diagnostico else pd.DataFrame()
    if consultas_validas == 0:
        return pd.DataFrame(), "O PNCP não respondeu às consultas; não conclua ausência de contratos. " + descricao, diagnostico_df
    if not linhas:
        return pd.DataFrame(), "Nenhum registro correspondeu ao escopo na amostra consultada; isso não significa ausência de contratações. " + descricao, diagnostico_df
    resultado = pd.DataFrame(linhas)
    return resultado.drop_duplicates(subset=["Identificador PNCP"]), descricao, diagnostico_df


def moeda(valor: float) -> str:
    return (f"R$ {valor:,.2f}").replace(",", "X").replace(".", ",").replace("X", ".")


def criar_pdf(nome: str, uf: str, ano: int, resumo: pd.DataFrame, nota: str, pncp: pd.DataFrame | None = None, demonstracao: bool = True) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RadarTitle", parent=styles["Title"], fontSize=16, leading=20, textColor=colors.HexColor("#1A365D"), spaceAfter=10)
    body = ParagraphStyle("RadarBody", parent=styles["BodyText"], fontSize=8.5, leading=12, spaceAfter=7, wordWrap="CJK")
    story = []
    if demonstracao:
        story.append(Paragraph("DEMONSTRAÇÃO GRATUITA — NÃO É LAUDO PERICIAL", ParagraphStyle("Demo", parent=styles["Heading2"], textColor=colors.red, fontSize=10)))
    story.append(Paragraph(f"Radar da Transparência Municipal — {html.escape(nome)} ({html.escape(uf)}) — {ano}", title_style))
    story.append(Paragraph(f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}.", body))
    story.append(Paragraph("Este relatório organiza dados públicos disponíveis na consulta. Não comprova irregularidade, fraude, corrupção ou conformidade legal. Verifique os dados nas fontes oficiais antes de publicar conclusões.", body))
    story.append(Spacer(1, 5))
    if not demonstracao and not resumo.empty:
        story.append(Paragraph("Resumo das linhas consultadas", styles["Heading2"]))
        story.append(Paragraph(
            "Os valores são de despesas liquidadas até o bimestre no RREO Anexo 02. "
            "As linhas de função e subfunção podem se sobrepor e não devem ser somadas.",
            body,
        ))
    if not resumo.empty:
        rows = [["Conta retornada pela fonte", "Valor da linha (R$)", "Percentual"]]
        for _, row in resumo.head(5 if demonstracao else 12).iterrows():
            rows.append([Paragraph(html.escape(str(row["conta"])), body), moeda(float(row["valor"])), "N/A"])
        table = Table(rows, colWidths=[83*mm, 43*mm, 28*mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("Resumo financeiro não disponível para os parâmetros consultados. O relatório não inventa nem estima valores ausentes.", body))
    story.extend([Spacer(1, 8), Paragraph("Limitações e interpretação", styles["Heading2"]), Paragraph(html.escape(nota), body)])
    if pncp is not None:
        story.append(Paragraph("Contratações públicas — PNCP", styles["Heading2"]))
        story.append(Paragraph(f"A consulta amostral do PNCP retornou {len(pncp)} registro(s) no escopo selecionado. As modalidades, o período e a paginação limitam a cobertura; não é lista anual completa.", body))
        if not demonstracao and not pncp.empty:
            rows = [["Órgão", "Objeto", "Valor estimado (R$)"]]
            for _, row in pncp.head(8).iterrows():
                value = row.get("Valor estimado (R$)")
                value_text = moeda(float(value)) if pd.notna(value) else "Não informado"
                rows.append([Paragraph(html.escape(str(row["Órgão"])[:90]), body), Paragraph(html.escape(str(row["Objeto"])[:160]), body), value_text])
            table = Table(rows, colWidths=[45*mm, 75*mm, 34*mm], repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey), ("FONTSIZE", (0, 0), (-1, -1), 7), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(table)
    story.extend([Spacer(1, 10), Paragraph("Fontes e metodologia", styles["Heading2"]), Paragraph("Fontes utilizadas conforme disponibilidade: API de localidades do IBGE, SICONFI/Tesouro Nacional e PNCP. O SICONFI foi consultado com os parâmetros apresentados no aplicativo; o PNCP consulta somente o mês selecionado e até 8 páginas de duas modalidades, com filtragem por órgão, sem equivaler ao levantamento anual. As linhas do Anexo 02 podem refletir funções e subfunções sobrepostas e não devem ser somadas.", body), Paragraph("Links: https://servicodados.ibge.gov.br/ ; https://siconfi.tesouro.gov.br/ ; https://pncp.gov.br/", body)])
    doc.build(story)
    return buffer.getvalue()


municipios = carregar_municipios()
if not municipios:
    st.error("Não foi possível carregar os municípios do IBGE. Confira sua conexão e tente novamente mais tarde.")
    st.link_button("Abrir API de municípios do IBGE", IBGE_URL)
    st.stop()

opcoes = {f"{m['nome']} - {m['uf']}": m for m in municipios}
labels = list(opcoes.keys())
default_a = labels.index("Recife - PE") if "Recife - PE" in labels else 0
default_b = labels.index("Olinda - PE") if "Olinda - PE" in labels else min(1, len(labels) - 1)

with st.sidebar:
    st.header("Consulta gratuita")
    cidade_label = st.selectbox("Município", labels, index=default_a)
    ano = st.selectbox("Exercício", [2025, 2024, 2023, 2022], index=0)
    mes_pncp = st.selectbox("Mês das publicações no PNCP", list(range(1, 13)), index=8, format_func=lambda m: ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"][m - 1])
    escopo_pncp = st.radio(
        "Contratações incluídas",
        ["Prefeitura / Administração Direta", "Administração Municipal Ampliada"],
        help="A primeira opção inclui prefeitura e secretarias municipais identificadas. A ampliada inclui também outras entidades do Executivo municipal, como previdência e fundos.",
    )
    consultar = st.button("Consultar dados públicos", type="primary", width="stretch")
    st.caption("A cobertura depende do exercício e da disponibilidade de cada fonte.")
    chave_admin_configurada = secret("CHAVE_ADMIN_RELATORIOS")
    chave_admin_digitada = st.text_input("Acesso do responsável (opcional)", type="password", help="Use somente se você configurou CHAVE_ADMIN_RELATORIOS nos Secrets.")
    admin_ok = bool(chave_admin_configurada) and hmac.compare_digest(chave_admin_digitada, chave_admin_configurada)
    if admin_ok:
        st.success("Modo de entrega manual habilitado")

key = f"pncp_v5_dados_{opcoes[cidade_label]['id']}_{ano}_{mes_pncp}_{escopo_pncp}"
if consultar or key not in st.session_state:
    municipio = opcoes[cidade_label]
    with st.spinner("Consultando SICONFI e PNCP..."):
        raw, nota_siconfi = carregar_siconfi(municipio["id"], ano)
        resumo, total, nota_resumo = processar_resumo(raw)
        pncp, nota_pncp, diagnostico_pncp = buscar_pncp(municipio["id"], municipio["nome"], ano, mes_pncp, escopo_pncp)
        st.session_state[key] = {"municipio": municipio, "ano": ano, "raw": raw, "resumo": resumo, "total": total, "nota": f"{nota_siconfi} {nota_resumo}", "pncp": pncp, "nota_pncp": nota_pncp, "mes_pncp": mes_pncp, "diagnostico_pncp": diagnostico_pncp}
r = st.session_state[key]

st.header(f"{r['municipio']['nome']} — {r['municipio']['uf']} | {r['ano']}")
st.caption("Fontes: SICONFI/Tesouro Nacional e Portal Nacional de Contratações Públicas (PNCP), conforme disponibilidade.")
tab1, tab2, tab3 = st.tabs(["📊 Orçamento", "📑 Contratações", "📄 Relatório de R$ 29"])

with tab1:
    st.subheader("Despesas liquidadas — RREO Anexo 02")
    st.info(r["nota"])
    if not r["resumo"].empty:
        c1, c2 = st.columns(2)
        c1.metric("Maior linha exibida", moeda(float(r["resumo"]["valor"].max())))
        c2.metric("Linhas retornadas", len(r["resumo"]))
        st.caption("As linhas podem se sobrepor. Não devem ser somadas.")
        st.plotly_chart(px.bar(r["resumo"].head(12).sort_values("valor"), x="valor", y="conta", orientation="h", title="Maiores linhas retornadas"), width="stretch")
        st.dataframe(r["resumo"], width="stretch", hide_index=True)
        st.download_button("Baixar tabela exibida (CSV)", r["resumo"].to_csv(index=False).encode("utf-8-sig"), file_name=f"radar_{r['municipio']['id']}_{r['ano']}.csv", mime="text/csv")
    else:
        st.warning("O resumo está indisponível. Veja o diagnóstico acima e consulte a fonte oficial.")
    st.link_button("Consultar SICONFI oficial", "https://siconfi.tesouro.gov.br/")

with tab2:
    st.subheader(f"Contratações no PNCP — {escopo_pncp}")
    st.info(r["nota_pncp"])
    st.caption("Classificação dos órgãos refeita na versão v5. A coluna Unidade administrativa permite conferir secretarias e fundos separadamente.")
    if not r["pncp"].empty:
        st.dataframe(r["pncp"], width="stretch", hide_index=True)
        st.download_button("Baixar registros desta página (CSV)", r["pncp"].to_csv(index=False).encode("utf-8-sig"), file_name=f"pncp_{r['municipio']['id']}_{r['ano']}.csv", mime="text/csv")
    else:
        st.warning("A consulta não retornou registros para exibição. Não conclua que não houve contratação; verifique a pesquisa diretamente na fonte.")
    with st.expander("Diagnóstico: órgãos e unidades retornados pelo PNCP"):
        st.caption("Identificação conforme os campos enviados ao PNCP. A presença de um órgão não comprova vínculo administrativo com a prefeitura. Esta tabela é apenas para conferir filtros; não integra o PDF.")
        if not r["diagnostico_pncp"].empty:
            st.dataframe(r["diagnostico_pncp"], width="stretch", hide_index=True)
            st.download_button("Baixar diagnóstico CSV", r["diagnostico_pncp"].to_csv(index=False).encode("utf-8-sig"), file_name="diagnostico_pncp.csv", mime="text/csv")
        else:
            st.write("Nenhum órgão recebido para diagnóstico.")
    st.link_button("Consultar PNCP oficial", "https://pncp.gov.br/")

with tab3:
    st.subheader("Raio-X municipal — relatório inicial")
    st.metric("Preço de lançamento", moeda(PRECO_RAIO_X))
    st.markdown("**O que o relatório inclui**")
    st.markdown("- Resumo dos dados retornados para o município e exercício escolhidos.\n- Tabela e gráfico das categorias retornadas, quando os dados permitem.\n- Amostra das contratações identificadas como da prefeitura no PNCP, quando disponível.\n- Fontes, parâmetros de consulta, metodologia e limitações.\n- Arquivo PDF preparado para leitura e compartilhamento.")
    st.warning("O relatório é informativo e exploratório. Não é auditoria, parecer jurídico ou contábil, nem prova de irregularidade. Os resultados dependem da disponibilidade e da validação das fontes.")
    demo_pdf = criar_pdf(r["municipio"]["nome"], r["municipio"]["uf"], r["ano"], r["resumo"], r["nota"], r["pncp"], demonstracao=True)
    st.download_button("Baixar demonstração gratuita em PDF", data=demo_pdf, file_name=f"demonstracao_radar_{r['municipio']['id']}_{r['ano']}.pdf", mime="application/pdf", width="stretch")
    st.divider()
    st.markdown("#### Solicitar o relatório completo por R$ 29")
    st.write("Nesta primeira versão, o pedido é manual: você envia os dados do pedido, combina o pagamento e recebe o PDF depois da confirmação. O aplicativo ainda não processa pagamentos nem confirma Pix automaticamente.")
    with st.form("pedido_relatorio"):
        nome_cliente = st.text_input("Seu nome ou organização")
        email_cliente = st.text_input("E-mail para entrega")
        municipio_pedido = st.text_input("Município/UF do relatório", value=f"{r['municipio']['nome']} - {r['municipio']['uf']}")
        ano_pedido = st.selectbox("Exercício desejado", [2025, 2024, 2023, 2022], index=[2025, 2024, 2023, 2022].index(r["ano"]))
        observacoes = st.text_area("O que você gostaria de entender? (opcional)", max_chars=500)
        enviado = st.form_submit_button("Preparar solicitação — R$ 29", type="primary", width="stretch")
    if enviado:
        if not nome_cliente.strip() or not email_cliente.strip() or "@" not in email_cliente:
            st.error("Informe seu nome e um e-mail válido para preparar o pedido.")
        elif not municipio_pedido.strip():
            st.error("Informe o município e a UF.")
        else:
            pedido = f"SOLICITAÇÃO — RADAR DA TRANSPARÊNCIA MUNICIPAL\nProduto: Raio-X municipal (R$ 29,00)\nNome/organização: {nome_cliente.strip()}\nE-mail: {email_cliente.strip()}\nMunicípio/UF: {municipio_pedido.strip()}\nExercício: {ano_pedido}\nPergunta do cliente: {observacoes.strip() or 'Não informada'}\n\nStatus: solicitação preparada; pagamento ainda NÃO realizado nem confirmado."
            st.success("Solicitação preparada. Copie o texto abaixo e envie ao responsável pelo Radar para combinar pagamento e prazo.")
            st.code(pedido, language="text")
            st.download_button("Baixar solicitação em TXT", data=pedido.encode("utf-8"), file_name="solicitacao_radar.txt", mime="text/plain")
            contato = secret("CONTATO_VENDAS")
            link_pedidos = secret("LINK_PEDIDOS")
            if contato:
                st.info(f"Contato para combinar pagamento e entrega: {contato}")
            if link_pedidos.startswith("https://"):
                st.link_button("Enviar solicitação ao responsável", link_pedidos, width="stretch")
            else:
                st.caption("Antes de divulgar, configure LINK_PEDIDOS com um link https:// de WhatsApp ou formulário de contato em Settings → Secrets. Sem isso, o cliente precisará copiar ou baixar a solicitação e encaminhá-la manualmente.")
    if admin_ok:
        st.divider()
        st.markdown("#### Área privada do responsável — entrega manual")
        st.warning("Use esta área somente depois de confirmar o pagamento fora do aplicativo. Confira município e exercício do pedido antes de gerar o arquivo. O app não verifica Pix nem registra pedidos pagos.")
        pdf_pago = criar_pdf(r["municipio"]["nome"], r["municipio"]["uf"], r["ano"], r["resumo"], r["nota"], r["pncp"], demonstracao=False)
        st.download_button("Gerar PDF para entrega manual", data=pdf_pago, file_name=f"raio_x_{r['municipio']['id']}_{r['ano']}.pdf", mime="application/pdf", width="stretch")

st.divider()
st.caption("Protótipo inicial • Sem integração de pagamento • Consulte e valide as fontes oficiais • Versão de teste, não auditada.")
