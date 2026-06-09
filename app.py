import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import base64
import json
import pickle
import requests
import re
import time
import csv
import unicodedata
import hashlib
import uuid
import threading
from io import StringIO, BytesIO
from textwrap import dedent

try:
    import openpyxl
except Exception:
    openpyxl = None

try:
    import pandas as pd
except Exception:
    pd = None

st.set_page_config(
    page_title="Dashboard de Hubs Shopee",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

BASE_DIR = Path(__file__).parent
LOGO_PATH = BASE_DIR / "shopee_logo.png"
CSS_PATH = BASE_DIR / "style.css"
STATE_PATH = BASE_DIR / "dashboard_state.json"
ROTAS_CACHE_PATH = BASE_DIR / "rotas_cache.pkl"
USERS_PATH = BASE_DIR / "usuarios.json"
PENDING_USERS_PATH = BASE_DIR / "cadastros_pendentes.json"
SESSIONS_PATH = BASE_DIR / "sessoes.json"

HUBS = ["LPE-02", "LPE-03", "LPE-07", "LPE-11", "LPE-12"]
FUSO_BRASIL = ZoneInfo("America/Recife")

def agora_brasil():
    return datetime.now(FUSO_BRASIL)


def html(txt):
    st.markdown(dedent(txt).strip(), unsafe_allow_html=True)


def img_base64(path):
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode()
    return ""


if CSS_PATH.exists():
    html(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>")

logo64 = img_base64(LOGO_PATH)

# =========================================================
# LOGIN, USUÁRIOS E CONTROLE DE ACESSO
# =========================================================

def hash_senha(senha):
    return hashlib.sha256(str(senha or "").encode("utf-8")).hexdigest()


def normalizar_login(login):
    return str(login or "").strip().lower()


def usuario_padrao_admin():
    return {
        "nicolas": {
            "nome": "Nicolas Queiroz",
            "email": "nicolas.queiroz@shopee.com",
            "senha_hash": hash_senha("123456"),
            "perfil": "admin",
            "hub": "TODOS",
            "ativo": True,
        }
    }


def carregar_usuarios():
    try:
        if not USERS_PATH.exists():
            usuarios = usuario_padrao_admin()
            salvar_usuarios(usuarios)
            return usuarios
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            usuarios = json.load(f)
        alterou = False
        for _, dados in usuarios.items():
            if "senha" in dados and "senha_hash" not in dados:
                dados["senha_hash"] = hash_senha(dados.get("senha", ""))
                dados.pop("senha", None)
                alterou = True
        if alterou:
            salvar_usuarios(usuarios)
        return usuarios
    except Exception:
        return usuario_padrao_admin()


def salvar_usuarios(usuarios):
    try:
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(usuarios, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def carregar_pendentes():
    try:
        if PENDING_USERS_PATH.exists():
            with open(PENDING_USERS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def salvar_pendentes(pendentes):
    try:
        with open(PENDING_USERS_PATH, "w", encoding="utf-8") as f:
            json.dump(pendentes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def carregar_sessoes():
    try:
        if SESSIONS_PATH.exists():
            with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def salvar_sessoes(sessoes):
    try:
        with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(sessoes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def encontrar_usuario_por_login(login_digitado, usuarios):
    login = normalizar_login(login_digitado)
    for usuario, dados in usuarios.items():
        if login == normalizar_login(usuario) or login == normalizar_login(dados.get("email", "")):
            return usuario, dados
    return None, None


def criar_sessao(usuario, dados):
    token = uuid.uuid4().hex
    sessoes = carregar_sessoes()
    sessoes[token] = {"usuario": usuario, "criado_em": agora_brasil().strftime("%d/%m/%Y %H:%M")}
    salvar_sessoes(sessoes)

    st.session_state.auth_token = token
    st.session_state.logado = True
    st.session_state.usuario_login = usuario
    st.session_state.usuario_nome = dados.get("nome", usuario)
    st.session_state.usuario_email = dados.get("email", "")
    st.session_state.perfil = dados.get("perfil", "analista")
    st.session_state.hub_permitido = dados.get("hub", "")
    st.query_params["auth"] = token
    return token


def restaurar_sessao_por_token():
    if st.session_state.get("logado", False):
        token_atual = st.session_state.get("auth_token") or st.query_params.get("auth", "")
        if token_atual:
            st.query_params["auth"] = token_atual
        return True

    token = st.session_state.get("auth_token") or st.query_params.get("auth", "")
    if not token:
        return False

    sessao = carregar_sessoes().get(token)
    if not sessao:
        return False

    usuarios = carregar_usuarios()
    usuario = sessao.get("usuario")
    dados = usuarios.get(usuario)
    if not dados or not dados.get("ativo", False):
        return False

    st.session_state.auth_token = token
    st.session_state.logado = True
    st.session_state.usuario_login = usuario
    st.session_state.usuario_nome = dados.get("nome", usuario)
    st.session_state.usuario_email = dados.get("email", "")
    st.session_state.perfil = dados.get("perfil", "analista")
    st.session_state.hub_permitido = dados.get("hub", "")
    st.query_params["auth"] = token
    return True


def auth_query(extra=""):
    token = st.session_state.get("auth_token") or st.query_params.get("auth", "")
    partes = []
    if token:
        partes.append(f"auth={token}")
    if extra:
        partes.append(extra)
    return "&".join(partes)


def perfil_usuario():
    return str(st.session_state.get("perfil", "")).lower()


def usuario_logado_eh_gestao():
    return perfil_usuario() in ["admin", "lideranca", "liderança"]


def usuario_logado_eh_coordenador():
    return perfil_usuario() == "coordenador"


def usuario_pode_acessar_hub(hub):
    if usuario_logado_eh_gestao():
        return True
    hub_permitido = st.session_state.get("hub_permitido", "")
    return str(hub).upper() == str(hub_permitido).upper()


def hubs_visiveis_usuario():
    if usuario_logado_eh_gestao():
        return HUBS
    hub_permitido = st.session_state.get("hub_permitido", "")
    return [hub_permitido] if hub_permitido in HUBS else []


def usuario_pode_ver_consolidado():
    return usuario_logado_eh_gestao() or usuario_logado_eh_coordenador()


def fazer_logout():
    token = st.session_state.get("auth_token", st.query_params.get("auth", ""))
    if token:
        try:
            sessoes = carregar_sessoes()
            sessoes.pop(token, None)
            salvar_sessoes(sessoes)
        except Exception:
            pass

    for chave in ["logado", "auth_token", "usuario_login", "usuario_nome", "usuario_email", "perfil", "hub_permitido"]:
        st.session_state.pop(chave, None)

    st.session_state.logado = False
    st.session_state.tela = "login"
    st.query_params.clear()
    st.query_params["tela"] = "login"
    st.rerun()


def render_login():
    import streamlit.components.v1 as components
    hora_atual = agora_brasil().strftime("%H:%M:%S")
    mostrar_cadastro = bool(st.session_state.get("mostrar_cadastro_login", False))

    html(f'''
<style>
[data-testid="stSidebar"], [data-testid="collapsedControl"], header, footer, #MainMenu {{display:none !important; visibility:hidden !important;}}
.stApp {{
    min-height:100vh !important;
    background:
        radial-gradient(circle at 38% 24%, rgba(255,90,0,.30) 0%, rgba(255,90,0,.10) 25%, transparent 48%),
        radial-gradient(circle at 92% 74%, rgba(238,77,45,.78) 0%, rgba(238,77,45,.42) 30%, transparent 60%),
        linear-gradient(112deg, #020815 0%, #06101f 38%, #160d15 60%, #ee4d2d 135%) !important;
    color:#fff !important; overflow-x:hidden !important;
}}
.block-container {{max-width:1950px !important; padding:22px 34px 16px 34px !important; margin:0 auto !important;}}
div[data-testid="column"] {{display:flex !important; align-items:center !important; justify-content:center !important;}}
div[data-testid="column"] > div {{width:100% !important;}}
div[data-testid="stForm"] {{
    border:2px solid rgba(255,102,42,.92) !important; border-radius:32px !important;
    background:linear-gradient(145deg, rgba(5,14,28,.96), rgba(7,7,16,.90)) !important;
    box-shadow:0 0 32px rgba(255,90,0,.38), inset 0 0 70px rgba(255,255,255,.035) !important;
    padding:70px 58px 54px 58px !important; min-height:900px !important; max-height:900px !important;
    max-width:720px !important; width:720px !important; margin:0 auto !important; overflow:hidden !important;
}}
.login-right-logo {{ text-align:center; margin-bottom:10px; }}
.login-right-logo img {{ width:235px; max-width:74%; }}
.login-right-sub {{ text-align:center; color:rgba(255,255,255,.92); font-size:16px; font-weight:800; margin-top:8px; }}
.login-right-accent {{ width:86px; height:4px; background:#ff5a21; border-radius:99px; margin:18px auto 30px auto; box-shadow:0 0 22px rgba(255,90,0,.80); }}
.login-access-divider {{ display:flex; align-items:center; gap:18px; color:rgba(255,255,255,.80); font-size:14px; font-weight:850; margin:24px 0 18px 0; }}
.login-access-divider::before, .login-access-divider::after {{ content:""; height:1px; flex:1; background:rgba(255,255,255,.22); }}
div[data-testid="stForm"] label {{ color:rgba(255,255,255,.90) !important; font-size:13px !important; font-weight:850 !important; }}
div[data-testid="stForm"] input {{ height:58px !important; background:rgba(4,13,27,.92) !important; border:1.5px solid rgba(255,90,0,.78) !important; color:#fff !important; border-radius:12px !important; font-size:14px !important; font-weight:700 !important; }}
div[data-testid="stFormSubmitButton"] button {{ width:100% !important; min-height:58px !important; border-radius:13px !important; border:1px solid rgba(255,90,0,.90) !important; font-size:16px !important; font-weight:950 !important; color:#fff !important; background:linear-gradient(90deg,#ff5a00,#f0442d) !important; }}
.login-note-bottom {{ margin-top:18px; text-align:center; color:rgba(255,255,255,.78); font-size:12px; line-height:1.40; font-weight:750; }}
.register-title {{ text-align:center; color:#ff5a21; font-size:22px; font-weight:1000; margin:8px 0 18px 0; }}
</style>
''')

    left, right = st.columns([1, 1], gap="large")
    left_html = f'''
<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin:0; padding:0; background:transparent; font-family:Arial, Helvetica, sans-serif; color:#fff; }}
.login-left-card {{ position:relative; height:900px; border-radius:32px; padding:46px 48px 36px 48px; border:1px solid rgba(255,255,255,.18); background: radial-gradient(circle at 70% 4%, rgba(255,90,0,.30), transparent 26%), linear-gradient(145deg, rgba(7,18,35,.97), rgba(8,9,20,.88)); box-shadow:0 34px 90px rgba(0,0,0,.40); overflow:hidden; box-sizing:border-box; }}
.login-left-content {{ position:relative; z-index:2; }}
.login-logo-white img {{ width:235px; filter:brightness(0) invert(1); margin-bottom:34px; }}
.login-chip {{ display:inline-flex; padding:9px 19px; border-radius:999px; border:1px solid rgba(255,105,55,.75); background:rgba(238,77,45,.14); color:#ffb09a; font-size:11px; font-weight:950; letter-spacing:1.8px; text-transform:uppercase; margin-bottom:22px; }}
.login-kicker {{ color:#fff; font-size:32px; font-weight:950; margin-bottom:12px; }}
.login-title-big {{ color:#ff5a21; font-size:54px; line-height:.96; font-weight:1000; margin:0 0 14px 0; }}
.login-subtitle-left {{ max-width:650px; color:rgba(245,247,255,.88); font-size:17px; line-height:1.34; font-weight:700; margin:0 0 24px 0; }}
.login-section-label {{ color:#ff6b2a; font-size:15px; font-weight:950; text-transform:uppercase; margin:0 0 12px 0; }}
.hubs-grid {{ display:grid; grid-template-columns:repeat(5, 1fr); gap:11px; margin-bottom:22px; }}
.hub-tile-login {{ min-height:98px; border-radius:14px; border:1px solid rgba(255,90,0,.78); background:rgba(7,16,31,.60); display:flex; flex-direction:column; align-items:center; justify-content:center; color:#fff; font-weight:950; font-size:17px; }}
.hub-tile-login span {{ font-size:32px; color:#ff5a21; margin-bottom:7px; line-height:1; }}
.monitor-grid-login {{ display:grid; grid-template-columns:repeat(5, 1fr); gap:10px; margin-bottom:18px; }}
.monitor-login-tile {{ min-height:94px; border-radius:14px; border:1px solid rgba(255,255,255,.20); background:rgba(13,23,42,.62); padding:13px 8px; text-align:center; box-sizing:border-box; }}
.monitor-login-tile .icon {{ font-size:24px; color:#ff5a21; margin-bottom:7px; }}
.monitor-login-tile b {{ display:block; font-size:14px; line-height:1.12; }}
.system-login-row {{ height:44px; border-radius:14px; border:1px solid rgba(255,255,255,.22); background:rgba(8,16,30,.52); display:flex; align-items:center; justify-content:space-between; padding:0 18px; margin:8px 0 18px 0; color:#fff; font-weight:800; font-size:14px; }}
.system-login-row .clock {{ color:#ff6b2a; font-size:16px; font-weight:950; }}
.login-footer-brand {{ display:grid; grid-template-columns:1.08fr 1px .9fr; align-items:center; gap:24px; padding-top:14px; border-top:1px solid rgba(255,255,255,.14); }}
.footer-title-login {{ font-size:19px; font-weight:950; color:#fff; }} .footer-sub-login {{ color:rgba(255,255,255,.82); font-size:13px; margin-top:2px; }} .footer-line-login {{ height:46px; background:rgba(255,255,255,.22); }} .footer-version-login {{ color:#ff5a21; font-size:14px; font-weight:950; }} .footer-author-login {{ color:#fff; font-size:13px; font-weight:850; margin-top:7px; }}
</style></head><body>
<div class="login-left-card"><div class="login-left-content">
<div class="login-logo-white"><img src="data:image/png;base64,{logo64}" /></div>
<div class="login-chip">⚡ Central logística inteligente</div><div class="login-kicker">Dashboard</div><h1 class="login-title-big">HUBS SPX</h1>
<p class="login-subtitle-left">Acompanhe em tempo real a performance dos hubs, rotas, drivers e indicadores operacionais.</p>
<div class="login-section-label">Nossos hubs</div><div class="hubs-grid">
<div class="hub-tile-login"><span>⌂</span>LPE-02</div><div class="hub-tile-login"><span>⌂</span>LPE-03</div><div class="hub-tile-login"><span>⌂</span>LPE-07</div><div class="hub-tile-login"><span>⌂</span>LPE-11</div><div class="hub-tile-login"><span>⌂</span>LPE-12</div></div>
<div class="login-section-label">Monitoramento em tempo real</div><div class="monitor-grid-login"><div class="monitor-login-tile"><div class="icon">⌖</div><b>Rotas<br>Ativas</b></div><div class="monitor-login-tile"><div class="icon">♙</div><b>Drivers<br>Conectados</b></div><div class="monitor-login-tile"><div class="icon">▥</div><b>Performance<br>Operacional</b></div><div class="monitor-login-tile"><div class="icon">◎</div><b>Delivery<br>Success</b></div><div class="monitor-login-tile"><div class="icon">◷</div><b>Atualizações<br>em Tempo Real</b></div></div>
<div class="system-login-row"><div>Sistema atualizado em tempo real</div><div class="clock">{hora_atual}</div></div>
<div class="login-footer-brand"><div><div class="footer-title-login">Shopee Express Brasil</div><div class="footer-sub-login">Operations Control Center</div></div><div class="footer-line-login"></div><div><div class="footer-version-login">Dashboard HUBS SPX</div><div class="footer-author-login">Criado por Nicolas Queiroz</div></div></div>
</div></div></body></html>
'''
    with left:
        components.html(left_html, height=920, scrolling=False)

    with right:
        if not mostrar_cadastro:
            with st.form("form_login_panel"):
                st.markdown(f'''<div class="login-right-logo"><img src="data:image/png;base64,{logo64}" /></div><div class="login-right-sub">Acesse com seu usuário ou e-mail corporativo</div><div class="login-right-accent"></div>''', unsafe_allow_html=True)
                login_digitado = st.text_input("Usuário ou e-mail", placeholder="Usuário ou usuario@shopee.com")
                senha_digitada = st.text_input("Senha", placeholder="Senha", type="password")
                st.checkbox("Lembrar-me", value=True)
                entrar = st.form_submit_button("ENTRAR NO PAINEL  →", use_container_width=True)
                st.markdown('<div class="login-access-divider">Não tem acesso?</div>', unsafe_allow_html=True)
                solicitar_tela = st.form_submit_button("♙  SOLICITAR CADASTRO DE USUÁRIO", use_container_width=True)
                st.markdown('<div class="login-note-bottom">Central segura SPX: o acesso é liberado conforme perfil operacional. Analistas visualizam apenas o hub cadastrado; liderança e admin visualizam a operação consolidada.</div>', unsafe_allow_html=True)

            if solicitar_tela:
                st.session_state["mostrar_cadastro_login"] = True
                st.rerun()
            if entrar:
                usuarios = carregar_usuarios()
                usuario, dados = encontrar_usuario_por_login(login_digitado, usuarios)
                if not dados:
                    st.error("Usuário/e-mail ou senha inválidos.")
                elif not dados.get("ativo", False):
                    st.error("Seu usuário está inativo. Procure a liderança/admin.")
                elif dados.get("senha_hash") != hash_senha(senha_digitada):
                    st.error("Usuário/e-mail ou senha inválidos.")
                else:
                    token = criar_sessao(usuario, dados)
                    if not usuario_logado_eh_gestao() and st.session_state.hub_permitido in HUBS:
                        st.session_state.hub = st.session_state.hub_permitido
                        st.session_state.tela = "hub"
                        st.query_params.clear(); st.query_params["auth"] = token; st.query_params["tela"] = "hub"; st.query_params["hub"] = st.session_state.hub_permitido
                    else:
                        st.session_state.tela = "home"
                        st.query_params.clear(); st.query_params["auth"] = token; st.query_params["tela"] = "home"
                    st.rerun()
        else:
            with st.form("form_cadastro_panel"):
                st.markdown(f'''<div class="login-right-logo"><img src="data:image/png;base64,{logo64}" /></div><div class="register-title">Solicitar cadastro de usuário</div><div class="login-right-accent"></div>''', unsafe_allow_html=True)
                nome = st.text_input("Nome completo", placeholder="Nome completo")
                usuario = st.text_input("Usuário desejado", placeholder="usuario")
                email = st.text_input("E-mail", placeholder="usuario@shopee.com")
                hub = st.selectbox("Hub", HUBS)
                senha = st.text_input("Senha desejada", type="password", placeholder="Senha")
                confirmar = st.text_input("Confirmar senha", type="password", placeholder="Confirmar senha")
                solicitar = st.form_submit_button("SOLICITAR CADASTRO  →", use_container_width=True)
                voltar = st.form_submit_button("VOLTAR AO LOGIN", use_container_width=True)
            if voltar:
                st.session_state["mostrar_cadastro_login"] = False
                st.rerun()
            if solicitar:
                usuario_norm = normalizar_login(usuario)
                email_norm = normalizar_login(email)
                usuarios = carregar_usuarios()
                pendentes = carregar_pendentes()
                if not nome.strip() or not usuario_norm or not email_norm or not senha:
                    st.error("Preencha todos os campos.")
                elif senha != confirmar:
                    st.error("As senhas não conferem.")
                elif usuario_norm in usuarios or usuario_norm in pendentes:
                    st.error("Esse usuário já existe ou já está pendente de aprovação.")
                elif any(email_norm == normalizar_login(d.get("email", "")) for d in usuarios.values()):
                    st.error("Esse e-mail já está cadastrado.")
                else:
                    pendentes[usuario_norm] = {"nome": nome.strip(), "email": email_norm, "senha_hash": hash_senha(senha), "perfil_solicitado": "analista", "hub": hub, "status": "pendente", "criado_em": datetime.now().strftime("%d/%m/%Y %H:%M")}
                    salvar_pendentes(pendentes)
                    st.success("Cadastro solicitado com sucesso. Aguarde aprovação da liderança/admin.")


def render_acesso_negado(hub):
    render_header("Acesso não permitido", "Você não possui permissão para visualizar este hub.")
    st.error(f"Acesso bloqueado para {hub}.")
    st.info(f"Hub liberado para seu usuário: {st.session_state.get('hub_permitido', '-')}")
    if st.button("Voltar", type="primary"):
        st.session_state.tela = "home"
        st.rerun()


def render_admin_usuarios():
    render_header("Gestão de usuários", "Aprove cadastros e controle o acesso por hub.")
    if not usuario_logado_eh_gestao():
        st.error("Acesso permitido apenas para liderança/admin.")
        return
    usuarios = carregar_usuarios()
    pendentes = carregar_pendentes()
    st.subheader("Cadastros pendentes")
    if not pendentes:
        st.info("Nenhum cadastro pendente.")
    else:
        for usuario, dados in list(pendentes.items()):
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2.4, 1.2, 1.2, 1])
                with c1:
                    st.write(f"**{dados.get('nome', usuario)}**")
                    st.caption(f"Usuário: {usuario} | E-mail: {dados.get('email','')} | Solicitado em: {dados.get('criado_em','')}")
                with c2:
                    perfil_aprovado = st.selectbox("Perfil", ["analista", "coordenador", "lideranca", "admin"], key=f"perfil_pendente_{usuario}")
                with c3:
                    opcoes_hub = HUBS + ["TODOS"]
                    hub_atual = dados.get("hub", "LPE-12")
                    hub_aprovado = st.selectbox("Hub", opcoes_hub, index=opcoes_hub.index(hub_atual) if hub_atual in opcoes_hub else 0, key=f"hub_pendente_{usuario}")
                with c4:
                    if st.button("Aprovar", key=f"aprovar_{usuario}", type="primary", use_container_width=True):
                        usuarios[usuario] = {"nome": dados.get("nome", usuario), "email": dados.get("email", ""), "senha_hash": dados.get("senha_hash", ""), "perfil": perfil_aprovado, "hub": hub_aprovado, "ativo": True}
                        pendentes.pop(usuario, None)
                        salvar_usuarios(usuarios); salvar_pendentes(pendentes); st.rerun()
                    if st.button("Rejeitar", key=f"rejeitar_{usuario}", use_container_width=True):
                        pendentes.pop(usuario, None)
                        salvar_pendentes(pendentes); st.rerun()

    st.subheader("Usuários cadastrados")
    for usuario, dados in list(usuarios.items()):
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2.4, 1.2, 1.2, 1])
            with c1:
                st.write(f"**{dados.get('nome', usuario)}**")
                st.caption(f"Usuário: {usuario} | E-mail: {dados.get('email','')}")
            with c2:
                perfis = ["analista", "coordenador", "lideranca", "admin"]
                perfil_editado = st.selectbox("Perfil", perfis, index=perfis.index(dados.get("perfil", "analista")) if dados.get("perfil", "analista") in perfis else 0, key=f"perfil_user_{usuario}")
            with c3:
                hubs_opcoes = HUBS + ["TODOS"]
                hub_atual = dados.get("hub", "LPE-12")
                hub_editado = st.selectbox("Hub", hubs_opcoes, index=hubs_opcoes.index(hub_atual) if hub_atual in hubs_opcoes else 0, key=f"hub_user_{usuario}")
            with c4:
                ativo = st.toggle("Ativo", value=bool(dados.get("ativo", True)), key=f"ativo_user_{usuario}")
                if st.button("Salvar", key=f"salvar_user_{usuario}", type="primary", use_container_width=True):
                    dados["perfil"] = perfil_editado; dados["hub"] = hub_editado; dados["ativo"] = ativo
                    salvar_usuarios(usuarios)
                    st.success("Usuário atualizado.")

    with st.expander("Criar usuário manualmente"):
        with st.form("form_criar_usuario_manual"):
            nome = st.text_input("Nome")
            usuario = st.text_input("Usuário")
            email = st.text_input("E-mail")
            senha = st.text_input("Senha", type="password")
            perfil = st.selectbox("Perfil", ["analista", "coordenador", "lideranca", "admin"], key="perfil_manual")
            hub = st.selectbox("Hub", HUBS + ["TODOS"], key="hub_manual")
            criar = st.form_submit_button("Criar usuário", use_container_width=True)
        if criar:
            usuario_norm = normalizar_login(usuario)
            if not nome or not usuario_norm or not email or not senha:
                st.error("Preencha todos os campos.")
            elif usuario_norm in usuarios:
                st.error("Usuário já existe.")
            else:
                usuarios[usuario_norm] = {"nome": nome, "email": normalizar_login(email), "senha_hash": hash_senha(senha), "perfil": perfil, "hub": hub, "ativo": True}
                salvar_usuarios(usuarios)
                st.success("Usuário criado.")
                st.rerun()

# =========================================================
# ESTADO / CACHE
# =========================================================

def carregar_rotas_cache():
    try:
        if ROTAS_CACHE_PATH.exists():
            with open(ROTAS_CACHE_PATH, "rb") as f:
                dados = pickle.load(f)
                if isinstance(dados, dict):
                    return dados
    except Exception:
        return {}
    return {}


def salvar_rotas_cache():
    try:
        rotas = st.session_state.get("rotas_por_hub", {})
        tmp_path = ROTAS_CACHE_PATH.with_suffix(".tmp")
        with open(tmp_path, "wb") as f:
            pickle.dump(rotas, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp_path.replace(ROTAS_CACHE_PATH)
    except Exception:
        pass


def carregar_estado_persistido():
    estado = {}
    try:
        if STATE_PATH.exists():
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                estado = json.load(f)
    except Exception:
        estado = {}
    rotas_cache = carregar_rotas_cache()
    if rotas_cache:
        estado["rotas_por_hub"] = rotas_cache
    return estado if isinstance(estado, dict) else {}


def salvar_estado_persistido():
    try:
        estado = {
            "hub": st.session_state.get("hub", "LPE-12"),
            "tema_escuro": st.session_state.get("tema_escuro", False),
            "hubs": st.session_state.get("hubs", {}),
            "db_links_por_hub": st.session_state.get("db_links_por_hub", {}),
            "contatos_por_hub": st.session_state.get("contatos_por_hub", {}),
        }
        tmp_path = STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, separators=(",", ":"))
        tmp_path.replace(STATE_PATH)
        salvar_rotas_cache()
    except Exception:
        pass


def hub_default():
    return {"Volume": 0, "Entregues": 0, "Pendentes": 0, "Pacotes em Rota de Entrega": 0, "Onhold": 0, "Total de Rotas": 0, "Não Coletadas": 0, "Última Atualização": "Sem atualização"}


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
if "db_links_por_hub" not in st.session_state:
    st.session_state.db_links_por_hub = {hub: "" for hub in HUBS}
if "contatos_por_hub" not in st.session_state:
    st.session_state.contatos_por_hub = {hub: {} for hub in HUBS}
if "consolidado_resultado" not in st.session_state:
    st.session_state.consolidado_resultado = None

if "estado_carregado" not in st.session_state:
    estado = carregar_estado_persistido()
    if estado:
        st.session_state.hub = estado.get("hub", st.session_state.hub)
        st.session_state.tema_escuro = estado.get("tema_escuro", st.session_state.tema_escuro)
        hubs_salvos = estado.get("hubs", {})
        if isinstance(hubs_salvos, dict):
            for h in HUBS:
                if h in hubs_salvos and isinstance(hubs_salvos[h], dict):
                    st.session_state.hubs[h].update(hubs_salvos[h])
        rotas_salvas = estado.get("rotas_por_hub", {})
        if isinstance(rotas_salvas, dict):
            for h in HUBS:
                if h in rotas_salvas and isinstance(rotas_salvas[h], list):
                    st.session_state.rotas_por_hub[h] = rotas_salvas[h]
        db_links = estado.get("db_links_por_hub", {})
        if isinstance(db_links, dict):
            st.session_state.db_links_por_hub.update(db_links)
        contatos = estado.get("contatos_por_hub", {})
        if isinstance(contatos, dict):
            st.session_state.contatos_por_hub.update(contatos)
    st.session_state.estado_carregado = True

params = st.query_params
if "tela" in params:
    st.session_state.tela = params.get("tela", "home")
if "hub" in params:
    hub_param = params.get("hub")
    if hub_param in HUBS:
        st.session_state.hub = hub_param
        if params.get("tela", "hub") == "hub":
            st.session_state.tela = "hub"
if "theme" in params:
    st.session_state.tema_escuro = params.get("theme", "light") == "dark"
if "logado" not in st.session_state:
    st.session_state.logado = False

restaurar_sessao_por_token()
if st.session_state.get("tela") == "logout":
    fazer_logout()
if not st.session_state.get("logado", False):
    render_login()
    st.stop()

# =========================================================
# FUNÇÕES DE DADOS / API
# =========================================================

def log(msg):
    st.session_state.terminal.append(f"> {msg}")


def limpar_ats(texto):
    return [at.strip().upper() for at in texto.replace(",", "\n").replace(";", "\n").splitlines() if at.strip()]


def normalizar_nome(nome):
    texto = str(nome or "").strip().lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(ch for ch in texto if unicodedata.category(ch) != "Mn")
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def limpar_telefone(valor):
    numero = re.sub(r"\D+", "", str(valor or ""))
    if not numero:
        return ""
    if numero.startswith("00"):
        numero = numero[2:]
    if len(numero) in (10, 11):
        numero = "55" + numero
    return numero


def converter_link_google_csv(link):
    link = str(link or "").strip()
    if not link:
        return ""
    if "docs.google.com/spreadsheets" not in link:
        return link
    gid = "0"
    gid_match = re.search(r"[#&?]gid=(\d+)", link)
    if gid_match:
        gid = gid_match.group(1)
    if "/d/e/" in link:
        base = link.split("?")[0]
        if base.endswith("/pubhtml"):
            base = base.replace("/pubhtml", "/pub")
        elif not base.endswith("/pub"):
            base = base.rstrip("/") + "/pub"
        return f"{base}?output=csv&gid={gid}"
    id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link)
    if id_match:
        return f"https://docs.google.com/spreadsheets/d/{id_match.group(1)}/export?format=csv&gid={gid}"
    return link


def carregar_database_contatos(link_database):
    url = converter_link_google_csv(link_database)
    if not url:
        return {}
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    conteudo = resp.content.decode("utf-8-sig", errors="replace")
    leitor = csv.reader(StringIO(conteudo))
    contatos = {}
    for linha in leitor:
        if len(linha) < 9:
            continue
        nome = str(linha[1] or "").strip()
        telefone = limpar_telefone(linha[8] if len(linha) > 8 else "")
        if not nome or not telefone:
            continue
        nome_norm = normalizar_nome(nome)
        if nome_norm and nome_norm not in contatos:
            contatos[nome_norm] = telefone
    return contatos


def carregar_database_arquivo(arquivo):
    if arquivo is None:
        return {}
    nome_arquivo = arquivo.name.lower()
    contatos = {}

    def adicionar_linha(linha):
        if len(linha) < 9:
            return
        nome = str(linha[1] or "").strip()
        telefone = limpar_telefone(linha[8] if len(linha) > 8 else "")
        if not nome or not telefone:
            return
        nome_norm = normalizar_nome(nome)
        if nome_norm and nome_norm not in contatos:
            contatos[nome_norm] = telefone

    if nome_arquivo.endswith((".csv", ".txt")):
        conteudo = arquivo.getvalue().decode("utf-8-sig", errors="replace")
        for linha in csv.reader(StringIO(conteudo)):
            adicionar_linha(linha)
        return contatos
    if nome_arquivo.endswith((".xlsx", ".xlsm")):
        if openpyxl is None:
            raise ValueError("Para ler XLSX, instale openpyxl: pip install openpyxl")
        wb = openpyxl.load_workbook(BytesIO(arquivo.getvalue()), data_only=True, read_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            adicionar_linha(list(row))
        return contatos
    if nome_arquivo.endswith(".xls"):
        if pd is None:
            raise ValueError("Para ler XLS, instale pandas e xlrd: pip install pandas xlrd")
        df = pd.read_excel(BytesIO(arquivo.getvalue()), header=None)
        for _, row in df.iterrows():
            adicionar_linha(row.fillna("").tolist())
        return contatos
    raise ValueError("Formato não suportado. Envie .xlsx, .xls ou .csv")


def buscar_contato_motorista(nome_motorista, contatos):
    nome_norm = normalizar_nome(nome_motorista)
    if not nome_norm or not contatos:
        return ""
    if nome_norm in contatos:
        return contatos[nome_norm]
    partes = set(nome_norm.split())
    melhor_telefone = ""
    melhor_score = 0
    for nome_base, telefone in contatos.items():
        partes_base = set(nome_base.split())
        if not partes or not partes_base:
            continue
        score = len(partes & partes_base) / max(len(partes), len(partes_base))
        if score > melhor_score:
            melhor_score = score
            melhor_telefone = telefone
    return melhor_telefone if melhor_score >= 0.65 else ""


def montar_link_whatsapp(hub, rota):
    telefone = limpar_telefone(rota.get("Telefone", ""))
    motorista = str(rota.get("Motorista", "") or "").strip()
    if not telefone or motorista.upper() == "NÃO BIPADA" or not motorista:
        return ""
    texto = f"""Segue performance do horário!

📊 HUB {hub}
Motorista: {motorista}
AT: {rota.get('AT', '')}
Total: {rota.get('Total', 0)}
Entregues: {rota.get('Entregues', 0)}
Pendentes: {rota.get('Pendentes', 0)}
On Hold: {rota.get('On Hold', 0)}
Performance: {rota.get('Performance %', '0.0%')}
Hora bipada: {rota.get('Hora Bipada', 'Falta bipar')}
Bairro: {rota.get('Bairro', '')}
Cluster: {rota.get('Cluster', '')}

Sua rota ainda possui pacotes pendentes. Precisamos que avance com prioridade.
"""
    return "https://api.whatsapp.com/send?phone=" + telefone + "&text=" + quote(texto)


def aplicar_contatos_nas_rotas(rotas, contatos):
    for rota in rotas:
        rota["Telefone"] = buscar_contato_motorista(rota.get("Motorista", ""), contatos)
    return rotas


def epoch_para_data(valor):
    try:
        valor = int(valor)
        if valor <= 0:
            return "Falta bipar"
        return datetime.fromtimestamp(valor).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return "Falta bipar"


def obter_hora_bipada(rota):
    try:
        assigned_order_count = int(rota.get("assigned_order_count") or 0)
    except Exception:
        assigned_order_count = 0
    try:
        status = int(rota.get("status") or 0)
    except Exception:
        status = 0
    try:
        route_call_up_status = int(rota.get("route_call_up_status") or 0)
    except Exception:
        route_call_up_status = 0
    assigned_time = rota.get("assigned_time")
    if assigned_order_count <= 0 or status == 1 or route_call_up_status == 5 or assigned_time in [None, "", 0, "0"]:
        return "Falta bipar"
    return epoch_para_data(assigned_time)


def parse_hora_bipada_texto(valor):
    texto = str(valor or "").strip()
    if not texto or texto.lower() in ["falta bipar", "não bipada", "nao bipada", "-"]:
        return None
    try:
        return datetime.strptime(texto, "%d/%m/%Y %H:%M:%S")
    except Exception:
        return None


def calcular_percentual_progresso(rota):
    try:
        total = int(rota.get("Total") or 0)
        entregues = int(rota.get("Entregues") or 0)
        if total > 0:
            return min(max((entregues / total) * 100, 0), 100)
    except Exception:
        pass
    try:
        performance = rota.get("Performance", 0)
        if isinstance(performance, str):
            performance = performance.replace("%", "").replace(",", ".").strip()
            valor = float(performance)
            return min(max(valor, 0), 100)
        return min(max(float(performance) * 100, 0), 100)
    except Exception:
        return 0


def calcular_taxa_esperada_entrega(rota, horas_meta=8):
    hora_bipada = parse_hora_bipada_texto(rota.get("Hora Bipada", ""))
    if hora_bipada is None:
        return None
    try:
        horas_passadas = (datetime.now() - hora_bipada).total_seconds() / 3600
        horas_passadas = max(horas_passadas, 0)
        return min((horas_passadas / horas_meta) * 100, 100)
    except Exception:
        return None


def render_barra_percentual(percentual, texto_extra=""):
    try:
        percentual = float(percentual)
    except Exception:
        percentual = 0
    percentual = min(max(percentual, 0), 100)
    return f'''<div class="route-progress"><div class="route-progress-top"><b>{percentual:.1f}%</b><span>{str(texto_extra or '')}</span></div><div class="route-progress-bg"><div class="route-progress-fill" style="width:{percentual:.1f}%;"></div></div></div>'''


def formatar_taxa_esperada(rota):
    taxa = calcular_taxa_esperada_entrega(rota)
    return "-" if taxa is None else f"{taxa:.1f}%"

_thread_local_http = threading.local()

def get_http_session():
    session = getattr(_thread_local_http, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local_http.session = session
    return session


def parse_curl(curl_text):
    texto = str(curl_text or "").replace("\\\n", "\n").replace("\r", "")
    url = ""
    headers = {}
    cookies = ""
    data_raw = None
    match_url = re.search(r"curl\s+'([^']+)'", texto) or re.search(r'curl\s+"([^"]+)"', texto)
    if match_url:
        url = match_url.group(1).strip()
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("-H ") or linha.startswith("-H$") or linha.startswith("-H $"):
            match_header = re.search(r"-H\s+\$?'(.+?)'\s*\\?$", linha) or re.search(r'-H\s+"(.+?)"\s*\\?$', linha)
            if match_header:
                header = match_header.group(1)
                if ":" in header:
                    k, v = header.split(":", 1)
                    headers[k.strip()] = v.strip()
        if linha.startswith("-b "):
            match_cookie = re.search(r"-b\s+'(.+?)'\s*\\?$", linha) or re.search(r'-b\s+"(.+?)"\s*\\?$', linha)
            if match_cookie:
                cookies = match_cookie.group(1).strip()
        if linha.startswith("--data-raw"):
            match_data = re.search(r"--data-raw\s+'(.+?)'\s*\\?$", linha) or re.search(r'--data-raw\s+"(.+?)"\s*\\?$', linha)
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
    session = get_http_session()
    if method == "POST":
        resp = session.post(url, headers=headers, data=data_raw, timeout=25)
    else:
        resp = session.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.json()


def carregar_json_ou_curl(texto):
    texto = str(texto or "").strip()
    if not texto:
        return None
    if texto.startswith("{"):
        return json.loads(texto)
    return executar_curl(texto)


def extrair_lista_v2(resposta):
    if not isinstance(resposta, dict):
        return []
    data = resposta.get("data", resposta)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for chave in ["list", "rows", "items", "route_list", "task_list", "assignment_task_list", "assignment_tasks", "records"]:
            if isinstance(data.get(chave), list):
                return data.get(chave)
        result = data.get("result")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for chave in ["list", "rows", "items", "route_list", "task_list", "assignment_task_list", "records"]:
                if isinstance(result.get(chave), list):
                    return result.get(chave)
    return []


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

    progresso_v2 = st.progress(0)
    status_v2 = st.empty()
    status_v2.info("Consultando V2... iniciando busca de todas as páginas")
    total_api = None
    paginas_estimadas = limite_paginas
    for pagina in range(1, limite_paginas + 1):
        status_v2.info(f"Consultando V2... página {pagina}/{paginas_estimadas}")
        progresso_v2.progress(min(pagina / max(paginas_estimadas, 1), 0.98))
        body = dict(body_base)
        body["pageno"] = pagina
        body["count"] = count
        resposta = executar_curl(curl_v2, body_override=body)
        data = resposta.get("data", {}) if isinstance(resposta, dict) else {}
        lista = extrair_lista_v2(resposta)
        if total_api is None and isinstance(data, dict):
            total_api = data.get("total") or data.get("total_count") or data.get("totalCount") or 0
            try:
                total_api = int(total_api or 0)
            except Exception:
                total_api = 0
            if total_api:
                paginas_estimadas = min(limite_paginas, max(math.ceil(total_api / count), 1))
        if not lista:
            if pagina == 1:
                try:
                    status_v2.info("V2 retornou vazio. Tentando consulta original...")
                    resposta_original = executar_curl(curl_v2)
                    lista_original = extrair_lista_v2(resposta_original)
                    if lista_original:
                        todas.extend(lista_original)
                except Exception:
                    pass
            break
        todas.extend(lista)
        status_v2.info(f"Consultando V2... {len(todas)} rotas recebidas")
        if total_api and len(todas) >= total_api:
            break
        if not total_api and len(lista) < count:
            break
    progresso_v2.progress(1.0)
    status_v2.success(f"V2 concluído: {len(todas)} rotas recebidas")
    time.sleep(0.15)
    progresso_v2.empty(); status_v2.empty()
    log(f"V2 carregado: {len(todas)} rotas")
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
    for chave in ["status", "tracking_status", "order_status", "shipment_status", "parcel_status", "delivery_status"]:
        if chave in pacote:
            try:
                return int(pacote.get(chave))
            except Exception:
                return None
    return None


def get_com_retry(url, headers, timeout=15, tentativas=2):
    ultimo_erro = None
    session = get_http_session()
    for tentativa in range(tentativas):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            ultimo_erro = e
            time.sleep(0.35 * (tentativa + 1))
    raise ultimo_erro


def buscar_metricas_pacotes_por_at(curl_auth, at):
    base_url = base_url_do_curl(curl_auth)
    _, headers, _ = parse_curl(curl_auth)
    total = entregues = onhold = pendentes_status = 0
    count = 200
    for pagina in range(1, 50):
        url = f"{base_url}/spx_delivery/admin/assignment/assignment_task/detail/order/search?pageno={pagina}&count={count}&assignment_task_id={at}"
        resp = get_com_retry(url, headers, timeout=15, tentativas=2)
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
    return {"Total": total, "Entregues": entregues, "On Hold": onhold, "Pendentes": pendentes, "Performance": performance, "Performance %": f"{performance * 100:.1f}%"}


def buscar_metricas_em_lote(curl_auth, mapa_v2, max_workers=20):
    total_ats = len(mapa_v2)
    if total_ats == 0:
        return {}
    progresso = st.progress(0)
    status_txt = st.empty()
    status_txt.info(f"Iniciando consulta de pacotes para {total_ats} ATs...")
    resultados = {}
    inicio_metricas = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tarefas = {executor.submit(buscar_metricas_pacotes_por_at, curl_auth, at): at for at in mapa_v2.keys()}
        concluidas = 0
        for future in as_completed(tarefas):
            at = tarefas[future]
            try:
                resultados[at] = future.result()
            except Exception as e:
                resultados[at] = {"Total": 0, "Entregues": 0, "On Hold": 0, "Pendentes": 0, "Performance": 0, "Performance %": "0.0%"}
                log(f"Erro ao buscar pacotes da AT {at}: {e}")
            concluidas += 1
            progresso.progress(concluidas / total_ats)
            status_txt.info(f"Consultando pacotes: {concluidas}/{total_ats}")
    tempo_total = time.time() - inicio_metricas
    progresso.progress(1.0)
    status_txt.success(f"Pacotes concluídos: {total_ats}/{total_ats} ATs em {tempo_total:.1f}s")
    time.sleep(0.15)
    progresso.empty(); status_txt.empty()
    log(f"Métricas carregadas em {tempo_total:.1f}s")
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
            "AT": at, "Driver ID": rota.get("driver_id", ""), "Motorista": rota.get("driver_name", ""),
            "Modal": rota.get("vehicle_type", ""), "Gaiola": rota.get("corridor_cage", ""), "Bairro": rota.get("neighborhood", ""),
            "Cluster": rota.get("cluster", ""), "Cidade": rota.get("city", ""), "Hora Bipada": obter_hora_bipada(rota),
            "Hora Atribuição": epoch_para_data(rota.get("driver_assigned_time", 0)), "Distância KM": rota.get("total_distance", ""),
            "Paradas": rota.get("stops_number", ""), "Station": rota.get("station_name", ""), "Telefone": "",
            "Total": 0, "Entregues": 0, "On Hold": 0, "Pendentes": 0, "Performance": 0, "Performance %": "0.0%",
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
    nao_coletadas = sum(1 for r in rotas if str(r.get("Hora Bipada", "")).strip().lower() in ["não bipada", "falta bipar"])
    st.session_state.hubs[hub_atual]["Volume"] = total
    st.session_state.hubs[hub_atual]["Total de Rotas"] = qtd_rotas
    st.session_state.hubs[hub_atual]["Pendentes"] = pendentes
    st.session_state.hubs[hub_atual]["Pacotes em Rota de Entrega"] = pendentes
    st.session_state.hubs[hub_atual]["Entregues"] = entregues
    st.session_state.hubs[hub_atual]["Onhold"] = onhold
    st.session_state.hubs[hub_atual]["Não Coletadas"] = nao_coletadas
    st.session_state.hubs[hub_atual]["Última Atualização"] = agora_brasil().strftime("%d/%m/%Y %H:%M")


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
    elif campo_ordenacao == "Taxa esperada":
        rotas_exibicao.sort(key=lambda x: -1 if calcular_taxa_esperada_entrega(x) is None else calcular_taxa_esperada_entrega(x), reverse=ordem_desc)
    elif campo_ordenacao == "Progresso":
        rotas_exibicao.sort(key=lambda x: calcular_percentual_progresso(x), reverse=ordem_desc)
    return rotas_exibicao

# =========================================================
# SIDEBAR CORRIGIDA - NÃO COLOCAR DENTRO DE ASPAS
# =========================================================
menu_dashboard_active = "active" if st.session_state.tela == "home" else ""
menu_consolidado_active = "active" if st.session_state.tela == "consolidado" else ""
menu_admin_active = "active" if st.session_state.tela == "admin" else ""
tema_atual_url = "dark" if st.session_state.get("tema_escuro", False) else "light"
tema_destino_url = "light" if st.session_state.get("tema_escuro", False) else "dark"
tema_label = "Tema claro" if st.session_state.get("tema_escuro", False) else "Tema escuro"
tela_menu_atual = st.session_state.get("tela", "home")
hub_menu_atual = st.session_state.get("hub", "LPE-12")

menu_consolidado_link = ""
if usuario_pode_ver_consolidado():
    menu_consolidado_link = (
        f'<a class="fixed-menu-btn {menu_consolidado_active}" '
        f'href="?{auth_query("tela=consolidado&theme=" + tema_atual_url)}" '
        f'target="_self">Consolidado</a>'
    )

menu_admin_link = ""
if usuario_logado_eh_gestao():
    menu_admin_link = (
        f'<a class="fixed-menu-btn {menu_admin_active}" '
        f'href="?{auth_query("tela=admin&theme=" + tema_atual_url)}" '
        f'target="_self">Usuários</a>'
    )

nome_sidebar = str(st.session_state.get("usuario_nome", "Usuário")).replace("<", "").replace(">", "")
perfil_sidebar = str(st.session_state.get("perfil", "")).upper().replace("<", "").replace(">", "")
hub_sidebar = str(st.session_state.get("hub_permitido", "")).replace("<", "").replace(">", "")

sidebar_css = """
<style>
[data-testid="stSidebar"], [data-testid="collapsedControl"] {display: none !important;}
.fixed-sidebar {position: fixed; left: 0; top: 0; width: 285px; height: 100vh; background: linear-gradient(180deg, #ff5a00 0%, #f0442d 100%); z-index: 999999; padding: 32px 18px; box-sizing: border-box; box-shadow: 4px 0 25px rgba(0,0,0,0.14); overflow:hidden;}
.fixed-sidebar img {width: 225px; display: block; margin: 0 auto 42px auto; filter: brightness(0) invert(1);}
.fixed-menu-btn {display: flex; align-items: center; gap: 12px; width: 100%; color: white !important; padding: 18px 20px; border-radius: 16px; font-weight: 900; margin-bottom: 14px; text-decoration: none !important; font-size: 18px; box-sizing: border-box;}
.fixed-menu-btn.active {background: white; color: #ee4d2d !important;}
.fixed-menu-btn:hover {background: rgba(255,255,255,0.24); text-decoration: none !important;}
.theme-sidebar-title {color: rgba(255,255,255,0.78); font-size: 12px; font-weight: 900; text-transform: uppercase; margin: 30px 0 10px 8px;}
.theme-btn {background: rgba(255,255,255,0.18) !important;}
.fixed-footer {position: absolute; bottom: 28px; left: 18px; right: 18px; padding: 22px; border-radius: 18px; background: rgba(255,255,255,0.18); color: white; font-weight: 900; font-size: 16px; word-break: break-word;}
.block-container {padding-left: 320px !important; padding-top: 2.5rem !important;}
.hub-list-card {position: relative; display: grid; grid-template-columns: 190px minmax(110px,1fr) minmax(110px,1fr) minmax(110px,1fr) minmax(110px,1fr) 170px; align-items: center; gap: 18px; background: white; border-radius: 14px; padding: 24px 22px; margin-bottom: 14px; box-shadow: 0 10px 30px rgba(15,23,42,0.07); border: 1px solid #eef1f5; transition: all .25s ease; min-height: 120px; overflow: hidden;}
.hub-list-card:hover {border: 1.5px solid #ee4d2d; transform: translateY(-2px); box-shadow: 0 18px 40px rgba(238,77,45,0.12);}
.hub-list-card .open-hover {opacity: 0; transform: translateY(8px); transition: all .25s ease; pointer-events: none;}
.hub-list-card:hover .open-hover {opacity: 1; transform: translateY(0); pointer-events: auto;}
.hub-list-name {color: #ee4d2d; font-size: 30px; font-weight: 900;}
.hub-list-icon {width: 56px; height: 56px; border-radius: 50%; background: #fff0ea; display: flex; align-items: center; justify-content: center; font-size: 26px; margin-top: 18px;}
.hub-metric-label {font-weight: 700; color: #0f172a; font-size: 15px;}
.hub-metric-value {font-size: 22px; font-weight: 900; color: #020617; margin-top: 8px;}
.hub-metric-sub {font-size: 14px; color: #475569;}
.hub-open-btn {background: linear-gradient(90deg, #ff5a00, #ee4d2d); color: white !important; padding: 12px 14px; border-radius: 12px; font-weight: 900; text-align: center; text-decoration: none !important; display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 13px; width: 150px; max-width: 150px; box-sizing: border-box;}
.status-list {background:#d9fbe6; color:#07883d; padding:10px 18px; border-radius:999px; font-weight:900; text-align:center;}
.hub-card-extra-info {margin-top: 12px; display: grid; gap: 7px;}
.hub-card-info-line {font-size: 12px; font-weight: 800; color: #475569; line-height: 1.25; white-space: nowrap;}
.hub-card-info-line strong {color: #0f172a; font-weight: 950;}
.hub-card-info-warning {color: #ee4d2d !important;}
.last-update-home {text-align:center; margin-top:28px; color:#64748b; font-size:15px;}
.title {font-size: 34px; line-height: 1.15; margin-bottom: 12px; font-weight: 950;}
.subtitle {font-size: 16px; color: #334155;}
.section-title {font-size: 22px; font-weight: 950; margin: 30px 0 14px 0;}
.dashboard-box {margin: 34px 0 18px 0 !important; padding: 30px 28px !important; border-radius: 18px !important; background: #ffffff; border: 1px solid #e2e8f0; box-shadow: 0 10px 28px rgba(15,23,42,0.06);}
.dashboard-hub {font-size: 34px; font-weight: 950; color: #0f172a;}
.status {display: inline-flex; align-items: center; justify-content: center; min-width: 74px; height: 32px; padding: 0 16px; border-radius: 999px; background: #d9fbe6; color: #07883d; font-size: 13px; font-weight: 950;}
.last-update {font-size: 14px; color: #475569; font-weight: 800;}
.metric {min-height: 100px !important; padding: 22px 26px !important; border-radius: 16px !important; margin-bottom: 16px !important; background: #ffffff; border: 1px solid #e2e8f0; display: flex; align-items: center; gap: 20px; box-shadow: 0 8px 22px rgba(15,23,42,0.05);}
.metric.second {min-height: 100px !important;}
.circle {width: 58px; height: 58px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 28px; flex: 0 0 58px;}
.circle.orange {background:#fff0ea;} .circle.green {background:#d9fbe6;} .circle.yellow {background:#fff7ed;} .circle.purple {background:#f3e8ff;} .circle.blue {background:#dbeafe;}
.metric-title {font-size: 15px; font-weight: 900; color: #0f172a; margin-bottom: 8px;}
.metric-value {font-size: 28px; font-weight: 950; color: #020617; line-height: 1;}
.progress-card {min-height: 112px !important; padding: 22px 26px !important; border-radius: 16px !important; margin-top: 2px !important; margin-bottom: 22px !important; background: #ffffff; border: 1px solid #e2e8f0; box-shadow: 0 8px 22px rgba(15,23,42,0.05);}
.progress-title {font-size: 16px; font-weight: 950; color: #0f172a; margin-bottom: 18px;}
.progress-bg {width: 100%; height: 16px; background: #e5e7eb; border-radius: 999px; overflow: hidden;}
.fill {height: 100%; border-radius: 999px;} .fill.red {background: linear-gradient(90deg,#ef233c,#ff4058);} .fill.green-bar {background: linear-gradient(90deg,#22c55e,#16a34a);} .fill.orange-bar {background: linear-gradient(90deg,#ffb000,#ff5a00);} .fill.blue-bar {background: linear-gradient(90deg,#0066ff,#2f7dff);}
.progress-info {display:flex; justify-content:space-between; gap:12px; margin-top:14px; font-size:14px; font-weight:850; color:#0f172a;}
.ats-header-cell {min-height: 50px; display: flex; align-items: center; background: #ffffff; border-radius: 14px; padding: 0 16px; color: #0f172a; font-size: 15px; font-weight: 950; box-sizing: border-box; white-space: nowrap; margin-top: 4px !important;}
.ats-header-cell.center {justify-content: center;}
.ats-cell {width: 100%; min-height: 50px; display: flex; align-items: center; font-size: 14px; font-weight: 800; padding: 0 16px !important; border-radius: 12px !important; background: #ffffff; border: 1px solid #eef2f7; box-shadow: 0 3px 10px rgba(15,23,42,0.035); margin-bottom: 8px !important; box-sizing: border-box;}
div[data-testid="stButton"] button, div.stButton > button, button[kind="primary"], button[kind="secondary"] {background: linear-gradient(90deg, #ff5a00, #ee4d2d) !important; background-color: #ee4d2d !important; color: #ffffff !important; border: none !important; border-radius: 12px !important; font-weight: 900 !important; min-height: 42px !important; padding: 10px 14px !important; max-width: 190px !important; width: 100% !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; box-sizing: border-box !important;}
div[data-testid="stButton"] button *, div.stButton > button *, button[kind="primary"] *, button[kind="secondary"] * {color: #ffffff !important; fill: #ffffff !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important;}
.wpp-button {display: flex !important; align-items:center; justify-content:center; width: 100% !important; max-width: 100% !important; min-width: 0 !important; height:50px; background: #25D366 !important; color: #ffffff !important; padding: 10px 12px; border-radius: 12px; font-weight: 900; text-align: center; text-decoration: none !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; box-sizing: border-box; margin-bottom:8px;}
.sem-contato {display: block; color: #94a3b8; font-size: 12px; font-weight: 800; text-align: center;}
.route-progress {width: 100%; min-width: 130px;} .route-progress-top {display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; margin-bottom: 6px; color: #0f172a;} .route-progress-top b {color: #0066ff; font-size: 14px;} .route-progress-top span {color: #64748b; font-size: 11px; white-space: nowrap;} .route-progress-bg {width: 100%; height: 8px; background: #e5e7eb; border-radius: 999px; overflow: hidden;} .route-progress-fill {height: 100%; background: linear-gradient(90deg, #22c55e, #16a34a); border-radius: 999px;} .expected-progress .route-progress-fill {background: linear-gradient(90deg, #ffb000, #ff5a00);} .expected-empty {display: block; text-align: center; font-weight: 900; color: #94a3b8;}
.at-code {white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important;} .progress-cell, .expected-progress {min-width: 180px;}
.meta-result-card {background: #ffffff; border: 1px solid #dfe5ee; border-radius: 18px; padding: 26px 28px; margin-bottom: 18px; box-shadow: 0 10px 28px rgba(15,23,42,0.06);} .meta-result-title {font-size: 24px; font-weight: 950; color: #1e293b; margin-bottom: 20px;} .meta-result-line {font-size: 18px; color: #1e293b; margin: 14px 0;} .meta-progress-bg {width: 100%; height: 26px; border-radius: 999px; background: #cbd5e1; overflow: hidden; margin: 16px 0 18px 0;} .meta-progress-fill {height: 100%; min-width: 44px; border-radius: 999px; color: #ffffff; display: flex; align-items: center; justify-content: center; font-weight: 950; font-size: 15px;} .meta-am-fill {background: #5369df;} .meta-pm-fill {background: #20c997;} .meta-consolidado-fill {background: #ff6b1a;} .meta-form-title {font-size: 28px; font-weight: 950; color: #ee4d2d; margin-bottom: 18px;} .meta-section-title {font-size: 22px; font-weight: 950; color: #1e293b; margin-top: 12px; margin-bottom: 8px;}
@media (max-width: 1200px) {.hub-list-card {grid-template-columns: 1fr;} .hub-list-card .open-hover {opacity: 1; pointer-events: auto;}}
</style>
"""

sidebar_items = []
sidebar_items.append('<div class="fixed-sidebar">')
sidebar_items.append(f'<img src="data:image/png;base64,{logo64}">')
sidebar_items.append(
    f'<a class="fixed-menu-btn {menu_dashboard_active}" '
    f'href="?{auth_query("tela=home&theme=" + tema_atual_url)}" '
    f'target="_self">Dashboard</a>'
)
if menu_consolidado_link:
    sidebar_items.append(menu_consolidado_link)
if menu_admin_link:
    sidebar_items.append(menu_admin_link)
sidebar_items.append('<div class="theme-sidebar-title">Aparência</div>')
sidebar_items.append(
    f'<a class="fixed-menu-btn theme-btn" '
    f'href="?{auth_query("tela=" + tela_menu_atual + "&hub=" + hub_menu_atual + "&theme=" + tema_destino_url)}" '
    f'target="_self">{tema_label}</a>'
)
sidebar_items.append(
    f'<a class="fixed-menu-btn" '
    f'href="?{auth_query("tela=logout")}" '
    f'target="_self">Sair</a>'
)
sidebar_items.append(f'<div class="fixed-footer">{nome_sidebar}<br><small>{perfil_sidebar} - {hub_sidebar}</small></div>')
sidebar_items.append('</div>')
sidebar_markup = "".join(sidebar_items)

st.markdown(sidebar_css, unsafe_allow_html=True)
st.markdown(sidebar_markup, unsafe_allow_html=True)

if st.session_state.get("tema_escuro", False):
    html("""
    <style>
    .stApp {background: #0f172a !important; color: #f8fafc !important;}
    .title, .section-title, .dashboard-hub, .metric-title, .metric-value, .progress-title, .progress-info, .progress-info span, .progress-info b, .hub-metric-label, .hub-metric-value, .subtitle, .config-title, .config-subtitle {color: #f8fafc !important;}
    .hub-list-card, .metric, .progress-card, .dashboard-box, .ats-cell, .ats-header, .ats-header-cell, [data-testid="stVerticalBlockBorderWrapper"] {background: #1e293b !important; color: #f8fafc !important; border-color: #334155 !important; box-shadow:none !important;}
    .progress-bg {background: #e5e7eb !important;} .progress-info {color: #f8fafc !important;}
    .hub-metric-sub, .hub-card-info-line, .last-update-home, .last-update, .stMarkdown, p, label, span {color: inherit !important;}
    .hub-card-info-line strong {color: #f8fafc !important;} .hub-card-info-warning {color: #ff7a45 !important;}
    .metric-value, .hub-metric-value, div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] * {color: #f8fafc !important;}
    input, textarea, select {background: #0f172a !important; color: #f8fafc !important; border-color: #334155 !important;}
    .stAlert {background: #1e293b !important; color: #f8fafc !important;}
    .route-progress-top, .route-progress-top span {color: #cbd5e1 !important;} .route-progress-top b {color: #3b82f6 !important;} .route-progress-bg {background: #334155 !important;} .expected-empty {color: #cbd5e1 !important;}
    .meta-result-card {background: #1e293b !important; border-color: #334155 !important; color: #f8fafc !important;} .meta-result-title, .meta-result-line, .meta-result-line b, .meta-section-title, .meta-form-title {color: #f8fafc !important;} .meta-progress-bg {background: #334155 !important;} .wpp-button, .wpp-button * {color: #ffffff !important;} .sem-contato {color: #cbd5e1 !important;}
    </style>
    """)

# =========================================================
# TELAS
# =========================================================

@st.dialog("Detalhes da AT")
def detalhes_at(rota):
    st.markdown(f"### {rota['AT']}")
    st.write(f"**Driver ID:** {rota['Driver ID']}")
    st.write(f"**Nome do motorista:** {rota['Motorista']}")
    st.write(f"**Telefone:** {rota.get('Telefone', '') or 'Sem contato'}")
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
    st.write(f"**Taxa esperada de entrega:** {formatar_taxa_esperada(rota)}")
    link_wpp = montar_link_whatsapp(st.session_state.get("hub", "LPE-12"), rota)
    if link_wpp:
        st.markdown(f'<a class="wpp-button" href="{link_wpp}" target="_blank">WhatsApp</a>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="sem-contato">Sem contato</span>', unsafe_allow_html=True)


def render_header(titulo="Dashboard de Hubs", subtitulo="Acompanhe a performance operacional dos hubs em tempo real."):
    c1, c2, c3 = st.columns([0.7, 5, 2])
    with c1:
        html('<div class="hub-icon header-hub-icon">🏢</div>')
    with c2:
        html(f'<div class="title">{titulo}</div><div class="subtitle">{subtitulo}</div>')
    with c3:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width=220)
        else:
            html("<h2 style='color:#ee4d2d;text-align:right;'>🛍️ Shopee</h2>")


def voltar_home_botao():
    st.session_state.tela = "home"
    st.query_params["auth"] = st.session_state.get("auth_token", "")
    st.query_params["tela"] = "home"
    st.query_params["theme"] = "dark" if st.session_state.get("tema_escuro", False) else "light"
    if "hub" in st.query_params:
        del st.query_params["hub"]
    st.rerun()


def render_home():
    render_header()
    html('<div class="section-title">Selecione um Hub</div>')
    for hub_nome in hubs_visiveis_usuario():
        dados = st.session_state.hubs[hub_nome]
        ultima_atualizacao_hub = dados.get("Última Atualização", "Sem atualização")
        rotas_faltando_carregar = int(dados.get("Não Coletadas", 0) or 0)
        html(f"""
        <div class="hub-list-card"><div><div class="hub-list-name">{hub_nome}</div><div class="hub-list-icon">🏢</div></div>
        <div><div class="hub-metric-label">📦 Volume</div><div class="hub-metric-value">{dados['Volume']:,}</div><div class="hub-metric-sub">pacotes</div></div>
        <div><div class="hub-metric-label">🔗 Rotas</div><div class="hub-metric-value">{dados['Total de Rotas']:,}</div><div class="hub-metric-sub">rotas</div></div>
        <div><div class="hub-metric-label">✅ Entregues</div><div class="hub-metric-value">{dados['Entregues']:,}</div><div class="hub-metric-sub">pacotes</div></div>
        <div><div class="hub-metric-label">⏸️ On Hold</div><div class="hub-metric-value">{dados['Onhold']:,}</div><div class="hub-metric-sub">pacotes</div></div>
        <div><div class="status-list">Ativo</div><div class="hub-card-extra-info"><div class="hub-card-info-line">🕘 Atualizado: <strong>{ultima_atualizacao_hub}</strong></div><div class="hub-card-info-line hub-card-info-warning">🚚 Faltam carregar: <strong>{rotas_faltando_carregar}</strong> rotas</div></div><div class="open-hover" style="margin-top:14px;"><a class="hub-open-btn" href="?{auth_query(f'tela=hub&hub={hub_nome}&theme={tema_atual_url}')}" target="_self">Abrir Dashboard →</a></div></div></div>
        """.replace(",", "."))
    html(f'<div class="last-update-home">🕘 Última atualização: {datetime.now().strftime("%d/%m/%Y %H:%M")}</div>')


def render_dashboard_hub(hub):
    dados = st.session_state.hubs[hub]
    rotas_hub = st.session_state.rotas_por_hub.get(hub, [])
    ultima_atualizacao_hub = dados.get("Última Atualização", "Sem atualização")
    if st.button("← Voltar para Hubs", key=f"voltar_{hub}", type="primary"):
        voltar_home_botao()
    html(f'<div class="dashboard-box"><div style="display:flex;justify-content:space-between;align-items:center;"><div><span class="dashboard-hub">{hub}</span><span class="status" style="float:none;margin-left:18px;">Ativo</span></div><div class="last-update">Última atualização: {ultima_atualizacao_hub} 🔄</div></div></div>')
    volume = dados["Volume"]
    entregues = dados["Entregues"]
    pendentes = dados["Pendentes"]
    performance = entregues / volume if volume else 0
    falta = 1 - performance if volume else 0
    meta_95 = math.ceil(volume * 0.95) if volume else 0
    faltam_meta = max(meta_95 - entregues, 0)
    progresso_meta = entregues / meta_95 if meta_95 else 0
    cor_perf = "green-bar" if performance >= 0.95 else "red"

    metricas_1 = [("📦", "Volume", dados["Volume"], "orange"), ("✅", "Entregues", dados["Entregues"], "green"), ("⏱️", "Pendentes", dados["Pendentes"], "yellow"), ("🚚", "Pacotes em Rota de Entrega", dados["Pacotes em Rota de Entrega"], "purple")]
    for col, (icone, nome, valor, cor) in zip(st.columns(4), metricas_1):
        with col:
            html(f'<div class="metric"><div class="circle {cor}">{icone}</div><div><div class="metric-title">{nome}</div><div class="metric-value">{valor:,}</div></div></div>'.replace(",", "."))
    metricas_2 = [("⏸️", "Onhold", dados["Onhold"], "blue"), ("🔗", "Total de Rotas", dados["Total de Rotas"], "blue"), ("📦", "Não Coletadas", dados["Não Coletadas"], "orange")]
    for col, (icone, nome, valor, cor) in zip(st.columns(3), metricas_2):
        with col:
            html(f'<div class="metric second"><div class="circle {cor}">{icone}</div><div><div class="metric-title">{nome}</div><div class="metric-value">{valor:,}</div></div></div>'.replace(",", "."))
    p1, p2, p3 = st.columns(3)
    with p1:
        html(f'<div class="progress-card"><div class="progress-title">Performance atual</div><div class="progress-bg"><div class="fill {cor_perf}" style="width:{performance*100:.1f}%;"></div></div><div class="progress-info"><span>{entregues:,} / {volume:,} entregues</span><b style="color:#ef233c;">{performance*100:.1f}%</b></div></div>'.replace(",", "."))
    with p2:
        html(f'<div class="progress-card"><div class="progress-title">Meta 95%</div><div class="progress-bg"><div class="fill orange-bar" style="width:{min(progresso_meta,1)*100:.1f}%;"></div></div><div class="progress-info"><span>Meta: {meta_95:,} entregas</span><b>Faltam {faltam_meta:,}</b></div></div>'.replace(",", "."))
    with p3:
        html(f'<div class="progress-card"><div class="progress-title">Falta para concluir 100%</div><div class="progress-bg"><div class="fill blue-bar" style="width:{falta*100:.1f}%;"></div></div><div class="progress-info"><span>Faltam {pendentes:,}</span><b style="color:#0066ff;">{falta*100:.1f}%</b></div></div>'.replace(",", "."))

    if rotas_hub:
        html('<div class="section-title">Lista de ATs do Hub</div>')
        col_ord1, col_ord2 = st.columns([2, 1])
        with col_ord1:
            campo_ordenacao = st.selectbox("Ordenar por", ["Progresso", "Taxa esperada", "Hora Bipada", "Motorista", "AT", "Total", "Entregues", "Pendentes"], index=0, key=f"ordenar_{hub}")
        with col_ord2:
            ordem_desc = st.toggle("Decrescente", value=True, key=f"ordem_{hub}")
        rotas_exibicao = ordenar_rotas(rotas_hub, campo_ordenacao, ordem_desc)
        COLUNAS_ATS = [2.0, 4.0, 2.6, 3.3, 3.1, 1.4, 1.6]
        for col, titulo in zip(st.columns(COLUNAS_ATS), ["AT", "Motorista", "Hora bipada", "Progresso", "Taxa esperada", "Ação", "WhatsApp"]):
            with col:
                html(f'<div class="ats-header-cell center">{titulo}</div>')
        for i, rota in enumerate(rotas_exibicao):
            c_at, c_motorista, c_hora, c_prog, c_esperada, c_btn, c_wpp = st.columns(COLUNAS_ATS)
            progresso_percentual = calcular_percentual_progresso(rota)
            taxa_esperada = calcular_taxa_esperada_entrega(rota)
            progresso_texto = f'{int(rota.get("Entregues") or 0)}/{int(rota.get("Total") or 0)}'
            with c_at: html(f'<div class="ats-cell at-code">{rota["AT"]}</div>')
            with c_motorista: html(f'<div class="ats-cell">{rota["Motorista"] or "Sem motorista"}</div>')
            with c_hora: html(f'<div class="ats-cell">{rota["Hora Bipada"]}</div>')
            with c_prog: html(f'<div class="ats-cell">{render_barra_percentual(progresso_percentual, progresso_texto)}</div>')
            with c_esperada:
                html('<div class="ats-cell"><span class="expected-empty">-</span></div>' if taxa_esperada is None else f'<div class="ats-cell expected-progress">{render_barra_percentual(taxa_esperada, "8h")}</div>')
            with c_btn:
                if st.button("Abrir", key=f"detalhe_{hub}_{i}_{rota['AT']}", use_container_width=True, type="primary"):
                    detalhes_at(rota)
            with c_wpp:
                link_wpp = montar_link_whatsapp(hub, rota)
                st.markdown(f'<a class="wpp-button" href="{link_wpp}" target="_blank">WhatsApp</a>' if link_wpp else '<span class="sem-contato">Sem contato</span>', unsafe_allow_html=True)
    else:
        st.info(f"Nenhuma rota carregada para {hub}. Abra a aba Configuração e atualize este hub.")


def render_configuracao_hub(hub):
    html(f'<div class="config-title">⚙️ Configuração Operacional - {hub}</div><div class="config-subtitle">Cole o Bash LIST, o Bash V2 e as ATs específicas deste hub.</div>')
    bash_list = st.text_area("Bash LIST / AUTH", height=180, key=f"bash_list_{hub}")
    bash_v2 = st.text_area("Bash V2", height=240, key=f"bash_v2_{hub}")
    ats_texto = st.text_area("ATs para buscar", height=170, placeholder="Cole uma AT por linha ou separadas por vírgula.", key=f"ats_{hub}")
    link_database = st.text_input("Link da planilha Database do Hub", value=st.session_state.db_links_por_hub.get(hub, ""), placeholder="Cole o link da aba Database. Coluna B = Nome | Coluna I = Telefone", key=f"database_{hub}")
    st.session_state.db_links_por_hub[hub] = link_database
    arquivo_database = st.file_uploader("Anexar arquivo Database do Hub (.xlsx, .xls ou .csv)", type=["xlsx", "xls", "csv"], key=f"database_file_{hub}")
    col_a, col_c, col_b = st.columns([1, 1, 2])
    with col_a:
        iniciar = st.button(f"🚀 Atualizar {hub}", use_container_width=True, key=f"iniciar_{hub}", type="primary")
    with col_c:
        carregar_contatos_btn = st.button("📱 Carregar contatos", use_container_width=True, key=f"carregar_contatos_{hub}", type="primary")
    with col_b:
        somente_v2 = st.checkbox("Buscar todas as páginas do V2", value=True, key=f"somente_v2_{hub}")

    if carregar_contatos_btn:
        try:
            if arquivo_database is not None:
                contatos_database = carregar_database_arquivo(arquivo_database)
            elif link_database.strip():
                contatos_database = carregar_database_contatos(link_database)
            else:
                raise ValueError("Anexe o arquivo Database ou informe o link da Database.")
            st.session_state.contatos_por_hub[hub] = contatos_database
            if st.session_state.rotas_por_hub.get(hub):
                st.session_state.rotas_por_hub[hub] = aplicar_contatos_nas_rotas(st.session_state.rotas_por_hub[hub], contatos_database)
            salvar_estado_persistido()
            st.success(f"Contatos carregados para {hub}: {len(contatos_database)}")
        except Exception as e:
            st.error(f"Não foi possível carregar a Database: {e}")
            log(f"Não foi possível carregar contatos do hub {hub}: {e}")

    if iniciar:
        st.session_state.terminal = []
        ats = limpar_ats(ats_texto)
        try:
            log(f"Iniciando processo do hub {hub}...")
            log(f"ATs digitadas: {len(ats)}")
            contatos_database = st.session_state.contatos_por_hub.get(hub, {}) or {}
            if arquivo_database is not None or link_database.strip():
                try:
                    log("Carregando Database de contatos...")
                    contatos_database = carregar_database_arquivo(arquivo_database) if arquivo_database is not None else carregar_database_contatos(link_database)
                    st.session_state.contatos_por_hub[hub] = contatos_database
                    log(f"Contatos carregados: {len(contatos_database)}")
                except Exception as e:
                    contatos_database = st.session_state.contatos_por_hub.get(hub, {}) or {}
                    log(f"Não foi possível carregar a Database: {e}")
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
            lista_v2 = buscar_todas_paginas_v2(bash_v2) if somente_v2 else (carregar_json_ou_curl(bash_v2).get("data", {}).get("list", []))
            log(f"Rotas recebidas do V2: {len(lista_v2)}")
            mapa_v2 = processar_rotas_v2(lista_v2, ats)
            log(f"ATs encontradas no V2: {len(mapa_v2)}")
            if not mapa_v2:
                st.warning("Nenhuma AT encontrada no V2.")
            else:
                log("Consultando pacotes por AT em modo seguro...")
                metricas_lote = buscar_metricas_em_lote(bash_list, mapa_v2, max_workers=6)
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
                rotas = aplicar_contatos_nas_rotas(rotas, contatos_database)
                st.session_state.rotas_por_hub[hub] = rotas
                atualizar_hub_com_rotas(hub, rotas)
                salvar_estado_persistido()
                st.success(f"Atualização do {hub} finalizada.")
                st.session_state.tela = "hub"; st.session_state.hub = hub
                token_atual = st.session_state.get("auth_token") or st.query_params.get("auth", "")
                if token_atual: st.query_params["auth"] = token_atual
                st.query_params["tela"] = "hub"; st.query_params["hub"] = hub; st.query_params["theme"] = "dark" if st.session_state.get("tema_escuro", False) else "light"
                time.sleep(1); st.rerun()
        except Exception as e:
            st.error(f"Erro ao processar: {e}")
            log(f"ERRO: {e}")
    for linha in st.session_state.terminal[-40:]:
        st.code(linha, language="text")


def fmt_numero(valor):
    try:
        return f"{int(valor):,}".replace(",", ".")
    except Exception:
        return "0"


def calcular_meta_bloco(total, entregues, meta_percentual):
    total = max(int(total or 0), 0)
    entregues = max(int(entregues or 0), 0)
    meta_percentual = float(meta_percentual or 95)
    target_meta = int(total * (meta_percentual / 100)) if total else 0
    target_real = (entregues / total * 100) if total else 0
    faltam = max(target_meta - entregues, 0)
    margem_nao_entregar = max(total - target_meta, 0)
    return {"total": total, "entregues": entregues, "meta_percentual": meta_percentual, "target_meta": target_meta, "target_real": target_real, "faltam": faltam, "margem_nao_entregar": margem_nao_entregar}


def render_card_meta(titulo, dados, classe_fill):
    total = dados["total"]; meta_percentual = dados["meta_percentual"]; target_meta = dados["target_meta"]; target_real = dados["target_real"]; entregues = dados["entregues"]; faltam = dados["faltam"]; margem = dados["margem_nao_entregar"]
    largura = min(max(target_real, 0), 100)
    html(f'<div class="meta-result-card"><div class="meta-result-title">📊 {titulo}: Volumetria {fmt_numero(total)}</div><div class="meta-result-line">❗ Target Meta {meta_percentual:.0f}%: Qtd pacotes <b>{fmt_numero(target_meta)}</b></div><div class="meta-result-line">📦 Target Real {target_real:.2f}% Pacotes entregues até o momento: <b>{fmt_numero(entregues)}</b></div><div class="meta-progress-bg"><div class="meta-progress-fill {classe_fill}" style="width:{largura:.1f}%;">{target_real:.1f}%</div></div><div class="meta-result-line">📉 Faltam <b>{fmt_numero(faltam)}</b> pacotes para atingir a meta</div><div class="meta-result-line">⚠️ Para alcançar {meta_percentual:.0f}% você pode NÃO entregar até <b>{fmt_numero(margem)}</b> pacotes</div></div>')


def calcular_resultado_consolidado(total_am, entregues_am, total_pm, entregues_pm, meta_percentual):
    am = calcular_meta_bloco(total_am, entregues_am, meta_percentual)
    pm = calcular_meta_bloco(total_pm, entregues_pm, meta_percentual)
    consolidado = calcular_meta_bloco(am["total"] + pm["total"], am["entregues"] + pm["entregues"], meta_percentual)
    return {"AM": am, "PM": pm, "Consolidado": consolidado}


@st.dialog("Calcular Meta")
def dialog_calcular_meta():
    hub_dialog = st.session_state.get("hub", "LPE-12")
    html(f'<div class="meta-form-title">🎯 Calcular Meta - {hub_dialog}</div>')
    html('<div class="meta-section-title">🌅 AM</div>')
    col_am1, col_am2 = st.columns(2)
    with col_am1: total_am = st.number_input("Total AM", min_value=0, step=1, placeholder="Ex: 12452", key="calc_total_am")
    with col_am2: entregues_am = st.number_input("Entregues AM", min_value=0, step=1, placeholder="Ex: 10718", key="calc_entregues_am")
    html('<div class="meta-section-title">🌆 PM</div>')
    col_pm1, col_pm2 = st.columns(2)
    with col_pm1: total_pm = st.number_input("Total PM", min_value=0, step=1, placeholder="Ex: 9446", key="calc_total_pm")
    with col_pm2: entregues_pm = st.number_input("Entregues PM", min_value=0, step=1, placeholder="Ex: 2014", key="calc_entregues_pm")
    meta_percentual = st.number_input("Meta desejada", min_value=1.0, max_value=100.0, value=95.0, step=1.0, key="calc_meta_percentual")
    if st.button("Calcular AM / PM / Consolidado", use_container_width=True, type="primary"):
        st.session_state.consolidado_resultado = calcular_resultado_consolidado(total_am, entregues_am, total_pm, entregues_pm, meta_percentual)
        st.rerun()
    if st.button("Fechar", use_container_width=True):
        st.rerun()


def render_consolidado():
    render_header(titulo="Consolidado", subtitulo="Calcule a meta operacional por janela AM, PM e consolidado.")
    if st.button("🎯 Calcular Meta", type="primary"):
        dialog_calcular_meta()
    resultado = st.session_state.get("consolidado_resultado")
    if not resultado:
        st.info("Clique em Calcular Meta para informar os dados AM/PM e gerar o consolidado.")
        return
    render_card_meta("AM", resultado["AM"], "meta-am-fill")
    render_card_meta("PM", resultado["PM"], "meta-pm-fill")
    render_card_meta("Consolidado", resultado["Consolidado"], "meta-consolidado-fill")

# =========================================================
# ROTEAMENTO FINAL
# =========================================================
if st.session_state.tela == "home":
    render_home()
elif st.session_state.tela == "admin":
    render_admin_usuarios()
elif st.session_state.tela == "config":
    render_header(titulo="Configurações", subtitulo="Configurações gerais do sistema.")
    st.info("Use a barra lateral para alternar entre tema claro e tema escuro.")
elif st.session_state.tela == "consolidado":
    if usuario_pode_ver_consolidado():
        render_consolidado()
    else:
        st.error("Seu perfil não possui acesso ao consolidado.")
else:
    hub_atual = st.session_state.hub
    if not usuario_pode_acessar_hub(hub_atual):
        render_acesso_negado(hub_atual)
        st.stop()
    render_header(titulo=f"Dashboard {hub_atual}", subtitulo=f"Performance operacional em tempo real do hub {hub_atual}.")
    aba_dashboard, aba_config = st.tabs([f"📊 Dashboard {hub_atual}", f"⚙️ Configuração {hub_atual}"])
    with aba_dashboard:
        render_dashboard_hub(hub_atual)
    with aba_config:
        render_configuracao_hub(hub_atual)
