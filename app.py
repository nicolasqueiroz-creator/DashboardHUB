import streamlit as st
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import base64
import json
import requests
import re
import time
from textwrap import dedent

st.set_page_config(
    page_title="Dashboard de Hubs Shopee",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

LOGO_PATH = Path(__file__).parent / "shopee_logo.png"
CSS_PATH = Path(__file__).parent / "style.css"

HUBS = ["LPE-02", "LPE-03", "LPE-07", "LPE-11", "LPE-12"]


def html(txt):
    st.markdown(dedent(txt).strip(), unsafe_allow_html=True)


def img_base64(path):
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode()
    return ""


if CSS_PATH.exists():
    html(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>")

logo64 = img_base64(LOGO_PATH)


def hub_default():
    return {
        "Volume": 0,
        "Entregues": 0,
        "Pendentes": 0,
        "Pacotes em Rota de Entrega": 0,
        "Onhold": 0,
        "Total de Rotas": 0,
        "Não Coletadas": 0,
    }


if "hub" not in st.session_state:
    st.session_state.hub = "LPE-12"

if "tela" not in st.session_state:
    st.session_state.tela = "home"

if "tema_escuro" not in st.session_state:
    st.session_state.tema_escuro = False

if "hubs" not in st.session_state:
    st.session_state.hubs = {hub: hub_default() for hub in HUBS}

if "rotas_por_hub" not in st.session_state:
    st.session_state.rotas_por_hub = {hub: [] for hub in HUBS}

if "terminal" not in st.session_state:
    st.session_state.terminal = []


params = st.query_params

if "tela" in params:
    st.session_state.tela = params.get("tela", "home")

if "hub" in params:
    hub_param = params.get("hub")
    if hub_param in HUBS:
        st.session_state.hub = hub_param
        st.session_state.tela = "hub"


def log(msg):
    st.session_state.terminal.append(f"> {msg}")


def limpar_ats(texto):
    return [
        at.strip().upper()
        for at in texto.replace(",", "\n").replace(";", "\n").splitlines()
        if at.strip()
    ]


def epoch_para_data(valor):
    try:
        valor = int(valor)
        if valor <= 0:
            return "Não bipada"
        return datetime.fromtimestamp(valor).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return "Não bipada"


def parse_curl(curl_text):
    texto = curl_text.replace("\\\n", "\n").replace("\r", "")
    url = ""
    headers = {}
    cookies = ""
    data_raw = None

    match_url = re.search(r"curl\s+'([^']+)'", texto)
    if not match_url:
        match_url = re.search(r'curl\s+"([^"]+)"', texto)

    if match_url:
        url = match_url.group(1).strip()

    for linha in texto.splitlines():
        linha = linha.strip()

        if linha.startswith("-H ") or linha.startswith("-H$") or linha.startswith("-H $"):
            match_header = re.search(r"-H\s+\$?'(.+?)'\s*\\?$", linha)
            if not match_header:
                match_header = re.search(r'-H\s+"(.+?)"\s*\\?$', linha)

            if match_header:
                header = match_header.group(1)
                if ":" in header:
                    k, v = header.split(":", 1)
                    headers[k.strip()] = v.strip()

        if linha.startswith("-b "):
            match_cookie = re.search(r"-b\s+'(.+?)'\s*\\?$", linha)
            if not match_cookie:
                match_cookie = re.search(r'-b\s+"(.+?)"\s*\\?$', linha)

            if match_cookie:
                cookies = match_cookie.group(1).strip()

        if linha.startswith("--data-raw"):
            match_data = re.search(r"--data-raw\s+'(.+?)'\s*\\?$", linha)
            if not match_data:
                match_data = re.search(r'--data-raw\s+"(.+?)"\s*\\?$', linha)

            if match_data:
                data_raw = match_data.group(1).strip()

    if cookies:
        headers["cookie"] = cookies

    headers_lower = {k.lower(): v for k, v in headers.items()}

    if "content-type" not in headers_lower and "assignment_task/search/v2" in url:
        headers["content-type"] = "application/json;charset=UTF-8"

    return url, headers, data_raw


def base_url_do_curl(curl_text):
    url, _, _ = parse_curl(curl_text)
    if not url:
        raise ValueError("Não consegui identificar a URL no bash.")
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def executar_curl(curl_text, body_override=None):
    url, headers, data_raw = parse_curl(curl_text)

    if not url:
        raise ValueError("URL não encontrada no bash/cURL.")

    if not data_raw and "assignment_task/search/v2" in url:
        data_raw = '{"pageno":1,"count":100,"search_type":0}'

    method = "POST" if data_raw else "GET"

    if body_override is not None:
        method = "POST"
        data_raw = json.dumps(body_override, ensure_ascii=False)

    if method == "POST":
        resp = requests.post(url, headers=headers, data=data_raw, timeout=60)
    else:
        resp = requests.get(url, headers=headers, timeout=60)

    resp.raise_for_status()
    return resp.json()


def carregar_json_ou_curl(texto):
    texto = texto.strip()
    if not texto:
        return None
    if texto.startswith("{"):
        return json.loads(texto)
    return executar_curl(texto)


def buscar_todas_paginas_v2(curl_v2, limite_paginas=50, count=100):
    todas = []
    url, headers, data_raw = parse_curl(curl_v2)

    if not url:
        raise ValueError("URL do V2 não encontrada.")

    if data_raw:
        try:
            body_base = json.loads(data_raw)
        except Exception:
            body_base = {"pageno": 1, "count": count, "search_type": 0}
    else:
        body_base = {"pageno": 1, "count": count, "search_type": 0}

    total_api = None

    for pagina in range(1, limite_paginas + 1):
        body = dict(body_base)
        body["pageno"] = pagina
        body["count"] = count

        resposta = executar_curl(curl_v2, body_override=body)
        data = resposta.get("data", {})
        lista = data.get("list", [])

        if total_api is None:
            total_api = data.get("total", 0)

        if not lista:
            break

        todas.extend(lista)

        if total_api and len(todas) >= total_api:
            break

    return todas


def extrair_lista_pacotes(resposta):
    if not isinstance(resposta, dict):
        return []

    data = resposta.get("data", resposta)

    if isinstance(data, dict):
        for chave in ["list", "orders", "order_list", "package_list", "tracking_list"]:
            if isinstance(data.get(chave), list):
                return data.get(chave)

    if isinstance(data, list):
        return data

    return []


def extrair_status_pacote(pacote):
    if not isinstance(pacote, dict):
        return None

    for chave in [
        "status",
        "tracking_status",
        "order_status",
        "shipment_status",
        "parcel_status",
        "delivery_status",
    ]:
        if chave in pacote:
            try:
                return int(pacote.get(chave))
            except Exception:
                return None

    return None


def get_com_retry(url, headers, timeout=30, tentativas=3):
    ultimo_erro = None

    for tentativa in range(tentativas):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            ultimo_erro = e
            time.sleep(0.7 * (tentativa + 1))

    raise ultimo_erro


def buscar_metricas_pacotes_por_at(curl_auth, at):
    base_url = base_url_do_curl(curl_auth)
    _, headers, _ = parse_curl(curl_auth)

    total = 0
    entregues = 0
    onhold = 0
    pendentes_status = 0
    count = 200

    for pagina in range(1, 50):
        url = (
            f"{base_url}/spx_delivery/admin/assignment/assignment_task/detail/order/search"
            f"?pageno={pagina}&count={count}&assignment_task_id={at}"
        )

        resp = get_com_retry(url, headers, timeout=30, tentativas=3)
        pacotes = extrair_lista_pacotes(resp.json())

        if not pacotes:
            break

        for pacote in pacotes:
            status = extrair_status_pacote(pacote)
            total += 1

            if status == 4:
                entregues += 1
            elif status == 5:
                onhold += 1
            elif status == 2:
                pendentes_status += 1

        if len(pacotes) < count:
            break

    pendentes = pendentes_status

    if pendentes == 0 and total > 0:
        pendentes = max(total - entregues - onhold, 0)

    performance = entregues / total if total else 0

    return {
        "Total": total,
        "Entregues": entregues,
        "On Hold": onhold,
        "Pendentes": pendentes,
        "Performance": performance,
        "Performance %": f"{performance * 100:.1f}%"
    }


def buscar_metricas_em_lote(curl_auth, mapa_v2, max_workers=6):
    total_ats = len(mapa_v2)

    if total_ats == 0:
        return {}

    progresso = st.progress(0)
    status_txt = st.empty()
    resultados = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tarefas = {
            executor.submit(buscar_metricas_pacotes_por_at, curl_auth, at): at
            for at in mapa_v2.keys()
        }

        concluidas = 0

        for future in as_completed(tarefas):
            at = tarefas[future]

            try:
                resultados[at] = future.result()
            except Exception as e:
                resultados[at] = {
                    "Total": 0,
                    "Entregues": 0,
                    "On Hold": 0,
                    "Pendentes": 0,
                    "Performance": 0,
                    "Performance %": "0.0%"
                }
                log(f"Erro ao buscar pacotes da AT {at}: {e}")

            concluidas += 1
            progresso.progress(concluidas / total_ats)
            status_txt.info(f"Consultando pacotes: {concluidas}/{total_ats}")

    progresso.empty()
    status_txt.empty()
    return resultados


def processar_rotas_v2(lista_v2, ats_desejadas=None):
    ats_set = set(ats_desejadas or [])
    mapa = {}

    for rota in lista_v2:
        at = str(rota.get("assignment_task_id", "")).upper().strip()

        if not at:
            continue

        if ats_set and at not in ats_set:
            continue

        mapa[at] = {
            "AT": at,
            "Driver ID": rota.get("driver_id", ""),
            "Motorista": rota.get("driver_name", ""),
            "Modal": rota.get("vehicle_type", ""),
            "Gaiola": rota.get("corridor_cage", ""),
            "Bairro": rota.get("neighborhood", ""),
            "Cluster": rota.get("cluster", ""),
            "Cidade": rota.get("city", ""),
            "Hora Bipada": epoch_para_data(rota.get("assigned_time", 0)),
            "Hora Atribuição": epoch_para_data(rota.get("driver_assigned_time", 0)),
            "Distância KM": rota.get("total_distance", ""),
            "Paradas": rota.get("stops_number", ""),
            "Station": rota.get("station_name", ""),
            "Total": 0,
            "Entregues": 0,
            "On Hold": 0,
            "Pendentes": 0,
            "Performance": 0,
            "Performance %": "0.0%",
        }

    return mapa


def criar_rotas_apenas_v2(mapa_v2):
    rotas = []

    for _, rota in mapa_v2.items():
        total = int(rota.get("Total") or 0)
        entregues = int(rota.get("Entregues") or 0)

        rota["Performance"] = entregues / total if total else 0
        rota["Performance %"] = f"{rota['Performance'] * 100:.1f}%"

        rotas.append(rota)

    return rotas


def atualizar_hub_com_rotas(hub_atual, rotas):
    total = sum(int(r.get("Total") or 0) for r in rotas)
    entregues = sum(int(r.get("Entregues") or 0) for r in rotas)
    onhold = sum(int(r.get("On Hold") or 0) for r in rotas)
    qtd_rotas = len(rotas)
    pendentes = sum(int(r.get("Pendentes") or 0) for r in rotas)
    nao_coletadas = sum(
        1 for r in rotas
        if str(r.get("Hora Bipada", "")).lower() == "não bipada"
    )

    st.session_state.hubs[hub_atual]["Volume"] = total
    st.session_state.hubs[hub_atual]["Total de Rotas"] = qtd_rotas
    st.session_state.hubs[hub_atual]["Pendentes"] = pendentes
    st.session_state.hubs[hub_atual]["Pacotes em Rota de Entrega"] = pendentes
    st.session_state.hubs[hub_atual]["Entregues"] = entregues
    st.session_state.hubs[hub_atual]["Onhold"] = onhold
    st.session_state.hubs[hub_atual]["Não Coletadas"] = nao_coletadas


def ordenar_rotas(rotas, campo_ordenacao, ordem_desc):
    rotas_exibicao = rotas.copy()

    if campo_ordenacao == "Performance":
        rotas_exibicao.sort(key=lambda x: float(x.get("Performance", 0)), reverse=ordem_desc)
    elif campo_ordenacao == "Hora Bipada":
        rotas_exibicao.sort(key=lambda x: str(x.get("Hora Bipada", "")), reverse=ordem_desc)
    elif campo_ordenacao == "Motorista":
        rotas_exibicao.sort(key=lambda x: str(x.get("Motorista", "")), reverse=ordem_desc)
    elif campo_ordenacao == "AT":
        rotas_exibicao.sort(key=lambda x: str(x.get("AT", "")), reverse=ordem_desc)
    elif campo_ordenacao == "Total":
        rotas_exibicao.sort(key=lambda x: int(x.get("Total", 0)), reverse=ordem_desc)
    elif campo_ordenacao == "Entregues":
        rotas_exibicao.sort(key=lambda x: int(x.get("Entregues", 0)), reverse=ordem_desc)
    elif campo_ordenacao == "Pendentes":
        rotas_exibicao.sort(key=lambda x: int(x.get("Pendentes", 0)), reverse=ordem_desc)

    return rotas_exibicao


html(f"""
<style>
[data-testid="stSidebar"], [data-testid="collapsedControl"] {{
    display: none !important;
}}

.fixed-sidebar {{
    position: fixed;
    left: 0;
    top: 0;
    width: 285px;
    height: 100vh;
    background: linear-gradient(180deg, #ff5a00 0%, #f0442d 100%);
    z-index: 999999;
    padding: 32px 18px;
    box-sizing: border-box;
    box-shadow: 4px 0 25px rgba(0,0,0,0.14);
}}

.fixed-sidebar img {{
    width: 225px;
    display: block;
    margin: 0 auto 42px auto;
    filter: brightness(0) invert(1);
}}

.fixed-menu-btn {{
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    color: white !important;
    padding: 18px 20px;
    border-radius: 16px;
    font-weight: 900;
    margin-bottom: 14px;
    text-decoration: none !important;
    font-size: 18px;
    box-sizing: border-box;
}}

.fixed-menu-btn.active {{
    background: white;
    color: #ee4d2d !important;
}}

.fixed-menu-btn:hover {{
    background: rgba(255,255,255,0.24);
    text-decoration: none !important;
}}

.fixed-footer {{
    position: absolute;
    bottom: 28px;
    left: 18px;
    right: 18px;
    padding: 22px;
    border-radius: 18px;
    background: rgba(255,255,255,0.18);
    color: white;
    font-weight: 900;
    font-size: 16px;
}}

.block-container {{
    padding-left: 320px !important;
    padding-top: 2.5rem !important;
}}

.hub-list-card {{
    position: relative;
    display: grid;
    grid-template-columns: 190px 1fr 1fr 1fr 1fr 160px;
    align-items: center;
    gap: 18px;
    background: white;
    border-radius: 14px;
    padding: 24px 22px;
    margin-bottom: 14px;
    box-shadow: 0 10px 30px rgba(15,23,42,0.07);
    border: 1px solid #eef1f5;
    transition: all .25s ease;
    min-height: 120px;
    overflow: hidden;
}}

.hub-list-card:hover {{
    border: 1.5px solid #ee4d2d;
    transform: translateY(-2px);
    box-shadow: 0 18px 40px rgba(238,77,45,0.12);
}}

.hub-list-card .open-hover {{
    opacity: 0;
    transform: translateY(8px);
    transition: all .25s ease;
    pointer-events: none;
}}

.hub-list-card:hover .open-hover {{
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
}}

.hub-list-name {{
    color: #ee4d2d;
    font-size: 30px;
    font-weight: 900;
}}

.hub-list-icon {{
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: #fff0ea;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 26px;
    margin-top: 18px;
}}

.hub-metric-label {{
    font-weight: 700;
    color: #0f172a;
    font-size: 15px;
}}

.hub-metric-value {{
    font-size: 22px;
    font-weight: 900;
    color: #020617;
    margin-top: 8px;
}}

.hub-metric-sub {{
    font-size: 14px;
    color: #475569;
}}

.hub-open-btn {{
    background: linear-gradient(90deg, #ff5a00, #ee4d2d);
    color: white !important;
    padding: 14px 16px;
    border-radius: 12px;
    font-weight: 900;
    text-align: center;
    text-decoration: none !important;
    display: block;
    white-space: nowrap;
    font-size: 14px;
    max-width: 150px;
}}

.status-list {{
    background:#d9fbe6;
    color:#07883d;
    padding:10px 18px;
    border-radius:999px;
    font-weight:900;
    text-align:center;
}}

.last-update-home {{
    text-align:center;
    margin-top:28px;
    color:#64748b;
    font-size:15px;
}}

@media (max-width: 1200px) {{
    .hub-list-card {{
        grid-template-columns: 1fr;
    }}

    .hub-list-card .open-hover {{
        opacity: 1;
        pointer-events: auto;
    }}
}}
</style>

<div class="fixed-sidebar">
    <img src="data:image/png;base64,{logo64}">
    <a class="fixed-menu-btn active" href="?tela=home" target="_self">🏠 Dashboard</a>
    <a class="fixed-menu-btn" href="?tela=config" target="_self">⚙️ Configurações</a>
    <a class="fixed-menu-btn" href="?tela=consolidado" target="_self">📊 Consolidado</a>
    <div class="fixed-footer">
        📦 Operação Shopee<br>
        <small>Logística & Distribuição</small>
    </div>
</div>
""")


if st.session_state.tema_escuro:
    html("""
    <style>
    .stApp {
        background: #0f172a !important;
        color: white !important;
    }

    .title, .section-title, .dashboard-hub,
    .metric-title, .progress-title,
    .hub-metric-label, .hub-metric-value,
    .subtitle {
        color: white !important;
    }

    .hub-list-card,
    .metric,
    .progress-card,
    .dashboard-box,
    .ats-cell,
    .ats-header {
        background: #1e293b !important;
        color: white !important;
        border-color: #334155 !important;
    }

    .hub-metric-sub,
    .last-update-home,
    .last-update {
        color: #cbd5e1 !important;
    }
    </style>
    """)


@st.dialog("Detalhes da AT")
def detalhes_at(rota):
    st.markdown(f"### {rota['AT']}")
    st.write(f"**Driver ID:** {rota['Driver ID']}")
    st.write(f"**Nome do motorista:** {rota['Motorista']}")
    st.write(f"**Modal:** {rota['Modal']}")
    st.write(f"**Gaiola:** {rota['Gaiola']}")
    st.write(f"**Bairro:** {rota['Bairro']}")
    st.write(f"**Cluster:** {rota['Cluster']}")
    st.write(f"**Hora que a rota foi bipada:** {rota['Hora Bipada']}")
    st.write(f"**Quantidade total:** {rota['Total']}")
    st.write(f"**Entregues:** {rota['Entregues']}")
    st.write(f"**On Hold:** {rota['On Hold']}")
    st.write(f"**Pendentes:** {rota['Pendentes']}")
    st.write(f"**Performance atual:** {rota['Performance %']}")


def render_header(titulo="Dashboard de Hubs", subtitulo="Acompanhe a performance operacional dos hubs em tempo real."):
    c1, c2, c3 = st.columns([0.7, 5, 2])

    with c1:
        html('<div class="hub-icon header-hub-icon">🏢</div>')

    with c2:
        html(f"""
        <div class="title">{titulo}</div>
        <div class="subtitle">{subtitulo}</div>
        """)

    with c3:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width=220)
        else:
            html("<h2 style='color:#ee4d2d;text-align:right;'>🛍️ Shopee</h2>")


def abrir_hub_botao(hub):
    st.session_state.hub = hub
    st.session_state.tela = "hub"
    st.query_params["tela"] = "hub"
    st.query_params["hub"] = hub
    st.rerun()


def voltar_home_botao():
    st.session_state.tela = "home"
    st.query_params["tela"] = "home"
    if "hub" in st.query_params:
        del st.query_params["hub"]
    st.rerun()


def render_home():
    render_header()

    html('<div class="section-title">Selecione um Hub</div>')

    for hub_nome in HUBS:
        dados = st.session_state.hubs[hub_nome]

        html(f"""
        <div class="hub-list-card">
            <div>
                <div class="hub-list-name">{hub_nome}</div>
                <div class="hub-list-icon">🏢</div>
            </div>
            <div>
                <div class="hub-metric-label">📦 Volume</div>
                <div class="hub-metric-value">{dados["Volume"]:,}</div>
                <div class="hub-metric-sub">pacotes</div>
            </div>
            <div>
                <div class="hub-metric-label">🔗 Rotas</div>
                <div class="hub-metric-value">{dados["Total de Rotas"]:,}</div>
                <div class="hub-metric-sub">rotas</div>
            </div>
            <div>
                <div class="hub-metric-label">✅ Entregues</div>
                <div class="hub-metric-value">{dados["Entregues"]:,}</div>
                <div class="hub-metric-sub">pacotes</div>
            </div>
            <div>
                <div class="hub-metric-label">⏸️ On Hold</div>
                <div class="hub-metric-value">{dados["Onhold"]:,}</div>
                <div class="hub-metric-sub">pacotes</div>
            </div>
            <div>
                <div class="status-list">Ativo</div>
                <div class="open-hover" style="margin-top:16px;">
                    <a class="hub-open-btn" href="?tela=hub&hub={hub_nome}" target="_self">
                        Abrir Dashboard →
                    </a>
                </div>
            </div>
        </div>
        """.replace(",", "."))

    html(f"""
    <div class="last-update-home">
        🕘 Última atualização: {datetime.now().strftime("%d/%m/%Y %H:%M")}
    </div>
    """)


def render_dashboard_hub(hub):
    dados = st.session_state.hubs[hub]
    rotas_hub = st.session_state.rotas_por_hub.get(hub, [])

    if st.button("← Voltar para Hubs", key=f"voltar_{hub}"):
        voltar_home_botao()

    html(f"""
    <div class="dashboard-box">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <span class="dashboard-hub">{hub}</span>
                <span class="status" style="float:none;margin-left:18px;">Ativo</span>
            </div>
            <div class="last-update">
                Última atualização: {datetime.now().strftime("%d/%m/%Y %H:%M")} 🔄
            </div>
        </div>
    </div>
    """)

    volume = dados["Volume"]
    entregues = dados["Entregues"]
    pendentes = dados["Pendentes"]

    performance = entregues / volume if volume else 0
    falta = 1 - performance if volume else 0
    meta_95 = math.ceil(volume * 0.95) if volume else 0
    faltam_meta = max(meta_95 - entregues, 0)
    progresso_meta = entregues / meta_95 if meta_95 else 0
    cor_perf = "green-bar" if performance >= 0.95 else "red"

    m1, m2, m3, m4 = st.columns(4)

    metricas_1 = [
        ("📦", "Volume", dados["Volume"], "orange"),
        ("✅", "Entregues", dados["Entregues"], "green"),
        ("⏱️", "Pendentes", dados["Pendentes"], "yellow"),
        ("🚚", "Pacotes em Rota de Entrega", dados["Pacotes em Rota de Entrega"], "purple"),
    ]

    for col, (icone, nome, valor, cor) in zip([m1, m2, m3, m4], metricas_1):
        with col:
            html(f"""
            <div class="metric">
                <div class="circle {cor}">{icone}</div>
                <div>
                    <div class="metric-title">{nome}</div>
                    <div class="metric-value">{valor:,}</div>
                </div>
            </div>
            """.replace(",", "."))

    m5, m6, m7 = st.columns(3)

    metricas_2 = [
        ("⏸️", "Onhold", dados["Onhold"], "blue"),
        ("🔗", "Total de Rotas", dados["Total de Rotas"], "blue"),
        ("📦", "Não Coletadas", dados["Não Coletadas"], "orange"),
    ]

    for col, (icone, nome, valor, cor) in zip([m5, m6, m7], metricas_2):
        with col:
            html(f"""
            <div class="metric second">
                <div class="circle {cor}">{icone}</div>
                <div>
                    <div class="metric-title">{nome}</div>
                    <div class="metric-value">{valor:,}</div>
                </div>
            </div>
            """.replace(",", "."))

    p1, p2, p3 = st.columns(3)

    with p1:
        html(f"""
        <div class="progress-card">
            <div class="progress-title">Performance atual</div>
            <div class="progress-bg">
                <div class="fill {cor_perf}" style="width:{performance*100:.1f}%;"></div>
            </div>
            <div class="progress-info">
                <span>{entregues:,} / {volume:,} entregues</span>
                <b style="color:#ef233c;">{performance*100:.1f}%</b>
            </div>
        </div>
        """.replace(",", "."))

    with p2:
        html(f"""
        <div class="progress-card">
            <div class="progress-title">Meta 95%</div>
            <div class="progress-bg">
                <div class="fill orange-bar" style="width:{min(progresso_meta,1)*100:.1f}%;"></div>
            </div>
            <div class="progress-info">
                <span>Meta: {meta_95:,} entregas</span>
                <b>Faltam {faltam_meta:,}</b>
            </div>
        </div>
        """.replace(",", "."))

    with p3:
        html(f"""
        <div class="progress-card">
            <div class="progress-title">Falta para concluir 100%</div>
            <div class="progress-bg">
                <div class="fill blue-bar" style="width:{falta*100:.1f}%;"></div>
            </div>
            <div class="progress-info">
                <span>Faltam {pendentes:,}</span>
                <b style="color:#0066ff;">{falta*100:.1f}%</b>
            </div>
        </div>
        """.replace(",", "."))

    if rotas_hub:
        html('<div class="section-title">Lista de ATs do Hub</div>')

        col_ord1, col_ord2 = st.columns([2, 1])

        with col_ord1:
            campo_ordenacao = st.selectbox(
                "Ordenar por",
                ["Performance", "Hora Bipada", "Motorista", "AT", "Total", "Entregues", "Pendentes"],
                index=0,
                key=f"ordenar_{hub}"
            )

        with col_ord2:
            ordem_desc = st.toggle("Decrescente", value=True, key=f"ordem_{hub}")

        rotas_exibicao = ordenar_rotas(rotas_hub, campo_ordenacao, ordem_desc)

        html("""
        <div class="ats-header">
            <div>AT</div>
            <div>Motorista</div>
            <div>Hora bipada</div>
            <div>Performance</div>
            <div>Ação</div>
        </div>
        """)

        for i, rota in enumerate(rotas_exibicao):
            c_at, c_motorista, c_hora, c_perf, c_btn = st.columns([2, 4, 3, 2, 1.2])

            with c_at:
                html(f'<div class="ats-cell at-code">{rota["AT"]}</div>')

            with c_motorista:
                html(f'<div class="ats-cell">{rota["Motorista"] or "Sem motorista"}</div>')

            with c_hora:
                html(f'<div class="ats-cell">{rota["Hora Bipada"]}</div>')

            with c_perf:
                html(f'<div class="ats-cell perf">{rota["Performance %"]}</div>')

            with c_btn:
                if st.button("Abrir", key=f"detalhe_{hub}_{i}_{rota['AT']}", use_container_width=True):
                    detalhes_at(rota)
    else:
        st.info(f"Nenhuma rota carregada para {hub}. Abra a aba Configuração e atualize este hub.")


def render_configuracao_hub(hub):
    html(f"""
    <div class="config-title">⚙️ Configuração Operacional - {hub}</div>
    <div class="config-subtitle">Cole o Bash LIST, o Bash V2 e as ATs específicas deste hub.</div>
    """)

    bash_list = st.text_area("Bash LIST / AUTH", height=180, key=f"bash_list_{hub}")
    bash_v2 = st.text_area("Bash V2", height=240, key=f"bash_v2_{hub}")

    ats_texto = st.text_area(
        "ATs para buscar",
        height=170,
        placeholder="Cole uma AT por linha ou separadas por vírgula.",
        key=f"ats_{hub}"
    )

    col_a, col_b = st.columns([1, 2])

    with col_a:
        iniciar = st.button(f"🚀 Atualizar {hub}", use_container_width=True, key=f"iniciar_{hub}")

    with col_b:
        somente_v2 = st.checkbox("Buscar todas as páginas do V2", value=True, key=f"somente_v2_{hub}")

    if iniciar:
        st.session_state.terminal = []
        ats = limpar_ats(ats_texto)

        try:
            log(f"Iniciando processo do hub {hub}...")
            log(f"ATs digitadas: {len(ats)}")

            if not bash_v2.strip():
                raise ValueError("Cole o Bash V2.")

            if not bash_list.strip():
                raise ValueError("Cole o Bash LIST/AUTH.")

            try:
                log("Validando LIST/AUTH...")
                json_list = carregar_json_ou_curl(bash_list)
                if json_list:
                    log("LIST/AUTH validado com sucesso.")
            except Exception as e:
                log(f"LIST/AUTH não validado, mas vou tentar usar cookies mesmo assim: {e}")

            log("Consultando V2...")

            if somente_v2:
                lista_v2 = buscar_todas_paginas_v2(bash_v2)
            else:
                json_v2 = carregar_json_ou_curl(bash_v2)
                lista_v2 = json_v2.get("data", {}).get("list", [])

            log(f"Rotas recebidas do V2: {len(lista_v2)}")

            mapa_v2 = processar_rotas_v2(lista_v2, ats)
            log(f"ATs encontradas no V2: {len(mapa_v2)}")

            if not mapa_v2:
                st.warning("Nenhuma AT encontrada no V2.")
            else:
                log("Consultando pacotes por AT em modo seguro...")

                metricas_lote = buscar_metricas_em_lote(
                    bash_list,
                    mapa_v2,
                    max_workers=6
                )

                for at, metricas in metricas_lote.items():
                    rota = mapa_v2[at]
                    rota["Total"] = metricas["Total"]
                    rota["Entregues"] = metricas["Entregues"]
                    rota["On Hold"] = metricas["On Hold"]
                    rota["Pendentes"] = metricas["Pendentes"]
                    rota["Performance"] = metricas["Performance"]
                    rota["Performance %"] = metricas["Performance %"]

                log(f"Pacotes consultados para {len(metricas_lote)} ATs.")

                rotas = criar_rotas_apenas_v2(mapa_v2)

                st.session_state.rotas_por_hub[hub] = rotas
                atualizar_hub_com_rotas(hub, rotas)

                st.success(f"Atualização do {hub} finalizada. Abrindo o dashboard atualizado...")
                st.session_state.hub = hub
                st.session_state.tela = "hub"
                st.query_params["tela"] = "hub"
                st.query_params["hub"] = hub
                time.sleep(0.6)
                st.rerun()

        except Exception as e:
            st.error(f"Erro ao processar: {e}")
            log(f"ERRO: {e}")

    for linha in st.session_state.terminal[-40:]:
        st.code(linha, language="text")


if st.session_state.tela == "home":
    render_home()

elif st.session_state.tela == "config":
    render_header(
        titulo="Configurações",
        subtitulo="Personalize a aparência do dashboard."
    )

    st.markdown("### 🎨 Tema do sistema")

    tema_escuro = st.toggle(
        "Ativar tema escuro",
        value=st.session_state.get("tema_escuro", False)
    )

    st.session_state.tema_escuro = tema_escuro

    if tema_escuro:
        st.success("Tema escuro ativo.")
    else:
        st.success("Tema claro ativo.")

elif st.session_state.tela == "consolidado":
    render_header(
        titulo="Consolidado",
        subtitulo="Visão consolidada dos hubs operacionais."
    )
    st.info("Tela consolidada será configurada na próxima etapa.")

else:
    hub_atual = st.session_state.hub

    render_header(
        titulo=f"Dashboard {hub_atual}",
        subtitulo=f"Performance operacional em tempo real do hub {hub_atual}."
    )

    aba_dashboard, aba_config = st.tabs([
        f"📊 Dashboard {hub_atual}",
        f"⚙️ Configuração {hub_atual}"
    ])

    with aba_dashboard:
        render_dashboard_hub(hub_atual)

    with aba_config:
        render_configuracao_hub(hub_atual)
