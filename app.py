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


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_siconfi(ibge_id: str, ano: int) -> tuple[pd.DataFrame, str]:
    """Consulta RREO comum e, se vazio, RREO Simplificado do mesmo período."""
    import time

    base = {
        "an_exercicio": ano,
        "nr_periodo": 6,
        "no_anexo": "RREO-Anexo 02",
        "id_ente": ibge_id,
    }
    limite = 5000
    max_paginas = 10
    total_chamadas = 0
    for demonstrativo in ("RREO", "RREO Simplificado"):
        todos = []
        completo = False
        for pagina in range(max_paginas):
            # API STN limita a uma requisicao por segundo.
            if total_chamadas:
                time.sleep(1.1)
            total_chamadas += 1
            data = get_json(
                SICONFI_URL,
                {
                    **base,
                    "co_tipo_demonstrativo": demonstrativo,
                    "limit": limite,
                    "offset": pagina * limite,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return pd.DataFrame(), (
                    f"Falha de consulta no SICONFI ({demonstrativo}). "
                    "Não é possível confirmar disponibilidade; nenhum número foi estimado."
                )
            itens = data["items"]
            todos.extend(itens)
            if not data.get("hasMore", False):
                completo = True
                break
            if not itens:
                break
        if not completo:
            return pd.DataFrame(), (
                f"Paginação incompleta no SICONFI ({demonstrativo}); "
                "nenhum resumo calculado com registros parciais."
            )
        if not todos:
            continue
        frame = pd.DataFrame(todos)
        if not {"coluna", "conta", "valor"}.issubset(frame.columns):
            return pd.DataFrame(), (
                f"O demonstrativo {demonstrativo} não retornou os campos "
                "necessários para calcular as despesas."
            )
        return frame, (
            f"Fonte: SICONFI — {demonstrativo}, RREO Anexo 02, "
            f"exercício {ano}, 6º bimestre; {len(frame)} linhas recebidas. "
            + (
                "A consulta padrão veio vazia; foi utilizada a modalidade "
                "simplificada para o mesmo exercício e período. "
                if demonstrativo == "RREO Simplificado" else ""
            )
            + "Valores declarados no demonstrativo, sujeitos à validação na fonte."
        )

    return pd.DataFrame(), (
        f"O SICONFI não retornou dados do Anexo 02 de {ano}, 6º bimestre, "
        "nem para RREO comum nem para RREO Simplificado. "
        "Isso não comprova ausência de despesas ou omissão na prestação de contas. "
        "Consulte o Extrato de Entregas e os relatórios homologados no SICONFI."
    )


def processar_resumo(df: pd.DataFrame) -> tuple[pd.DataFrame, float | None, str]:
    """Seleciona somente funcoes do Anexo 02; nao soma subfuncoes nem totais."""
    import unicodedata

    def norm(valor: object) -> str:
        return " ".join(
            unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore")
            .decode("ascii").upper().split()
        )

    if df.empty:
        return pd.DataFrame(), None, ""
    required = {"conta", "coluna", "valor"}
    if not required.issubset(df.columns):
        return pd.DataFrame(), None, "Faltam os campos necessários no demonstrativo do SICONFI."

    funcoes = {
        "LEGISLATIVA", "JUDICIARIA", "ESSENCIAL A JUSTICA", "ADMINISTRACAO",
        "DEFESA NACIONAL", "SEGURANCA PUBLICA", "RELACOES EXTERIORES",
        "ASSISTENCIA SOCIAL", "PREVIDENCIA SOCIAL", "SAUDE", "TRABALHO",
        "EDUCACAO", "CULTURA", "DIREITOS DA CIDADANIA", "URBANISMO",
        "HABITACAO", "SANEAMENTO", "GESTAO AMBIENTAL", "CIENCIA E TECNOLOGIA",
        "AGRICULTURA", "ORGANIZACAO AGRARIA", "INDUSTRIA",
        "COMERCIO E SERVICOS", "COMUNICACOES", "ENERGIA", "TRANSPORTE",
        "DESPORTO E LAZER", "ENCARGOS ESPECIAIS", "RESERVA DE CONTINGENCIA",
    }
    dados = df.copy()
    colunas = dados["coluna"].map(norm)
    dados = dados[
        colunas.str.contains("DESPESAS LIQUIDADAS", na=False)
        & colunas.str.contains("ATE O BIMESTRE", na=False)
    ].copy()
    if dados.empty:
        return pd.DataFrame(), None, "A coluna de despesas liquidadas até o bimestre não foi encontrada."
    dados["_conta_norm"] = dados["conta"].map(norm)
    dados["valor"] = pd.to_numeric(dados["valor"], errors="coerce")
    dados = dados.dropna(subset=["valor"])
    # O nome exato da função é uma classificação contábil conhecida.
    # Subfunções, códigos FUxx, totais e intra-orçamentárias não entram.
    linhas = dados[dados["_conta_norm"].isin(funcoes)].copy()
    if linhas.empty:
        return pd.DataFrame(), None, (
            "Não foi possível identificar linhas exclusivas de função no Anexo 02. "
            "Subfunções e totais não foram somados para evitar dupla contagem."
        )
    # Uma única função pode aparecer em diferentes quadros ou classificações.
    # Se houver valores conflitantes, omitir essa função até validar a fonte.
    grupos = []
    conflitos = []
    for conta_norm, grupo in linhas.groupby("_conta_norm"):
        valores = grupo["valor"].round(2).unique()
        if len(valores) != 1:
            conflitos.append(conta_norm)
            continue
        grupos.append({"conta": str(grupo.iloc[0]["conta"]), "valor": float(valores[0])})
    if not grupos:
        return pd.DataFrame(), None, "As linhas das funções apresentam valores conflitantes; nenhum total foi calculado."
    resumo = pd.DataFrame(grupos).sort_values("valor", ascending=False).reset_index(drop=True)
    # Referência oficial: total de despesas EXCETO intra-orçamentárias (I).
    # Não calcular o percentual em relação à soma arbitrária das linhas.
    denominador = dados[
        dados["_conta_norm"].str.startswith("DESPESAS (EXCETO INTRA-ORCAMENTARIAS)")
    ]["valor"].round(2).unique()
    total = float(denominador[0]) if len(denominador) == 1 and denominador[0] > 0 else None
    if total is not None:
        resumo["percentual"] = (resumo["valor"] / total * 100).round(2)
    else:
        resumo["percentual"] = float("nan")
    nota = (
        "RREO Anexo 02: despesas LIQUIDADAS até o 6º bimestre. Exibidas apenas "
        "funções orçamentárias (Saúde, Educação etc.), sem somar subfunções ou "
        "totais, evitando a dupla contagem. Os valores não representam compras "
        "individualizadas, fornecedores ou pagamentos efetivos. "
    )
    if total is None:
        nota += "O total oficial de referência (I) não pôde ser validado; percentuais omitidos. "
    else:
        nota += "Percentuais calculados sobre DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (I). "
    if conflitos:
        nota += f"{len(conflitos)} função(ões) com dados conflitantes foram omitidas. "
    return resumo, total, nota


@st.cache_data(ttl=1800, show_spinner=False)
def carregar_pagina_pncp(data_inicial: str, data_final: str, codigo: int, ibge_id: str, pagina: int) -> tuple[list[dict[str, Any]] | None, int, int | None]:
    """Cache de 30 min, inclusive para 429: evita repetir chamadas em cada rerun."""
    params = {
        "dataInicial": data_inicial, "dataFinal": data_final,
        "codigoModalidadeContratacao": codigo,
        "codigoMunicipioIbge": ibge_id,
        "pagina": pagina, "tamanhoPagina": 50,
    }
    try:
        resposta = requests.get(
            PNCP_URL, params=params, timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "RadarTransparencia/0.6"},
        )
        if resposta.status_code == 204:
            return [], 204, 0
        if resposta.status_code != 200:
            return None, resposta.status_code, None
        dados = resposta.json()
        itens = dados.get("data") if isinstance(dados, dict) else None
        if not isinstance(itens, list):
            return None, 0, None
        total_paginas = dados.get("totalPaginas")
        try:
            total_paginas = int(total_paginas) if total_paginas is not None else None
        except (ValueError, TypeError):
            total_paginas = None
        return itens, 200, total_paginas
    except (requests.RequestException, ValueError) as exc:
        print(f"Falha consulta PNCP: {type(exc).__name__}: {exc}")
        return None, 0, None


def buscar_pncp(ibge_id: str, nome_municipio: str, ano: int, mes: int, periodo: str, escopo: str = "Prefeitura / Administração Direta") -> tuple[pd.DataFrame, str, pd.DataFrame]:
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
    if periodo == "Ano completo":
        mes_inicial, mes_final = 1, 12
    elif periodo == "Trimestre":
        mes_inicial = ((mes - 1) // 3) * 3 + 1
        mes_final = mes_inicial + 2
    else:
        mes_inicial = mes_final = mes
    data_inicial = f"{ano}{mes_inicial:02d}01"
    data_final = f"{ano}{mes_final:02d}{calendar.monthrange(ano, mes_final)[1]:02d}"
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
    bloqueado = False

    for codigo, modalidade in modalidades.items():
        if bloqueado:
            break
        for pagina in range(1, max_paginas_por_modalidade + 1):
            params = {
                "dataInicial": data_inicial,
                "dataFinal": data_final,
                "codigoModalidadeContratacao": codigo,
                "codigoMunicipioIbge": ibge_id,
                "pagina": pagina,
                "tamanhoPagina": por_pagina,
            }
            itens, http, total_paginas = carregar_pagina_pncp(
                data_inicial, data_final, codigo, ibge_id, pagina
            )
            if http == 204:
                consultas_validas += 1
                paginas_consultadas += 1
                break
            if itens is None:
                falhas.append(
                    f"{modalidade}, página {pagina}: "
                    + (f"HTTP {http}" if http else "falha de rede ou formato inesperado")
                )
                if http == 429:
                    bloqueado = True
                break
            consultas_validas += 1
            paginas_consultadas += 1
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
                    "Objeto resumido": resumir_objeto(item.get("objetoCompra")),
                    "Modalidade": item.get("modalidadeNome") or modalidade,
                    "Valor estimado (R$)": pd.to_numeric(item.get("valorTotalEstimado"), errors="coerce"),
                    "Data de publicação": str(item.get("dataPublicacaoPncp") or "")[:10],
                    "Identificador PNCP": item.get("numeroControlePNCP") or "Não informado",
                })

            if isinstance(total_paginas, int) and pagina >= total_paginas:
                break
            if len(itens) == 0 or (total_paginas is None and len(itens) < por_pagina):
                break
            if pagina == max_paginas_por_modalidade:
                truncado = True

    descricao = (
        f"Escopo: {escopo}. Período consultado: {mes_inicial:02d}/{ano} a {mes_final:02d}/{ano} ({periodo}), pregão eletrônico "
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
    if bloqueado:
        descricao += "O PNCP limitou requisições (HTTP 429); a consulta foi interrompida e novas tentativas automáticas foram evitadas. Resultados incompletos. "
    diagnostico_df = pd.DataFrame(diagnostico).drop_duplicates() if diagnostico else pd.DataFrame()
    if consultas_validas == 0:
        return pd.DataFrame(), "O PNCP não respondeu às consultas; não conclua ausência de contratos. " + descricao, diagnostico_df
    if not linhas:
        return pd.DataFrame(), "Nenhum registro correspondeu ao escopo na amostra consultada; isso não significa ausência de contratações. " + descricao, diagnostico_df
    resultado = pd.DataFrame(linhas)
    return resultado.drop_duplicates(subset=["Identificador PNCP"]), descricao, diagnostico_df


def resumir_objeto(texto: object, limite: int = 160) -> str:
    """Resumo extrativo: retira prefácios padronizados, preservando objeto original."""
    import re
    original = " ".join(str(texto or "").split())
    if not original or original == "Descrição não informada":
        return "Objeto não informado"
    original = re.sub(r"^\[Portal de Compras Públicas\]\s*[-–:]?\s*", "", original, flags=re.I)
    original = re.sub(r"^o objeto da presente licita[çc][ãa]o [ée]\s+", "", original, flags=re.I)
    original = re.sub(
        r"^(?:a escolha mais vantajosa para|a sele[çc][ãa]o da proposta mais vantajosa para)\s+",
        "", original, flags=re.I,
    )
    original = re.sub(r"^(?:o|a)\s+(?=(?:contrata[çc][ãa]o|aquisi[çc][ãa]o))", "", original, flags=re.I)
    original = original.lstrip(" -:;,.")
    if not original:
        return "Consulte o objeto original no PNCP"
    if len(original) <= limite:
        return original[0].upper() + original[1:]
    trecho = original[:limite].rsplit(" ", 1)[0]
    return (trecho or original[:limite]).rstrip(" ,;:") + "…"


@st.cache_data(ttl=21600, show_spinner=False)
def consultar_fornecedores_resultados(identificador: str) -> tuple[pd.DataFrame, str]:
    """Consulta resultados registrados de ate cinco itens de uma licitacao PNCP."""
    import re
    import time

    partes = re.fullmatch(r"(\d{14})-(\d+)-(\d+)/(\d{4})", str(identificador).strip())
    if not partes:
        return pd.DataFrame(), "Identificador PNCP inválido ou não reconhecido."
    cnpj, _tipo, sequencial, ano = partes.groups()
    base = f"https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{int(ano)}/{int(sequencial)}/itens"
    try:
        resposta = requests.get(
            base, params={"pagina": 1, "tamanhoPagina": 10},
            headers={"Accept": "application/json", "User-Agent": "RadarTransparencia/0.5"},
            timeout=REQUEST_TIMEOUT,
        )
        if resposta.status_code == 204:
            return pd.DataFrame(), "Não há itens registrados no PNCP para esta contratação."
        resposta.raise_for_status()
        dados = resposta.json()
    except (requests.RequestException, ValueError) as exc:
        return pd.DataFrame(), f"Não foi possível consultar os itens: {type(exc).__name__}."

    if isinstance(dados, list):
        itens = dados
    elif isinstance(dados, dict):
        itens = dados.get("data") or dados.get("items") or []
    else:
        itens = []
    if not isinstance(itens, list) or not itens:
        return pd.DataFrame(), "A API não retornou itens consultáveis para esta contratação."

    registros = []
    falhas = []
    itens_consultados = 0
    for item in itens[:5]:
        if not isinstance(item, dict):
            continue
        numero = item.get("numeroItem")
        if not str(numero or "").isdigit():
            continue
        itens_consultados += 1
        time.sleep(0.25)
        try:
            res = requests.get(
                f"{base}/{int(numero)}/resultados",
                headers={"Accept": "application/json", "User-Agent": "RadarTransparencia/0.5"},
                timeout=REQUEST_TIMEOUT,
            )
            if res.status_code == 204:
                continue
            res.raise_for_status()
            valores = res.json()
        except (requests.RequestException, ValueError) as exc:
            falhas.append(f"Item {numero}: {type(exc).__name__}")
            continue
        if isinstance(valores, dict):
            valores = valores.get("data") or valores.get("items") or []
        if not isinstance(valores, list):
            continue
        for resultado in valores:
            if not isinstance(resultado, dict):
                continue
            fornecedor = resultado.get("nomeRazaoSocialFornecedor")
            if not fornecedor:
                continue
            registros.append({
                "Item": int(numero),
                "Fornecedor informado no resultado": str(fornecedor),
                "CPF/CNPJ informado": str(resultado.get("niFornecedor") or "Não informado"),
                "Valor total homologado (R$)": pd.to_numeric(
                    resultado.get("valorTotalHomologado"), errors="coerce"
                ),
                "Data do resultado": str(resultado.get("dataResultado") or ""),
                "Ordem de classificação": resultado.get("ordemClassificacaoSrp") or "",
                "Situação": str(resultado.get("situacaoCompraItemResultadoNome") or
                                resultado.get("situacaoCompraItemResultadoId") or "Não informada"),
            })
    nota = (
        f"Consultados {itens_consultados} de {len(itens)} item(ns) da primeira página, "
        "limitados a cinco para evitar excesso de requisições. Os resultados "
        "podem informar diferentes fornecedores por item e classificação; "
        "não significam necessariamente contrato assinado ou pagamento efetuado."
    )
    if len(itens) > 5:
        nota += " Há itens adicionais não verificados."
    if falhas:
        nota += " Falhas: " + "; ".join(falhas)
    if not registros:
        return pd.DataFrame(), "Nenhum fornecedor foi identificado nos resultados consultados. " + nota
    return pd.DataFrame(registros).drop_duplicates(), nota


def moeda(valor: float) -> str:
    return (f"R$ {valor:,.2f}").replace(",", "X").replace(".", ",").replace("X", ".")


def criar_pdf(
    nome: str, uf: str, ano: int, resumo: pd.DataFrame, nota: str,
    pncp: pd.DataFrame | None = None, demonstracao: bool = True,
    contexto_pncp: str = "",
    resumo_anterior: pd.DataFrame | None = None,
    nota_anterior: str = "",
) -> bytes:
    """PDF informativo: funções orçamentárias separadas de contratações PNCP."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=17*mm, leftMargin=17*mm,
        topMargin=16*mm, bottomMargin=16*mm,
    )
    estilos = getSampleStyleSheet()
    titulo = ParagraphStyle(
        "RadarTitulo", parent=estilos["Title"], fontSize=15, leading=19,
        textColor=colors.HexColor("#183153"), spaceAfter=10,
    )
    corpo = ParagraphStyle(
        "RadarCorpo", parent=estilos["BodyText"], fontSize=8.5,
        leading=12.5, spaceAfter=7, wordWrap="CJK",
    )
    pequeno = ParagraphStyle(
        "RadarPequeno", parent=corpo, fontSize=7.5, leading=10.5,
    )
    cabecalho = ParagraphStyle(
        "RadarCabecalho", parent=pequeno, textColor=colors.white,
        fontName="Helvetica-Bold",
    )
    celula = ParagraphStyle("RadarCelula", parent=pequeno, leading=10)

    def par(texto: object, estilo=celula):
        return Paragraph(html.escape(str(texto)), estilo)

    def tabela(linhas: list[list[object]], larguras: list[float]):
        t = Table(linhas, colWidths=larguras, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#22476E")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#CFD8E3")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    story = []
    if demonstracao:
        story.append(Paragraph(
            "AMOSTRA GRATUITA — DADOS PÚBLICOS, NÃO É AUDITORIA",
            ParagraphStyle("RadarDemo", parent=estilos["Heading3"], textColor=colors.HexColor("#A13B28")),
        ))
    story.append(Paragraph(
        f"Radar da Transparência — {html.escape(nome)} ({html.escape(uf)}) — {ano}", titulo,
    ))
    story.append(Paragraph(
        f"Emissão: {datetime.now().strftime('%d/%m/%Y %H:%M')}. "
        "O levantamento depende das fontes oficiais e das opções selecionadas.", corpo,
    ))
    story.append(Paragraph(
        "O relatório distingue DESPESAS LIQUIDADAS (SICONFI) de VALORES ESTIMADOS "
        "DE CONTRATAÇÕES (PNCP). Uma contratação não comprova pagamento, "
        "e as duas fontes não devem ter seus valores somados.", corpo,
    ))
    story.append(Spacer(1, 5))
    story.append(Paragraph("1. Despesas por área orçamentária — SICONFI", estilos["Heading2"]))
    story.append(Paragraph(
        "RREO, Anexo 02, sexto bimestre. São mostradas exclusivamente "
        "funções orçamentárias (por exemplo, Saúde e Educação), sem subfunções "
        "sobrepostas. Isso indica a área da despesa liquidada, não o fornecedor "
        "nem a compra individual.", corpo,
    ))
    if resumo.empty:
        story.append(Paragraph(
            "Não houve dados seguros suficientes para um quadro por função.", corpo,
        ))
    else:
        linhas = [[par("Função / área", cabecalho),
                   par("Liquidado até o bimestre", cabecalho),
                   par("% do total (I)", cabecalho)]]
        limite = 5 if demonstracao else 28
        for _, linha in resumo.head(limite).iterrows():
            pct = linha.get("percentual")
            texto_pct = f"{float(pct):.1f}%" if pd.notna(pct) else "Não disponível"
            linhas.append([par(linha["conta"]), par(moeda(float(linha["valor"]))), par(texto_pct)])
        story.append(tabela(linhas, [74*mm, 60*mm, 38*mm]))
        if demonstracao and len(resumo) > limite:
            story.append(Paragraph(
                f"Amostra com {limite} de {len(resumo)} funções identificadas. "
                "O relatório completo apresenta as demais linhas válidas.", pequeno,
            ))
        if not demonstracao:
            story.append(Spacer(1, 7))
            story.append(Paragraph("Leitura orientada das despesas", estilos["Heading3"]))
            top3 = resumo.head(3)
            top3_nomes = ", ".join(str(v) for v in top3["conta"].tolist())
            if len(top3) == 3 and top3["percentual"].notna().all():
                participacao = float(top3["percentual"].sum())
                story.append(Paragraph(
                    f"Concentração: {html.escape(top3_nomes)} respondem por "
                    f"{participacao:.1f}% do total de referência (I) de despesas "
                    "liquidadas neste demonstrativo. A concentração mostra a "
                    "distribuição por função, não comprova qualidade dos serviços "
                    "nem irregularidades.", corpo,
                ))
            else:
                story.append(Paragraph(
                    "Concentração percentual indisponível: não foi possível "
                    "validar os percentuais de três funções distintas.", corpo,
                ))
            if len(resumo) > 1:
                maior = resumo.iloc[0]
                segunda = resumo.iloc[1]
                diferenca = float(maior["valor"]) - float(segunda["valor"])
                story.append(Paragraph(
                    f"Comparação: {html.escape(str(maior['conta']))} registra "
                    f"{moeda(float(maior['valor']))}, cerca de {moeda(diferenca)} "
                    f"a mais do que {html.escape(str(segunda['conta']))}. "
                    "A diferença não permite concluir se houve insuficiência "
                    "ou excesso de recursos em qualquer área.", corpo,
                ))
            story.append(Paragraph(
                "Para aprofundar: comparar os mesmos indicadores com outros "
                "exercícios, metas dos programas e entregas dos serviços públicos. "
                "Os dados atuais mostram a função orçamentária, mas não a "
                "execução física das políticas nem os pagamentos por fornecedor.", corpo,
            ))


    if not demonstracao:
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            f"Comparação de despesas: {ano - 1} e {ano}",
            estilos["Heading2"],
        ))
        anterior = resumo_anterior if resumo_anterior is not None else pd.DataFrame()
        if not anterior.empty and not resumo.empty:
            anteriores = {
                str(row["conta"]).strip().casefold(): float(row["valor"])
                for _, row in anterior.iterrows()
            }
            comparativos = [
                [par("Função", cabecalho), par(str(ano - 1), cabecalho),
                 par(str(ano), cabecalho), par("Variação", cabecalho)]
            ]
            linhas_comparaveis = []
            for _, atual in resumo.iterrows():
                nome = str(atual["conta"])
                valor_anterior = anteriores.get(nome.strip().casefold())
                if valor_anterior is None:
                    continue
                valor_atual = float(atual["valor"])
                variacao_pct = (
                    (valor_atual / valor_anterior - 1) * 100
                    if valor_anterior > 0 else None
                )
                linhas_comparaveis.append({
                    "nome": nome, "anterior": valor_anterior,
                    "atual": valor_atual, "variacao": variacao_pct,
                })
            seis = linhas_comparaveis[:6]
            adicionais = [
                item for item in linhas_comparaveis[6:]
                if item["variacao"] is not None and item["variacao"] > 100
            ]
            destaque = max(adicionais, key=lambda item: item["variacao"]) if adicionais else None
            selecionadas = seis + ([destaque] if destaque is not None else [])
            for item in selecionadas:
                variacao = (
                    f"{item['variacao']:+.1f}%"
                    if item["variacao"] is not None else "N/D"
                )
                nome = item["nome"] + (" (destaque >100%)" if item is destaque else "")
                comparativos.append([
                    par(nome), par(moeda(item["anterior"])),
                    par(moeda(item["atual"])), par(variacao),
                ])
            if len(comparativos) > 1:
                story.append(tabela(comparativos, [57*mm, 44*mm, 44*mm, 27*mm]))
                story.append(Paragraph(
                    "Seis maiores funções comparáveis no ano atual, mais o "
                    "maior crescimento acima de 100% fora desse grupo, se houver. "
                    "Variações NOMINAIS, sem correção da inflação, no mesmo "
                    "6º bimestre. Base pequena pode produzir variação alta. "
                    "Mudanças não comprovam eficiência ou irregularidades.",
                    pequeno,
                ))
            else:
                story.append(Paragraph(
                    "Nenhuma função foi encontrada em ambos os exercícios. "
                    "Não foi calculada variação.", corpo,
                ))
        else:
            story.append(Paragraph(
                f"Dados de {ano - 1} indisponíveis ou insuficientes para "
                "comparar funções orçamentárias. Nenhum valor foi estimado.", corpo,
            ))
        if nota_anterior:
            story.append(Paragraph(
                "Consulta do exercício anterior: " + html.escape(nota_anterior), pequeno,
            ))

    story.append(Spacer(1, 8))
    story.append(Paragraph("2. Contratações publicadas — PNCP", estilos["Heading2"]))
    story.append(Paragraph(
        "O PNCP apresenta o OBJETO da contratação e o VALOR TOTAL ESTIMADO, "
        "que não correspondem necessariamente às despesas já pagas ou liquidadas. "
        "A consulta é limitada às modalidades, filtros e páginas indicados.", corpo,
    ))
    if contexto_pncp:
        if "HTTP 429" in contexto_pncp or "Falhas:" in contexto_pncp:
            story.append(Paragraph(
                "ATENÇÃO: a consulta do PNCP apresentou falhas ou limitação "
                "de requisições. Os resultados são parciais e não devem ser "
                "interpretados como total de contratações.", pequeno,
            ))
        story.append(Paragraph(html.escape(contexto_pncp), pequeno))
    if pncp is None or pncp.empty:
        story.append(Paragraph(
            "Não há contratações disponíveis na amostra consultada. Isso não "
            "comprova inexistência de contratações.", corpo,
        ))
    else:
        story.append(Paragraph(
            f"{len(pncp)} registros retornados na amostra selecionada. "
            "Exemplos abaixo, sem representar a totalidade das compras.", corpo,
        ))
        linhas = [[par("Unidade / órgão", cabecalho),
                   par("Objeto em linguagem direta*", cabecalho),
                   par("Valor estimado", cabecalho)]]
        limite = 2 if demonstracao else 12
        for _, linha in pncp.head(limite).iterrows():
            unidade = str(linha.get("Unidade administrativa") or linha.get("Órgão") or "Não informada")
            objeto = str(linha.get("Objeto resumido") or resumir_objeto(linha.get("Objeto"), 160))
            valor = linha.get("Valor estimado (R$)")
            valor_texto = moeda(float(valor)) if pd.notna(valor) else "Não informado"
            linhas.append([
                par(unidade[:90]),
                par(objeto[:180] + ("…" if len(objeto) > 180 else "")),
                par(valor_texto),
            ])
        story.append(tabela(linhas, [53*mm, 79*mm, 40*mm]))
        story.append(Paragraph("*Resumo automático do texto publicado; consulte o objeto integral pelo identificador PNCP.", pequeno))
        if not demonstracao:
            story.append(Spacer(1, 7))
            story.append(Paragraph("Leitura orientada das contratações", estilos["Heading3"]))
            valores = pd.to_numeric(pncp["Valor estimado (R$)"], errors="coerce")
            qtd_zeros = int((valores == 0).sum())
            qtd_sem_valor = int(valores.isna().sum())
            qtd_positivos = int((valores > 0).sum())
            unidades = pncp.get("Unidade administrativa")
            if unidades is not None:
                unidades_validas = unidades.fillna("Não informada").astype(str)
                if not unidades_validas.empty:
                    mais_frequente = unidades_validas.value_counts().index[0]
                    frequencia = int(unidades_validas.value_counts().iloc[0])
                    story.append(Paragraph(
                        f"Unidade mais frequente na amostra: "
                        f"{html.escape(str(mais_frequente))}, com {frequencia} "
                        f"dos {len(pncp)} registros retornados. Isso mede apenas "
                        "a frequência de publicações na amostra, não o volume "
                        "de pagamentos dessa unidade.", corpo,
                    ))
            if qtd_positivos:
                indices_maiores = valores[valores > 0].nlargest(3).index
                destaques = []
                for indice in indices_maiores:
                    item = pncp.loc[indice]
                    identificador = str(item.get("Identificador PNCP") or "não informado")
                    destaque = (
                        f"{moeda(float(valores.loc[indice]))} "
                        f"(PNCP {identificador})"
                    )
                    destaques.append(destaque)
                story.append(Paragraph(
                    "Maiores valores estimados entre os registros retornados: "
                    + html.escape("; ".join(destaques))
                    + ". São estimativas de contratações publicadas, "
                    "não pagamentos comprovados ou ranking completo do ano.",
                    corpo,
                ))
            if qtd_zeros or qtd_sem_valor:
                story.append(Paragraph(
                    f"Qualidade dos dados: {qtd_zeros} registro(s) com valor "
                    f"estimado zero e {qtd_sem_valor} sem valor numérico. "
                    "É necessário conferir os respectivos registros oficiais "
                    "antes de interpretar esses valores; zero não comprova "
                    "contratação gratuita.", corpo,
                ))
            if "HTTP 429" in contexto_pncp or "Falhas:" in contexto_pncp:
                story.append(Paragraph(
                    "Limite crítico desta análise: a API não entregou todas as "
                    "páginas solicitadas. Não é possível calcular com segurança "
                    "o total anual de contratações, nem compará-lo à despesa "
                    "liquidada do SICONFI.", corpo,
                ))
            story.append(Paragraph(
                "Próximas verificações recomendadas: abrir os identificadores "
                "PNCP destacados, conferir os editais, eventuais contratos "
                "assinados, fornecedores, aditivos e dados de pagamentos no "
                "portal de transparência do município.", corpo,
            ))


    story.append(Spacer(1, 5))
    story.append(Paragraph("3. Fontes e limites da consulta", estilos["Heading2"]))
    story.append(Paragraph(html.escape(nota), pequeno))
    story.append(Paragraph(
        "Despesas por função (RREO Anexo 02), sem soma de subfunções. "
        "Percentuais sobre o total oficial (I), quando disponível. "
        "Não identificam destinatários de pagamentos. "
        "Fontes: https://siconfi.tesouro.gov.br/ e https://pncp.gov.br/ . "
        "Relatório informativo; não comprova irregularidades.", pequeno,
    ))
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
    periodo_pncp = st.selectbox("Período das publicações no PNCP", ["Ano completo", "Trimestre", "Mês"], index=0)
    mes_pncp = st.selectbox("Mês de referência (usado em Mês e Trimestre)", list(range(1, 13)), index=8, format_func=lambda m: ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"][m - 1])
    escopo_pncp = st.radio(
        "Contratações incluídas",
        ["Prefeitura / Administração Direta", "Administração Municipal Ampliada"],
        help="A primeira opção inclui prefeitura e secretarias municipais identificadas. A ampliada inclui também outras entidades do Executivo municipal, como previdência e fundos.",
    )
    consultar = st.button("Consultar contratações PNCP", type="primary", width="stretch")
    st.caption("As despesas são carregadas normalmente. O PNCP só é consultado quando você clica no botão, para reduzir erros HTTP 429.")
    st.caption("A cobertura depende do exercício e da disponibilidade de cada fonte.")
    chave_admin_configurada = secret("CHAVE_ADMIN_RELATORIOS")
    chave_admin_digitada = st.text_input("Acesso do responsável (opcional)", type="password", help="Use somente se você configurou CHAVE_ADMIN_RELATORIOS nos Secrets.")
    admin_ok = bool(chave_admin_configurada) and hmac.compare_digest(chave_admin_digitada, chave_admin_configurada)
    if admin_ok:
        st.success("Modo de entrega manual habilitado")

key = f"pncp_cache_v9_{opcoes[cidade_label]['id']}_{ano}_{periodo_pncp}_{mes_pncp}_{escopo_pncp}"
municipio = opcoes[cidade_label]
if key not in st.session_state:
    raw, nota_siconfi = carregar_siconfi(municipio["id"], ano)
    resumo, total, nota_resumo = processar_resumo(raw)
    st.session_state[key] = {
        "municipio": municipio, "ano": ano, "raw": raw,
        "resumo": resumo, "total": total,
        "nota": f"{nota_siconfi} {nota_resumo}",
        "pncp": pd.DataFrame(),
        "nota_pncp": "Consulta PNCP ainda não iniciada. Clique em 'Consultar contratações PNCP' na barra lateral. A ausência de linhas antes da consulta não indica ausência de contratos.",
        "mes_pncp": mes_pncp, "periodo_pncp": periodo_pncp,
        "diagnostico_pncp": pd.DataFrame(), "pncp_consultado": False,
    }
if consultar:
    with st.spinner("Consultando PNCP com cache para reduzir bloqueios..."):
        pncp, nota_pncp, diagnostico_pncp = buscar_pncp(
            municipio["id"], municipio["nome"], ano, mes_pncp, periodo_pncp, escopo_pncp
        )
        anterior = st.session_state[key]
        falhou_429 = "HTTP 429" in nota_pncp
        if falhou_429 and pncp.empty and not anterior["pncp"].empty:
            nota_pncp = (
                "O PNCP respondeu HTTP 429. Exibindo a última consulta desta sessão "
                "(não atualizada); os resultados continuam parciais. " + anterior["nota_pncp"]
            )
            pncp = anterior["pncp"]
            diagnostico_pncp = anterior["diagnostico_pncp"]
        anterior.update({
            "pncp": pncp, "nota_pncp": nota_pncp,
            "diagnostico_pncp": diagnostico_pncp, "pncp_consultado": True,
        })

r = st.session_state[key]

st.header(f"{r['municipio']['nome']} — {r['municipio']['uf']} | {r['ano']}")
st.caption("Fontes: SICONFI/Tesouro Nacional e Portal Nacional de Contratações Públicas (PNCP), conforme disponibilidade.")
tab1, tab2, tab3 = st.tabs(["📊 Orçamento", "📑 Contratações", "📄 Relatório de R$ 29"])

with tab1:
    st.subheader("Onde foram liquidadas as despesas — por função orçamentária")
    if r["resumo"].empty:
        st.warning(r["nota"] or "Não há informações financeiras suficientes para o resumo nesta consulta.")
    else:
        st.info(r["nota"])
    if not r["resumo"].empty:
        c1, c2 = st.columns(2)
        c1.metric("Maior função exibida", moeda(float(r["resumo"]["valor"].max())))
        c2.metric("Funções identificadas", len(r["resumo"]))
        st.caption("Valores por função, sem somar subfunções. Referência: despesas liquidadas até o 6º bimestre.")
        st.plotly_chart(px.bar(r["resumo"].head(12).sort_values("valor"), x="valor", y="conta", orientation="h", title="Áreas orçamentárias com maiores despesas liquidadas"), width="stretch")
        st.dataframe(r["resumo"], width="stretch", hide_index=True)
        st.download_button("Baixar tabela exibida (CSV)", r["resumo"].to_csv(index=False).encode("utf-8-sig"), file_name=f"radar_{r['municipio']['id']}_{r['ano']}.csv", mime="text/csv")
    else:
        st.caption("Nenhum valor foi estimado artificialmente. Consulte outros exercícios e o extrato oficial de entregas para investigar a disponibilidade.")
    st.link_button("Consultar SICONFI oficial", "https://siconfi.tesouro.gov.br/")

with tab2:
    st.subheader(f"Contratações no PNCP — {escopo_pncp}")
    st.info(r["nota_pncp"])
    st.caption("Objeto resumido remove apenas fórmulas repetitivas do texto do edital; consulte a coluna Objeto para a redação integral.")
    st.caption("Classificação dos órgãos refeita na versão v5. A coluna Unidade administrativa permite conferir secretarias e fundos separadamente.")
    if not r["pncp"].empty:
        st.dataframe(r["pncp"], width="stretch", hide_index=True)
        st.download_button("Baixar registros desta página (CSV)", r["pncp"].to_csv(index=False).encode("utf-8-sig"), file_name=f"pncp_{r['municipio']['id']}_{r['ano']}.csv", mime="text/csv")
    else:
        if r.get("pncp_consultado"):
            st.warning("Não há registros exibíveis nesta consulta. Se houve HTTP 429, tente mais tarde; não se conclui ausência de contratação.")
        else:
            st.caption("Consulta ainda não executada. Use o botão na barra lateral.")
    if not r["pncp"].empty:
        with st.expander("Consultar fornecedores informados nos resultados dos itens"):
            st.caption(
                "Consulta sob demanda na API oficial do PNCP. Um edital pode ter "
                "vários vencedores por item; fornecedor do resultado não comprova "
                "pagamento nem contrato assinado. São consultados até cinco itens."
            )
            identificadores = r["pncp"]["Identificador PNCP"].dropna().astype(str).unique().tolist()
            escolhido = st.selectbox(
                "Contratação PNCP", identificadores,
                format_func=lambda x: x,
                key="selecionar_resultado_pncp",
            )
            if st.button("Consultar fornecedores desta contratação", key="buscar_fornecedor_pncp"):
                with st.spinner("Consultando resultados oficiais por item..."):
                    fornecedores, nota_fornecedores = consultar_fornecedores_resultados(escolhido)
                st.session_state["resultado_fornecedor_pncp"] = {
                    "identificador": escolhido,
                    "dados": fornecedores,
                    "nota": nota_fornecedores,
                }
            resultado_fornecedor = st.session_state.get("resultado_fornecedor_pncp")
            if resultado_fornecedor and resultado_fornecedor["identificador"] == escolhido:
                st.info(resultado_fornecedor["nota"])
                if not resultado_fornecedor["dados"].empty:
                    st.dataframe(resultado_fornecedor["dados"], width="stretch", hide_index=True)
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
    st.markdown("- Despesas liquidadas por função orçamentária, sem dupla contagem entre funções e subfunções.\n- Percentuais sobre o total oficial de referência, quando disponível.\n- Amostra de objetos de contratações e valores estimados, separados das despesas do SICONFI.\n- Comparação das despesas por função com o ano anterior, quando houver dados.\n- Fontes, metodologia, escopo e limitações da consulta.\n- Arquivo PDF para leitura e compartilhamento.")
    st.warning("O relatório é informativo e exploratório. Não é auditoria, parecer jurídico ou contábil, nem prova de irregularidade. Os resultados dependem da disponibilidade e da validação das fontes.")
    demo_pdf = criar_pdf(r["municipio"]["nome"], r["municipio"]["uf"], r["ano"], r["resumo"], r["nota"], r["pncp"], demonstracao=True, contexto_pncp=r["nota_pncp"])
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
        with st.spinner("Consultando o ano anterior no SICONFI..."):
            raw_anterior, nota_anterior = carregar_siconfi(r["municipio"]["id"], r["ano"] - 1)
            resumo_anterior, _, nota_calculo_anterior = processar_resumo(raw_anterior)
            if nota_calculo_anterior:
                nota_anterior += " " + nota_calculo_anterior
            atual_simplificado = "RREO Simplificado" in r["nota"]
            anterior_simplificado = "RREO Simplificado" in nota_anterior
            if not resumo_anterior.empty and atual_simplificado != anterior_simplificado:
                resumo_anterior = pd.DataFrame()
                nota_anterior += " Comparação omitida: tipos de RREO diferentes entre exercícios."
        st.caption(
            f"Comparação: {r['ano'] - 1} versus {r['ano']}, mesma função e 6º bimestre; "
            "valores nominais, sem ajuste de inflação."
        )
        pdf_pago = criar_pdf(
            r["municipio"]["nome"], r["municipio"]["uf"], r["ano"],
            r["resumo"], r["nota"], r["pncp"], demonstracao=False,
            contexto_pncp=r["nota_pncp"],
            resumo_anterior=resumo_anterior,
            nota_anterior=nota_anterior,
        )
        st.download_button("Gerar PDF para entrega manual", data=pdf_pago, file_name=f"raio_x_{r['municipio']['id']}_{r['ano']}.pdf", mime="application/pdf", width="stretch")

st.divider()
st.caption("Protótipo inicial • Sem integração de pagamento • Consulte e valide as fontes oficiais • Versão de teste, não auditada.")
