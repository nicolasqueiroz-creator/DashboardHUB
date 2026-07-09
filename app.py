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
import os
from io import StringIO, BytesIO
from textwrap import dedent

try:
    from supabase import create_client
except Exception:
    create_client = None

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

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")
SUPABASE_TABLE_HUBS = "hubs_status"
SUPABASE_TABLE_ROTAS = "rotas_cache"
SUPABASE_TABLE_USUARIOS = "usuarios"
SUPABASE_TABLE_PENDENTES = "cadastros_pendentes"
SUPABASE_TABLE_SESSOES = "sessoes"
SUPABASE_TABLE_HISTORICO_DRIVER = "historico_driver"
SUPABASE_TABLE_FECHAMENTO_DRIVER = "historico_fechamento_driver"
FECHAMENTO_AUTO_HORA = 23
FECHAMENTO_AUTO_MINUTO = 50

def safe_log(msg):
    try:
        st.session_state.terminal.append(f"> {msg}")
    except Exception:
        pass


def get_supabase():
    if not create_client or not SUPABASE_URL or not SUPABASE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def config_default():
    # Mantém "ats" como legado para não perder configurações antigas já salvas.
    return {"bash_list": "", "bash_v2": "", "ats": "", "ats_am": "", "ats_pm": "", "db_link": ""}










def montar_dados_hub_para_salvar(hub):
    dados = dict(st.session_state.hubs.get(hub, {}))
    config = dict(st.session_state.config_por_hub.get(hub, config_default()))
    config["db_link"] = st.session_state.db_links_por_hub.get(hub, config.get("db_link", ""))
    dados["__config"] = config
    dados["__contatos"] = st.session_state.contatos_por_hub.get(hub, {})
    return dados


def aplicar_dados_hub_carregados(hub, dados):
    if not isinstance(dados, dict):
        return

    config = dados.get("__config", {})
    if isinstance(config, dict):
        atual = st.session_state.config_por_hub.get(hub, config_default())
        ats_legado = config.get("ats", atual.get("ats", ""))
        atual.update({
            "bash_list": config.get("bash_list", atual.get("bash_list", "")),
            "bash_v2": config.get("bash_v2", atual.get("bash_v2", "")),
            "ats": ats_legado,
            "ats_am": config.get("ats_am", atual.get("ats_am", ats_legado)),
            "ats_pm": config.get("ats_pm", atual.get("ats_pm", "")),
            "db_link": config.get("db_link", atual.get("db_link", "")),
        })
        st.session_state.config_por_hub[hub] = atual
        st.session_state.db_links_por_hub[hub] = atual.get("db_link", "")

    contatos = dados.get("__contatos", {})
    if isinstance(contatos, dict):
        st.session_state.contatos_por_hub[hub] = contatos

    metricas = {k: v for k, v in dados.items() if k not in ["__config", "__contatos"]}
    if metricas:
        st.session_state.hubs[hub].update(metricas)


def salvar_hub_supabase(hub):
    try:
        sb = get_supabase()
        if not sb:
            safe_log("Supabase não configurado. Salvando apenas localmente.")
            return False

        atualizado_por = st.session_state.get("usuario_login", "")
        dados_hub = montar_dados_hub_para_salvar(hub)

        sb.table(SUPABASE_TABLE_HUBS).upsert({
            "hub": hub,
            "dados": dados_hub,
            "atualizado_por": atualizado_por,
            "atualizado_em": agora_brasil().isoformat()
        }, on_conflict="hub").execute()

        sb.table(SUPABASE_TABLE_ROTAS).upsert({
            "hub": hub,
            "rotas": st.session_state.rotas_por_hub.get(hub, []),
            "atualizado_por": atualizado_por,
            "atualizado_em": agora_brasil().isoformat()
        }, on_conflict="hub").execute()

        return True

    except Exception as e:
        safe_log(f"Erro ao salvar {hub} no Supabase: {e}")
        return False


def salvar_todos_hubs_supabase():
    ok = True
    for hub in HUBS:
        ok = salvar_hub_supabase(hub) and ok
    return ok


def carregar_supabase():
    try:
        sb = get_supabase()
        if not sb:
            safe_log("Supabase não configurado. Carregando dados locais.")
            return False

        hubs_resp = sb.table(SUPABASE_TABLE_HUBS).select("*").execute()
        for item in hubs_resp.data or []:
            hub = item.get("hub")
            if hub in HUBS:
                aplicar_dados_hub_carregados(hub, item.get("dados", {}))

        rotas_resp = sb.table(SUPABASE_TABLE_ROTAS).select("*").execute()
        for item in rotas_resp.data or []:
            hub = item.get("hub")
            if hub in HUBS and isinstance(item.get("rotas"), list):
                st.session_state.rotas_por_hub[hub] = item["rotas"]

        return True

    except Exception as e:
        safe_log(f"Erro ao carregar Supabase: {e}")
        return False


def sincronizar_widgets_config_hub(hub):
    """Atualiza os campos da aba Configuração com o que acabou de vir do Supabase."""
    try:
        config = st.session_state.config_por_hub.get(hub, config_default())
        ats_legado = config.get("ats", "")
        st.session_state[f"bash_list_{hub}"] = config.get("bash_list", "")
        st.session_state[f"bash_v2_{hub}"] = config.get("bash_v2", "")
        st.session_state[f"ats_{hub}"] = ats_legado
        st.session_state[f"ats_am_{hub}"] = config.get("ats_am", ats_legado)
        st.session_state[f"ats_pm_{hub}"] = config.get("ats_pm", "")
        st.session_state[f"database_{hub}"] = config.get("db_link", st.session_state.db_links_por_hub.get(hub, ""))
    except Exception as e:
        safe_log(f"Erro ao sincronizar campos do hub {hub}: {e}")


def carregar_hub_supabase(hub, atualizar_widgets=False):
    """
    Carrega apenas um hub do Supabase.
    Isso evita usar cache antigo da sessão e impede sobrescrever alterações feitas por outro analista.
    """
    try:
        if hub not in HUBS:
            return False

        sb = get_supabase()
        if not sb:
            return False

        hubs_resp = (
            sb.table(SUPABASE_TABLE_HUBS)
            .select("*")
            .eq("hub", hub)
            .execute()
        )
        for item in hubs_resp.data or []:
            if isinstance(item.get("dados"), dict):
                aplicar_dados_hub_carregados(hub, item.get("dados", {}))

        rotas_resp = (
            sb.table(SUPABASE_TABLE_ROTAS)
            .select("*")
            .eq("hub", hub)
            .execute()
        )
        for item in rotas_resp.data or []:
            if isinstance(item.get("rotas"), list):
                st.session_state.rotas_por_hub[hub] = item["rotas"]

        if atualizar_widgets:
            sincronizar_widgets_config_hub(hub)

        st.session_state[f"ultimo_refresh_supabase_{hub}"] = agora_brasil().isoformat()
        return True

    except Exception as e:
        safe_log(f"Erro ao carregar {hub} no Supabase: {e}")
        return False


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


def _carregar_json_local(path, padrao=None):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                dados = json.load(f)
                return dados if isinstance(dados, dict) else (padrao or {})
    except Exception:
        pass
    return padrao or {}


def _salvar_json_local(path, dados):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _supabase_ler_mapa(tabela, coluna_chave):
    """
    Lê registros do Supabase.
    Aceita dois formatos de tabela:
    1) usuario/token + dados(jsonb)
    2) colunas abertas: usuario, nome, email, senha_hash, perfil, hub, ativo...
    """
    sb = get_supabase()
    if not sb:
        return None
    try:
        resp = sb.table(tabela).select("*").execute()
        resultado = {}
        for item in resp.data or []:
            chave = item.get(coluna_chave)
            if not chave:
                continue

            if isinstance(item.get("dados"), dict):
                dados = item.get("dados", {})
            else:
                dados = {
                    k: v for k, v in item.items()
                    if k not in [coluna_chave, "id", "created_at", "updated_at", "atualizado_em"]
                }

            if isinstance(dados, dict):
                resultado[str(chave)] = dados
        return resultado
    except Exception as e:
        safe_log(f"Erro ao carregar {tabela} do Supabase: {e}")
        return None


def _linha_supabase_generica(tabela, coluna_chave, chave, dados):
    """
    Monta uma linha compatível com dois modelos de tabela:
    - Modelo JSONB: chave + dados
    - Modelo aberto: usuario/nome/email/senha_hash/perfil/hub/ativo...
    A tentativa JSONB é feita primeiro; se a tabela não tiver a coluna dados,
    as funções abaixo fazem fallback automático para o modelo aberto.
    """
    dados = dados if isinstance(dados, dict) else {}
    return {
        coluna_chave: str(chave),
        "dados": dados,
        "atualizado_em": agora_brasil().isoformat()
    }


def _linha_supabase_aberta(tabela, coluna_chave, chave, dados):
    """Monta linha para tabelas Supabase com colunas abertas."""
    dados = dados if isinstance(dados, dict) else {}
    linha = {coluna_chave: str(chave)}

    if tabela == SUPABASE_TABLE_USUARIOS:
        linha.update({
            "nome": dados.get("nome", ""),
            "email": dados.get("email", ""),
            "senha_hash": dados.get("senha_hash", ""),
            "perfil": dados.get("perfil", "analista"),
            "hub": dados.get("hub", ""),
            "ativo": bool(dados.get("ativo", True)),
            "atualizado_em": agora_brasil().isoformat(),
        })
        return linha

    if tabela == SUPABASE_TABLE_PENDENTES:
        linha.update({
            "nome": dados.get("nome", ""),
            "email": dados.get("email", ""),
            "senha_hash": dados.get("senha_hash", ""),
            "perfil_solicitado": dados.get("perfil_solicitado", "analista"),
            "hub": dados.get("hub", ""),
            "status": dados.get("status", "pendente"),
            "criado_em": dados.get("criado_em", ""),
            "atualizado_em": agora_brasil().isoformat(),
        })
        return linha

    if tabela == SUPABASE_TABLE_SESSOES:
        linha.update({
            "usuario": dados.get("usuario", ""),
            "criado_em": dados.get("criado_em", ""),
            "atualizado_em": agora_brasil().isoformat(),
        })
        return linha

    linha.update(dados)
    linha["atualizado_em"] = agora_brasil().isoformat()
    return linha


def _supabase_salvar_mapa(tabela, coluna_chave, mapa):
    """
    Sincroniza registros no Supabase.
    Funciona com tabela no formato JSONB (chave + dados) e também com tabela
    em colunas abertas, como usuarios(usuario, nome, email, senha_hash...).
    """
    sb = get_supabase()
    if not sb:
        return False

    mapa = mapa if isinstance(mapa, dict) else {}

    linhas_json = []
    linhas_abertas = []
    for chave, dados in mapa.items():
        if not chave or not isinstance(dados, dict):
            continue
        linhas_json.append(_linha_supabase_generica(tabela, coluna_chave, chave, dados))
        linhas_abertas.append(_linha_supabase_aberta(tabela, coluna_chave, chave, dados))

    # 1ª tentativa: modelo JSONB: chave + dados
    try:
        sb.table(tabela).delete().neq(coluna_chave, "__registro_inexistente__").execute()
        if linhas_json:
            sb.table(tabela).upsert(linhas_json, on_conflict=coluna_chave).execute()
        return True
    except Exception as e_json:
        safe_log(f"Fallback Supabase para {tabela} em colunas abertas: {e_json}")

    # 2ª tentativa: modelo aberto
    try:
        sb.table(tabela).delete().neq(coluna_chave, "__registro_inexistente__").execute()
        if linhas_abertas:
            sb.table(tabela).upsert(linhas_abertas, on_conflict=coluna_chave).execute()
        return True
    except Exception as e_aberto:
        safe_log(f"Erro ao salvar {tabela} no Supabase: {e_aberto}")
        return False


def _supabase_upsert_um_registro(tabela, coluna_chave, chave, dados):
    """Salva um único registro sem apagar os demais. Aceita JSONB ou colunas abertas."""
    sb = get_supabase()
    if not sb:
        return False

    try:
        sb.table(tabela).upsert(
            _linha_supabase_generica(tabela, coluna_chave, chave, dados),
            on_conflict=coluna_chave
        ).execute()
        return True
    except Exception as e_json:
        safe_log(f"Fallback upsert Supabase para {tabela} em colunas abertas: {e_json}")

    try:
        sb.table(tabela).upsert(
            _linha_supabase_aberta(tabela, coluna_chave, chave, dados),
            on_conflict=coluna_chave
        ).execute()
        return True
    except Exception as e_aberto:
        safe_log(f"Erro ao salvar registro em {tabela}: {e_aberto}")
        return False


def salvar_usuario_individual(usuario, dados):
    """Cria/atualiza um usuário específico no Supabase e mantém backup local."""
    usuario = normalizar_login(usuario)
    if not usuario or not isinstance(dados, dict):
        return False

    ok_supabase = _supabase_upsert_um_registro(SUPABASE_TABLE_USUARIOS, "usuario", usuario, dados)

    usuarios_local = _carregar_json_local(USERS_PATH, {})
    if not isinstance(usuarios_local, dict):
        usuarios_local = {}
    usuarios_local[usuario] = dados
    _salvar_json_local(USERS_PATH, usuarios_local)

    return bool(ok_supabase)

def _corrigir_usuarios_legado(usuarios):
    usuarios = usuarios if isinstance(usuarios, dict) else {}
    alterou = False
    for _, dados in usuarios.items():
        if isinstance(dados, dict) and "senha" in dados and "senha_hash" not in dados:
            dados["senha_hash"] = hash_senha(dados.get("senha", ""))
            dados.pop("senha", None)
            alterou = True
    return usuarios, alterou


def carregar_usuarios():
    # Prioridade: Supabase. Fallback: arquivo local apenas se Supabase não estiver configurado/indisponível.
    try:
        usuarios_supabase = _supabase_ler_mapa(SUPABASE_TABLE_USUARIOS, "usuario")
        if usuarios_supabase is not None:
            usuarios_local = _carregar_json_local(USERS_PATH, {})
            if not isinstance(usuarios_local, dict):
                usuarios_local = {}

            # Se o Supabase ainda estiver vazio, usa o backup local antes de recriar apenas o admin.
            if not usuarios_supabase and usuarios_local:
                usuarios_supabase = usuarios_local
                salvar_usuarios(usuarios_supabase)

            if not usuarios_supabase:
                usuarios_supabase = usuario_padrao_admin()
                salvar_usuarios(usuarios_supabase)
                return usuarios_supabase

            usuarios_supabase, alterou = _corrigir_usuarios_legado(usuarios_supabase)
            if alterou:
                salvar_usuarios(usuarios_supabase)
            return usuarios_supabase
    except Exception as e:
        safe_log(f"Falha ao carregar usuários no Supabase: {e}")

    usuarios = _carregar_json_local(USERS_PATH, {})
    if not usuarios:
        usuarios = usuario_padrao_admin()
        salvar_usuarios(usuarios)
        return usuarios
    usuarios, alterou = _corrigir_usuarios_legado(usuarios)
    if alterou:
        salvar_usuarios(usuarios)
    return usuarios


def salvar_usuarios(usuarios):
    usuarios = usuarios if isinstance(usuarios, dict) else {}
    if _supabase_salvar_mapa(SUPABASE_TABLE_USUARIOS, "usuario", usuarios):
        # Mantém um backup local, mas a fonte oficial passa a ser o Supabase.
        _salvar_json_local(USERS_PATH, usuarios)
        return True
    _salvar_json_local(USERS_PATH, usuarios)
    return False


def carregar_pendentes():
    try:
        pendentes = _supabase_ler_mapa(SUPABASE_TABLE_PENDENTES, "usuario")
        if pendentes is not None:
            return pendentes
    except Exception as e:
        safe_log(f"Falha ao carregar pendentes no Supabase: {e}")
    return _carregar_json_local(PENDING_USERS_PATH, {})


def salvar_pendentes(pendentes):
    pendentes = pendentes if isinstance(pendentes, dict) else {}
    if _supabase_salvar_mapa(SUPABASE_TABLE_PENDENTES, "usuario", pendentes):
        _salvar_json_local(PENDING_USERS_PATH, pendentes)
        return
    _salvar_json_local(PENDING_USERS_PATH, pendentes)


def carregar_sessoes():
    """
    Sessões ficam locais para não derrubar login ao trocar de página/aba.
    Usuários e pendentes continuam no Supabase; sessão é só controle do navegador.
    """
    return _carregar_json_local(SESSIONS_PATH, {})


def salvar_sessoes(sessoes):
    sessoes = sessoes if isinstance(sessoes, dict) else {}
    _salvar_json_local(SESSIONS_PATH, sessoes)


def encontrar_usuario_por_login(login_digitado, usuarios):
    login = normalizar_login(login_digitado)
    for usuario, dados in usuarios.items():
        if login == normalizar_login(usuario) or login == normalizar_login(dados.get("email", "")):
            return usuario, dados
    return None, None


def criar_sessao(usuario, dados):
    token = uuid.uuid4().hex
    usuario_norm = normalizar_login(usuario)

    sessoes = carregar_sessoes()
    sessoes[token] = {
        "usuario": usuario_norm,
        "criado_em": agora_brasil().strftime("%d/%m/%Y %H:%M")
    }
    salvar_sessoes(sessoes)

    st.session_state.auth_token = token
    st.session_state.logado = True
    st.session_state.usuario_login = usuario_norm
    st.session_state.usuario_nome = dados.get("nome", usuario_norm)
    st.session_state.usuario_email = dados.get("email", "")
    st.session_state.perfil = dados.get("perfil", "analista")
    st.session_state.hub_permitido = dados.get("hub", "")

    st.query_params["auth"] = token
    st.query_params["u"] = usuario_norm
    return token


def restaurar_sessao_por_token():
    """
    Mantém o login ao navegar pelo app.

    Ordem de restauração:
    1) session_state já logado.
    2) token salvo em sessoes.json local.
    3) fallback pelo parâmetro u da URL, validando que o usuário existe e está ativo.
       Esse fallback evita voltar ao login caso o Streamlit reinicie a sessão no mobile.
    """
    if st.session_state.get("logado", False):
        token_atual = st.session_state.get("auth_token") or st.query_params.get("auth", "")
        usuario_atual = st.session_state.get("usuario_login") or st.query_params.get("u", "")
        if token_atual:
            st.query_params["auth"] = token_atual
        if usuario_atual:
            st.query_params["u"] = usuario_atual
        return True

    token = st.session_state.get("auth_token") or st.query_params.get("auth", "")
    usuario_url = normalizar_login(st.query_params.get("u", ""))

    usuarios = carregar_usuarios()

    usuario = ""
    if token:
        sessoes = carregar_sessoes()
        sessao = sessoes.get(token, {})
        usuario = normalizar_login(sessao.get("usuario", ""))

    if not usuario and usuario_url:
        usuario = usuario_url

    if not usuario:
        return False

    dados = usuarios.get(usuario)
    if not dados or not dados.get("ativo", False):
        return False

    if not token:
        token = uuid.uuid4().hex

    sessoes = carregar_sessoes()
    sessoes[token] = {
        "usuario": usuario,
        "criado_em": sessoes.get(token, {}).get("criado_em", agora_brasil().strftime("%d/%m/%Y %H:%M"))
    }
    salvar_sessoes(sessoes)

    st.session_state.auth_token = token
    st.session_state.logado = True
    st.session_state.usuario_login = usuario
    st.session_state.usuario_nome = dados.get("nome", usuario)
    st.session_state.usuario_email = dados.get("email", "")
    st.session_state.perfil = dados.get("perfil", "analista")
    st.session_state.hub_permitido = dados.get("hub", "")

    st.query_params["auth"] = token
    st.query_params["u"] = usuario
    return True


def auth_query(extra=""):
    token = st.session_state.get("auth_token") or st.query_params.get("auth", "")
    usuario = st.session_state.get("usuario_login") or st.query_params.get("u", "")

    partes = []
    if token:
        partes.append(f"auth={token}")
    if usuario:
        partes.append(f"u={usuario}")
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
                        st.query_params.clear(); st.query_params["auth"] = token; st.query_params["u"] = st.session_state.usuario_login; st.query_params["tela"] = "hub"; st.query_params["hub"] = st.session_state.hub_permitido
                    else:
                        st.session_state.tela = "home"
                        st.query_params.clear(); st.query_params["auth"] = token; st.query_params["u"] = st.session_state.usuario_login; st.query_params["tela"] = "home"
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
                    pendentes[usuario_norm] = {"nome": nome.strip(), "email": email_norm, "senha_hash": hash_senha(senha), "perfil_solicitado": "analista", "hub": hub, "status": "pendente", "criado_em": agora_brasil().strftime("%d/%m/%Y %H:%M")}
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
            email_norm = normalizar_login(email)

            if not nome.strip() or not usuario_norm or not email_norm or not senha:
                st.error("Preencha todos os campos.")
            elif usuario_norm in usuarios:
                st.error("Usuário já existe.")
            elif any(email_norm == normalizar_login(d.get("email", "")) for d in usuarios.values() if isinstance(d, dict)):
                st.error("Esse e-mail já está cadastrado.")
            else:
                novo_usuario = {
                    "nome": nome.strip(),
                    "email": email_norm,
                    "senha_hash": hash_senha(senha),
                    "perfil": perfil,
                    "hub": hub,
                    "ativo": True,
                }
                usuarios[usuario_norm] = novo_usuario
                if salvar_usuario_individual(usuario_norm, novo_usuario):
                    st.success("Usuário criado e salvo no Supabase.")
                    time.sleep(0.8)
                    st.rerun()
                else:
                    st.error("Não consegui salvar no Supabase. Confira SUPABASE_URL, SUPABASE_KEY e a tabela usuarios.")

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


def salvar_estado_persistido(hub_para_salvar=None):
    try:
        estado = {
            "hub": st.session_state.get("hub", "LPE-12"),
            "tema_escuro": st.session_state.get("tema_escuro", False),
            "hubs": st.session_state.get("hubs", {}),
            "db_links_por_hub": st.session_state.get("db_links_por_hub", {}),
            "contatos_por_hub": st.session_state.get("contatos_por_hub", {}),
            "config_por_hub": st.session_state.get("config_por_hub", {}),
        }

        tmp_path = STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, separators=(",", ":"))

        tmp_path.replace(STATE_PATH)
        salvar_rotas_cache()

        hub_alvo = hub_para_salvar or st.session_state.get("hub")
        if hub_alvo in HUBS:
            salvar_hub_supabase(hub_alvo)

    except Exception as e:
        try:
            log(f"Erro ao salvar estado persistido: {e}")
        except Exception:
            pass

def hub_default():
    return {
        "Volume": 0, "Entregues": 0, "Pendentes": 0, "Pacotes em Rota de Entrega": 0,
        "Onhold": 0, "Total de Rotas": 0, "Não Coletadas": 0, "Última Atualização": "Sem atualização",
        "Volume AM": 0, "Entregues AM": 0, "Pendentes AM": 0, "Onhold AM": 0, "Rotas AM": 0, "Performance AM": 0,
        "Volume PM": 0, "Entregues PM": 0, "Pendentes PM": 0, "Onhold PM": 0, "Rotas PM": 0, "Performance PM": 0,
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
if "config_por_hub" not in st.session_state:
    st.session_state.config_por_hub = {hub: config_default() for hub in HUBS}
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

        config_salva = estado.get("config_por_hub", {})
        if isinstance(config_salva, dict):
            for h in HUBS:
                if isinstance(config_salva.get(h), dict):
                    atual = st.session_state.config_por_hub.get(h, config_default())
                    atual.update(config_salva[h])
                    st.session_state.config_por_hub[h] = atual

        db_links = estado.get("db_links_por_hub", {})
        if isinstance(db_links, dict):
            st.session_state.db_links_por_hub.update(db_links)
            for h in HUBS:
                if st.session_state.db_links_por_hub.get(h):
                    st.session_state.config_por_hub[h]["db_link"] = st.session_state.db_links_por_hub[h]

        contatos = estado.get("contatos_por_hub", {})
        if isinstance(contatos, dict):
            st.session_state.contatos_por_hub.update(contatos)

    carregar_supabase()
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
            
        return ( 
            datetime.fromtimestamp(valor, tz=ZoneInfo("UTC"))
        .astimezone(FUSO_BRASIL) 
        .strftime("%d/%m/%Y %H:%M:%S")
        )
        
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
        hora_bipada = hora_bipada.replace(tzinfo=FUSO_BRASIL)
        horas_passadas = (agora_brasil() - hora_bipada).total_seconds() / 3600
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


def _esperada(rota):
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


def normalizar_status_pacote(valor):
    """Normaliza status numérico/texto da API para comparação segura."""
    texto = str(valor or "").strip().lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(ch for ch in texto if unicodedata.category(ch) != "Mn")
    texto = texto.replace(" ", "_").replace("-", "_")
    texto = re.sub(r"[^a-z0-9_]+", "", texto)
    texto = re.sub(r"_+", "_", texto).strip("_")
    return texto


def extrair_status_pacote(pacote):
    """
    Retorna todos os status encontrados no pacote.

    Importante: algumas respostas trazem o código numérico em `status` e o nome
    operacional em outro campo, como `status_name`, `status_desc` ou similar.
    Por isso não podemos parar no primeiro campo numérico, senão status como
    SP_Ready_Collection ficam fora do cálculo.
    """
    if not isinstance(pacote, dict):
        return []

    chaves_status = [
        "status", "status_name", "status_text", "status_desc", "status_description",
        "tracking_status", "tracking_status_name", "tracking_status_text",
        "order_status", "order_status_name", "order_status_text",
        "shipment_status", "shipment_status_name", "shipment_status_text",
        "parcel_status", "parcel_status_name", "parcel_status_text",
        "delivery_status", "delivery_status_name", "delivery_status_text",
        "display_status", "display_status_name", "display_status_text",
    ]

    valores = []
    for chave in chaves_status:
        if chave in pacote and pacote.get(chave) not in [None, ""]:
            valores.append(pacote.get(chave))

    # Fallback: procura qualquer chave que contenha status no nome.
    for chave, valor in pacote.items():
        if "status" in str(chave).lower() and valor not in [None, ""] and valor not in valores:
            valores.append(valor)

    return valores


def status_pacote_eh_entregue(status_valores):
    status_valores = status_valores if isinstance(status_valores, list) else [status_valores]
    textos_entregues = {
        "delivered",
        "delivery_done",
        "completed",
        "complete",
        "signed",
        "pod_uploaded",
        "sp_ready_collection",
        "sp_collection_collected",
        "collection_collected",
        "ready_collection",
    }

    for valor in status_valores:
        try:
            if int(valor) == 4:
                return True
        except Exception:
            pass

        texto = normalizar_status_pacote(valor)
        if texto in textos_entregues:
            return True

    return False


def status_pacote_eh_onhold(status_valores):
    """
    Identifica On Hold por texto explícito do status.

    Importante: não usar mais `status == 5` sozinho aqui, porque em algumas
    respostas da Shopee esse código aparece em pacotes que não são On Hold e
    infla absurdamente o indicador.
    """
    status_valores = status_valores if isinstance(status_valores, list) else [status_valores]
    textos_onhold = {
        "on_hold", "onhold", "hold", "held",
        "occurrence", "ocorrencia", "ocorrencia_aberta",
        "sp_on_hold", "sp_onhold", "problem", "exception",
    }

    for valor in status_valores:
        texto = normalizar_status_pacote(valor)
        if texto in textos_onhold:
            return True

    return False


def pacote_tem_ocorrencia(pacote):
    """
    Usa campos de ocorrência do detalhe do pacote para identificar On Hold.
    Na tela da Shopee existe a coluna "Número de ocorrências"; dependendo da
    resposta da API, esse valor pode vir com nomes diferentes.
    """
    if not isinstance(pacote, dict):
        return False

    chaves_ocorrencia = [
        "occurrence_count", "occurrences_count", "occurrence_num", "occurrence_number",
        "exception_count", "exceptions_count", "exception_num", "exception_number",
        "on_hold_count", "onhold_count", "hold_count",
        "abnormal_count", "abnormal_num", "issue_count", "problem_count",
        "num_occurrences", "numero_ocorrencias", "numero_de_ocorrencias",
        "occurrence", "occurrences", "exceptions", "abnormal_list", "problem_list",
    ]

    for chave in chaves_ocorrencia:
        if chave not in pacote:
            continue
        valor = pacote.get(chave)
        if valor in [None, "", "-", 0, "0"]:
            continue
        if isinstance(valor, (list, tuple, dict)):
            return len(valor) > 0
        try:
            return int(valor) > 0
        except Exception:
            texto = normalizar_status_pacote(valor)
            if texto and texto not in {"0", "none", "null", "false", "na", "n_a"}:
                return True

    # Fallback: qualquer campo que mencione ocorrência/exceção/on hold e tenha valor positivo.
    for chave, valor in pacote.items():
        chave_norm = normalizar_status_pacote(chave)
        if not any(palavra in chave_norm for palavra in ["occurrence", "ocorrencia", "exception", "onhold", "on_hold", "hold"]):
            continue
        if valor in [None, "", "-", 0, "0"]:
            continue
        if isinstance(valor, (list, tuple, dict)):
            if len(valor) > 0:
                return True
            continue
        try:
            if int(valor) > 0:
                return True
        except Exception:
            texto = normalizar_status_pacote(valor)
            if texto and texto not in {"0", "none", "null", "false", "na", "n_a"}:
                return True

    return False


def status_pacote_eh_pendente(status_valores):
    status_valores = status_valores if isinstance(status_valores, list) else [status_valores]
    textos_pendentes = {"pending", "delivering", "in_delivery", "out_for_delivery", "assigned"}

    for valor in status_valores:
        try:
            if int(valor) == 2:
                return True
        except Exception:
            pass

        texto = normalizar_status_pacote(valor)
        if texto in textos_pendentes:
            return True

    return False


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
            if status_pacote_eh_entregue(status):
                entregues += 1
            elif pacote_tem_ocorrencia(pacote) or status_pacote_eh_onhold(status):
                onhold += 1
            elif status_pacote_eh_pendente(status):
                pendentes_status += 1
        if len(pacotes) < count:
            break

    # Regra operacional: tudo que não foi entregue nem On Hold fica pendente.
    # Isso evita que status não mapeados sumam do fechamento da rota.
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


def processar_rotas_v2(lista_v2, ats_desejadas=None, mapa_janelas=None):
    ats_set = set(ats_desejadas or [])
    mapa_janelas = mapa_janelas or {}
    mapa = {}
    for rota in lista_v2:
        at = str(rota.get("assignment_task_id", "")).upper().strip()
        if not at:
            continue
        if ats_set and at not in ats_set:
            continue
        try:
            total_v2 = int(rota.get("order_count") or rota.get("assigned_order_count") or 0)
        except Exception:
            total_v2 = 0
        try:
            status_v2 = int(rota.get("status") or 0)
        except Exception:
            status_v2 = 0
        try:
            complete_time_v2 = int(rota.get("complete_time") or 0)
        except Exception:
            complete_time_v2 = 0

        rota_concluida_v2 = status_v2 == 5 and complete_time_v2 > 0

        mapa[at] = {
            "AT": at, "Janela": mapa_janelas.get(at, "-"), "Driver ID": rota.get("driver_id", ""), "Motorista": rota.get("driver_name", ""),
            "Modal": rota.get("vehicle_type", ""), "Gaiola": rota.get("corridor_cage", ""), "Bairro": rota.get("neighborhood", ""),
            "Cluster": rota.get("cluster", ""), "Cidade": rota.get("city", ""), "Hora Bipada": obter_hora_bipada(rota),
            "Hora Atribuição": epoch_para_data(rota.get("driver_assigned_time", 0)), "Distância KM": rota.get("total_distance", ""),
            "Paradas": rota.get("stops_number", ""), "Station": rota.get("station_name", ""), "Telefone": "",
            "Total V2": total_v2,
            "Status V2": status_v2,
            "Complete Time V2": complete_time_v2,
            "Rota Concluida V2": rota_concluida_v2,
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


def calcular_metricas_janela(rotas, janela):
    rotas_janela = [r for r in rotas if str(r.get("Janela", "")).upper() == str(janela).upper()]
    total = sum(int(r.get("Total") or 0) for r in rotas_janela)
    entregues = sum(int(r.get("Entregues") or 0) for r in rotas_janela)
    pendentes = sum(int(r.get("Pendentes") or 0) for r in rotas_janela)
    onhold = sum(int(r.get("On Hold") or 0) for r in rotas_janela)
    performance = entregues / total if total else 0
    return {
        "Volume": total,
        "Entregues": entregues,
        "Pendentes": pendentes,
        "Onhold": onhold,
        "Rotas": len(rotas_janela),
        "Performance": performance,
    }


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

    metricas_am = calcular_metricas_janela(rotas, "AM")
    metricas_pm = calcular_metricas_janela(rotas, "PM")
    st.session_state.hubs[hub_atual]["Volume AM"] = metricas_am["Volume"]
    st.session_state.hubs[hub_atual]["Entregues AM"] = metricas_am["Entregues"]
    st.session_state.hubs[hub_atual]["Pendentes AM"] = metricas_am["Pendentes"]
    st.session_state.hubs[hub_atual]["Onhold AM"] = metricas_am["Onhold"]
    st.session_state.hubs[hub_atual]["Rotas AM"] = metricas_am["Rotas"]
    st.session_state.hubs[hub_atual]["Performance AM"] = metricas_am["Performance"]
    st.session_state.hubs[hub_atual]["Volume PM"] = metricas_pm["Volume"]
    st.session_state.hubs[hub_atual]["Entregues PM"] = metricas_pm["Entregues"]
    st.session_state.hubs[hub_atual]["Pendentes PM"] = metricas_pm["Pendentes"]
    st.session_state.hubs[hub_atual]["Onhold PM"] = metricas_pm["Onhold"]
    st.session_state.hubs[hub_atual]["Rotas PM"] = metricas_pm["Rotas"]
    st.session_state.hubs[hub_atual]["Performance PM"] = metricas_pm["Performance"]

    st.session_state.hubs[hub_atual]["Última Atualização"] = agora_brasil().strftime("%d/%m/%Y %H:%M")


# =========================================================
# HISTÓRICO / INTELIGÊNCIA OPERACIONAL
# =========================================================

def motorista_valido_para_historico(rota):
    nome = str(rota.get("Motorista", "") or "").strip()
    if not nome:
        return False
    nome_upper = nome.upper()
    bloqueados = ["NÃO BIPADA", "NAO BIPADA", "SEM MOTORISTA", "SEM DRIVER", "SEM ATRIBUIÇÃO", "SEM ATRIBUICAO"]
    return nome_upper not in bloqueados


def performance_rota_percentual(rota):
    try:
        total = int(rota.get("Total") or 0)
        entregues = int(rota.get("Entregues") or 0)
        if total > 0:
            return round((entregues / total) * 100, 2)
    except Exception:
        pass
    try:
        perf = rota.get("Performance %", rota.get("Performance", 0))
        if isinstance(perf, str):
            return round(float(perf.replace("%", "").replace(",", ".").strip()), 2)
        perf_float = float(perf)
        if perf_float <= 1:
            perf_float *= 100
        return round(perf_float, 2)
    except Exception:
        return 0.0


def montar_linhas_historico_driver(hub, rotas, limite_ofensor=95.0):
    hoje = agora_brasil().date()
    iso = hoje.isocalendar()
    linhas = []

    for rota in rotas or []:
        if not motorista_valido_para_historico(rota):
            continue

        at = str(rota.get("AT", "") or "").strip().upper()
        if not at:
            continue

        total = int(rota.get("Total") or 0)
        entregues = int(rota.get("Entregues") or 0)
        on_hold = int(rota.get("On Hold") or 0)
        pendentes = int(rota.get("Pendentes") or 0)
        performance = performance_rota_percentual(rota)
        hora_bipada = str(rota.get("Hora Bipada", "") or "").strip()
        nao_coletada = hora_bipada.lower() in ["não bipada", "nao bipada", "falta bipar", ""]
        ofensora = bool(total > 0 and performance < float(limite_ofensor or 95.0))

        historico_id = f"{hoje.isoformat()}_{hub}_{at}"
        linhas.append({
            "historico_id": historico_id,
            "data": hoje.isoformat(),
            "semana": int(iso.week),
            "mes": int(hoje.month),
            "ano": int(hoje.year),
            "hub": hub,
            "janela": str(rota.get("Janela", "-") or "-"),
            "driver_id": str(rota.get("Driver ID", "") or ""),
            "motorista": str(rota.get("Motorista", "") or "").strip().title(),
            "at": at,
            "gaiola": str(rota.get("Gaiola", "") or ""),
            "cluster": str(rota.get("Cluster", "") or ""),
            "modalidade": str(rota.get("Modal", "") or ""),
            "volume": total,
            "entregues": entregues,
            "on_hold": on_hold,
            "pendentes": pendentes,
            "performance": performance,
            "ofensora": ofensora,
            "nao_coletada": nao_coletada,
            "atualizado_por": st.session_state.get("usuario_login", ""),
            "atualizado_em": agora_brasil().isoformat(),
        })

    return linhas


def salvar_historico_driver_supabase(hub, rotas, limite_ofensor=95.0):
    """Salva snapshot diário por AT/motorista no Supabase sem apagar histórico anterior."""
    try:
        sb = get_supabase()
        if not sb:
            safe_log("Supabase não configurado. Histórico não salvo na nuvem.")
            return False

        linhas = montar_linhas_historico_driver(hub, rotas, limite_ofensor=limite_ofensor)
        if not linhas:
            safe_log("Histórico: nenhuma rota válida para salvar.")
            return False

        # Envia em lotes para evitar payload grande.
        for i in range(0, len(linhas), 300):
            lote = linhas[i:i+300]
            sb.table(SUPABASE_TABLE_HISTORICO_DRIVER).upsert(lote, on_conflict="historico_id").execute()

        safe_log(f"Histórico salvo: {len(linhas)} rotas do {hub}.")
        return True
    except Exception as e:
        safe_log(f"Erro ao salvar histórico do {hub}: {e}")
        return False




def salvar_fechamento_driver_supabase(hub, rotas, limite_ofensor=95.0, data_fechamento=None, origem="manual"):
    """
    Salva o fechamento oficial do dia em uma tabela separada.
    A chave historico_id evita duplicidade: se salvar novamente no mesmo dia,
    a linha da mesma AT é atualizada em vez de duplicada.
    """
    try:
        sb = get_supabase()
        if not sb:
            safe_log("Supabase não configurado. Fechamento não salvo na nuvem.")
            return False

        data_ref = data_fechamento or agora_brasil().date()
        linhas_base = montar_linhas_historico_driver(hub, rotas, limite_ofensor=limite_ofensor)
        linhas = []

        for linha in linhas_base:
            at = str(linha.get("at", "") or "").strip().upper()
            if not at:
                continue

            linha["data"] = data_ref.isoformat()
            iso = data_ref.isocalendar()
            linha["semana"] = int(iso.week)
            linha["mes"] = int(data_ref.month)
            linha["ano"] = int(data_ref.year)
            linha["historico_id"] = f"{data_ref.isoformat()}_{hub}_{at}"
            linha["origem"] = origem
            linha["fechado_por"] = st.session_state.get("usuario_login", "")
            linha["fechado_em"] = agora_brasil().isoformat()
            linha["atualizado_em"] = agora_brasil().isoformat()
            linhas.append(linha)

        if not linhas:
            safe_log("Fechamento: nenhuma rota válida para salvar.")
            return False

        for i in range(0, len(linhas), 300):
            lote = linhas[i:i+300]
            sb.table(SUPABASE_TABLE_FECHAMENTO_DRIVER).upsert(lote, on_conflict="historico_id").execute()

        chave_sessao = f"fechamento_salvo_{hub}_{data_ref.isoformat()}"
        st.session_state[chave_sessao] = True
        safe_log(f"Fechamento salvo: {len(linhas)} rotas do {hub} em {data_ref.strftime('%d/%m/%Y')}.")
        return True

    except Exception as e:
        safe_log(f"Erro ao salvar fechamento do {hub}: {e}")
        return False


def fechamento_ja_existe_supabase(hub, data_ref=None):
    """Verifica se já existe fechamento salvo para o hub/data."""
    try:
        sb = get_supabase()
        if not sb:
            return False
        data_ref = data_ref or agora_brasil().date()
        resp = (
            sb.table(SUPABASE_TABLE_FECHAMENTO_DRIVER)
            .select("historico_id")
            .eq("hub", hub)
            .eq("data", data_ref.isoformat())
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        safe_log(f"Erro ao verificar fechamento do {hub}: {e}")
        return False


def auto_salvar_fechamento_driver(hub, limite_ofensor=95.0):
    """
    Tenta salvar automaticamente no fim do dia.
    Observação: em Streamlit isso roda quando o app estiver aberto ou receber interação.
    Para execução garantida às 23:50, é necessário agendador externo/Streamlit sempre ativo.
    """
    try:
        agora = agora_brasil()
        if agora.hour < FECHAMENTO_AUTO_HORA or (agora.hour == FECHAMENTO_AUTO_HORA and agora.minute < FECHAMENTO_AUTO_MINUTO):
            return False

        chave_sessao = f"fechamento_salvo_{hub}_{agora.date().isoformat()}"
        if st.session_state.get(chave_sessao):
            return False

        rotas = st.session_state.rotas_por_hub.get(hub, [])
        if not rotas:
            return False

        if fechamento_ja_existe_supabase(hub, agora.date()):
            st.session_state[chave_sessao] = True
            return False

        return salvar_fechamento_driver_supabase(
            hub,
            rotas,
            limite_ofensor=limite_ofensor,
            data_fechamento=agora.date(),
            origem="automatico"
        )
    except Exception as e:
        safe_log(f"Erro no fechamento automático do {hub}: {e}")
        return False


def carregar_fechamento_driver_supabase(hub, dias=35):
    try:
        sb = get_supabase()
        if not sb:
            return []
        data_min = (agora_brasil().date() - pd.Timedelta(days=int(dias))).isoformat() if pd is not None else None
        query = sb.table(SUPABASE_TABLE_FECHAMENTO_DRIVER).select("*").eq("hub", hub)
        if data_min:
            query = query.gte("data", data_min)
        resp = query.order("data", desc=True).execute()
        return resp.data or []
    except Exception as e:
        safe_log(f"Erro ao carregar fechamento do {hub}: {e}")
        return []


def carregar_historico_driver_supabase(hub, dias=35):
    try:
        sb = get_supabase()
        if not sb:
            return []
        data_min = (agora_brasil().date() - pd.Timedelta(days=int(dias))).isoformat() if pd is not None else None
        query = sb.table(SUPABASE_TABLE_HISTORICO_DRIVER).select("*").eq("hub", hub)
        if data_min:
            query = query.gte("data", data_min)
        resp = query.order("data", desc=True).execute()
        return resp.data or []
    except Exception as e:
        safe_log(f"Erro ao carregar histórico do {hub}: {e}")
        return []


def preparar_df_historico(hub, dias):
    if pd is None:
        return None
    dados = carregar_fechamento_driver_supabase(hub, dias=dias)
    df = pd.DataFrame(dados)
    if df.empty:
        return df

    for col in ["volume", "entregues", "on_hold", "pendentes", "semana", "mes", "ano"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    if "performance" in df.columns:
        df["performance"] = pd.to_numeric(df["performance"], errors="coerce").fillna(0.0)
    if "data" in df.columns:
        df["data"] = pd.to_datetime(df["data"], errors="coerce").dt.date
    if "ofensora" in df.columns:
        df["ofensora"] = df["ofensora"].fillna(False).astype(bool)
    if "nao_coletada" in df.columns:
        df["nao_coletada"] = df["nao_coletada"].fillna(False).astype(bool)

    # Proteção contra duplicidade histórica:
    # se o fechamento for salvo mais de uma vez, mantém só o registro mais recente
    # para a mesma data/hub/AT. Isso evita contar a mesma rota duas vezes.
    try:
        if "atualizado_em" in df.columns:
            df["_ordem_atualizacao"] = pd.to_datetime(df["atualizado_em"], errors="coerce")
        elif "fechado_em" in df.columns:
            df["_ordem_atualizacao"] = pd.to_datetime(df["fechado_em"], errors="coerce")
        else:
            df["_ordem_atualizacao"] = pd.Timestamp.min

        chaves_dedup = [c for c in ["data", "hub", "at"] if c in df.columns]
        if chaves_dedup:
            df = (
                df.sort_values("_ordem_atualizacao")
                  .drop_duplicates(subset=chaves_dedup, keep="last")
                  .drop(columns=["_ordem_atualizacao"], errors="ignore")
            )
    except Exception:
        pass

    return df




def montar_indice_confiabilidade(df):
    """Monta o índice de confiabilidade dos motoristas com base no histórico selecionado."""
    if pd is None or df is None or df.empty:
        return pd.DataFrame() if pd is not None else None

    base = df.copy()
    if "driver_id" not in base.columns:
        base["driver_id"] = ""
    if "motorista" not in base.columns:
        base["motorista"] = ""

    for col in ["volume", "entregues", "on_hold", "pendentes"]:
        if col not in base.columns:
            base[col] = 0
    if "performance" not in base.columns:
        base["performance"] = 0.0
    if "ofensora" not in base.columns:
        base["ofensora"] = False

    agrupado = (
        base.groupby(["driver_id", "motorista"], dropna=False)
        .agg(
            rotas=("at", "nunique"),
            dias_trabalhados=("data", "nunique"),
            ultima_data=("data", "max"),
            volume=("volume", "sum"),
            entregues=("entregues", "sum"),
            on_hold=("on_hold", "sum"),
            pendentes=("pendentes", "sum"),
            ofensores=("ofensora", "sum"),
            performance_media=("performance", "mean"),
        )
        .reset_index()
    )

    agrupado["performance_media"] = pd.to_numeric(agrupado["performance_media"], errors="coerce").fillna(0.0)
    agrupado["rotas"] = pd.to_numeric(agrupado["rotas"], errors="coerce").fillna(0).astype(int)
    agrupado["ofensores"] = pd.to_numeric(agrupado["ofensores"], errors="coerce").fillna(0).astype(int)
    agrupado["volume"] = pd.to_numeric(agrupado["volume"], errors="coerce").fillna(0).astype(int)
    agrupado["pendentes"] = pd.to_numeric(agrupado["pendentes"], errors="coerce").fillna(0).astype(int)
    agrupado["on_hold"] = pd.to_numeric(agrupado["on_hold"], errors="coerce").fillna(0).astype(int)

    agrupado["% ofensores"] = ((agrupado["ofensores"] / agrupado["rotas"].replace(0, 1)) * 100).round(1)
    agrupado["% pendentes"] = ((agrupado["pendentes"] / agrupado["volume"].replace(0, 1)) * 100).round(1)
    agrupado["% on hold"] = ((agrupado["on_hold"] / agrupado["volume"].replace(0, 1)) * 100).round(1)

    def calcular_linha(row):
        perf = float(row.get("performance_media", 0) or 0)
        ofensores = int(row.get("ofensores", 0) or 0)
        rotas = max(int(row.get("rotas", 0) or 0), 1)
        pct_of = float(row.get("% ofensores", 0) or 0)
        pct_pend = float(row.get("% pendentes", 0) or 0)
        pct_oh = float(row.get("% on hold", 0) or 0)

        nota = 100.0
        # Performance pesa mais: quanto mais distante de 98%, maior a perda.
        nota -= max(0.0, 98.0 - perf) * 1.7
        # Reincidência pesa muito, porque o objetivo é apoiar o planejamento.
        nota -= min(32.0, ofensores * 7.5)
        nota -= min(18.0, pct_of * 0.30)
        # Pendentes recorrentes e On Hold também entram, mas com peso menor.
        nota -= min(16.0, pct_pend * 0.90)
        nota -= min(8.0, pct_oh * 0.35)
        # Bônus pequeno para consistência: várias rotas, sem ofensor e média alta.
        if perf >= 98.0 and ofensores == 0 and rotas >= 3:
            nota += min(5.0, rotas * 0.5)

        nota = max(0.0, min(100.0, nota))
        if nota >= 90:
            faixa = "🟢 Confiável"
            recomendacao = "Prioridade positiva para escala"
        elif nota >= 75:
            faixa = "🟡 Atenção"
            recomendacao = "Acompanhar na operação"
        else:
            faixa = "🔴 Alto risco"
            recomendacao = "Avaliar antes de escalar"
        return pd.Series([round(nota, 1), faixa, recomendacao])

    agrupado[["Índice", "Classificação", "Recomendação"]] = agrupado.apply(calcular_linha, axis=1)
    agrupado["performance_media"] = agrupado["performance_media"].round(1)
    agrupado = agrupado.sort_values(["Índice", "performance_media", "rotas"], ascending=[False, False, False])
    return agrupado



def persistir_config_widgets_hub(hub):
    """Preserva os campos de Configuração antes de trocar de tela."""
    try:
        if hub not in HUBS:
            return
        atual = st.session_state.config_por_hub.get(hub, config_default()).copy()
        bash_list = st.session_state.get(f"bash_list_{hub}", atual.get("bash_list", ""))
        bash_v2 = st.session_state.get(f"bash_v2_{hub}", atual.get("bash_v2", ""))
        ats_am = st.session_state.get(f"ats_am_{hub}", atual.get("ats_am", atual.get("ats", "")))
        ats_pm = st.session_state.get(f"ats_pm_{hub}", atual.get("ats_pm", ""))
        db_link = st.session_state.get(f"database_{hub}", atual.get("db_link", st.session_state.db_links_por_hub.get(hub, "")))
        st.session_state.config_por_hub[hub] = {
            "bash_list": bash_list,
            "bash_v2": bash_v2,
            "ats": "\n".join([ats_am, ats_pm]).strip(),
            "ats_am": ats_am,
            "ats_pm": ats_pm,
            "db_link": db_link,
        }
        st.session_state.db_links_por_hub[hub] = db_link
    except Exception as e:
        safe_log(f"Erro ao preservar configuração do hub {hub}: {e}")


def render_nav_hub_unica(hub):
    """Navegação principal sem st.tabs/st.radio: só uma tela é renderizada por vez."""
    chave = f"hub_subtela_{hub}"
    if chave not in st.session_state:
        st.session_state[chave] = "dashboard"

    html("""
    <style>
    .hub-nav-spacer { margin: 4px 0 18px 0; }
    div[data-testid="stHorizontalBlock"] button {
        width: 100% !important;
        min-height: 46px !important;
        border-radius: 16px !important;
        font-weight: 950 !important;
        letter-spacing: .1px !important;
        transition: all .18s ease-in-out !important;
        box-shadow: 0 8px 22px rgba(0,0,0,.18) !important;
        overflow: visible !important;
        white-space: nowrap !important;
    }
    div[data-testid="stHorizontalBlock"] button[kind="secondary"] {
        background: rgba(15, 23, 42, .92) !important;
        color: #ffffff !important;
        border: 1px solid rgba(255,255,255,.22) !important;
    }
    div[data-testid="stHorizontalBlock"] button[kind="secondary"] * {
        color: #ffffff !important;
        opacity: 1 !important;
        visibility: visible !important;
    }
    div[data-testid="stHorizontalBlock"] button[kind="secondary"]:hover {
        background: rgba(255,90,0,.24) !important;
        border-color: rgba(255,90,0,.95) !important;
        color: #ff7a3d !important;
        transform: translateY(-1px);
    }
    div[data-testid="stHorizontalBlock"] button[kind="secondary"]:hover * {
        color: #ff7a3d !important;
    }
    div[data-testid="stHorizontalBlock"] button[kind="primary"] {
        background: linear-gradient(90deg,#ff5a00,#f0442d) !important;
        color: #ffffff !important;
        border: 1px solid rgba(255,90,0,.95) !important;
        box-shadow: 0 10px 24px rgba(255,90,0,.25) !important;
    }
    div[data-testid="stHorizontalBlock"] button[kind="primary"] * {
        color: #ffffff !important;
        opacity: 1 !important;
        visibility: visible !important;
    }
    </style>
    <div class="hub-nav-spacer"></div>
    """)

    opcoes = [
        ("dashboard", f"📊 Dashboard {hub}"),
        ("ranking", f"🏆 Ranking {hub}"),
        ("inteligencia", f"📈 Inteligência {hub}"),
        ("config", f"⚙️ Configuração {hub}"),
    ]

    cols = st.columns(4)
    for col, (valor, rotulo) in zip(cols, opcoes):
        with col:
            tipo = "primary" if st.session_state[chave] == valor else "secondary"
            if st.button(rotulo, key=f"nav_{hub}_{valor}", type=tipo, use_container_width=True):
                persistir_config_widgets_hub(hub)
                st.session_state[chave] = valor
                st.rerun()

    return st.session_state[chave]

def render_inteligencia_operacional(hub):
    html(f'<div class="section-title">📈 Inteligência Operacional - {hub}</div>')
    st.caption("Base oficial de fechamento diário: reutilização semanal, reincidência de ofensores e índice de confiabilidade a partir dos fechamentos salvos.")

    if pd is None:
        st.error("Para usar este módulo, instale pandas no projeto.")
        return

    rotas_atuais = st.session_state.rotas_por_hub.get(hub, [])
    col_f1, col_f2, col_f3 = st.columns([1, 1, 1.35])
    with col_f1:
        dias = st.selectbox("Período", [7, 14, 30, 60, 90], index=2, format_func=lambda x: f"Últimos {x} dias", key=f"hist_dias_{hub}")
    with col_f2:
        limite_ofensor = st.number_input("Limite para ofensor (%)", min_value=0.0, max_value=100.0, value=95.0, step=1.0, key=f"hist_limite_{hub}")
    with col_f3:
        if st.button("🌙 Salvar fechamento do dia", type="primary", key=f"salvar_fechamento_manual_{hub}", use_container_width=True):
            if salvar_fechamento_driver_supabase(hub, rotas_atuais, limite_ofensor=limite_ofensor, origem="manual"):
                st.success("Fechamento do dia salvo na Inteligência.")
            else:
                st.warning("Não foi possível salvar o fechamento. Verifique a tabela historico_fechamento_driver no Supabase.")

    auto_salvar_fechamento_driver(hub, limite_ofensor=limite_ofensor)
    st.caption(f"O fechamento automático tenta salvar a partir de {FECHAMENTO_AUTO_HORA:02d}:{FECHAMENTO_AUTO_MINUTO:02d}, quando o app estiver aberto ou receber interação. O botão acima permite salvar manualmente o fechamento oficial do dia.")

    df = preparar_df_historico(hub, dias)
    if df is None or df.empty:
        st.info("Ainda não há fechamento salvo para este hub. Clique em Salvar fechamento do dia para iniciar a base limpa a partir de hoje.")
        return

    # Recalcula ofensor conforme o limite selecionado para visualização.
    df["ofensora"] = df["performance"] < float(limite_ofensor)

    total_rotas = len(df)
    drivers_unicos = df["driver_id"].replace("", pd.NA).dropna().nunique() if "driver_id" in df.columns else df["motorista"].nunique()
    total_ofensoras = int(df["ofensora"].sum())
    perf_media = float(df["performance"].mean()) if total_rotas else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rotas no histórico", f"{total_rotas:,}".replace(",", "."))
    c2.metric("Motoristas únicos", f"{drivers_unicos:,}".replace(",", "."))
    c3.metric("Vezes ofensor", f"{total_ofensoras:,}".replace(",", "."))
    c4.metric("Performance média", f"{perf_media:.1f}%")

    # Navegação interna da Inteligência:
    # renderiza somente um módulo por vez para impedir o empilhamento após interações.
    chave_modulo_intel = f"modulo_inteligencia_{hub}"
    if chave_modulo_intel not in st.session_state:
        st.session_state[chave_modulo_intel] = "confiabilidade"

    modulos_intel = [
        ("confiabilidade", "🧭 Confiabilidade"),
        ("reuso", "🔁 Reutilização"),
        ("ofensores", "🔥 Ofensores"),
        ("motorista", "👤 Motorista"),
        ("base", "📄 Base"),
    ]

    cols_modulos = st.columns(5)
    for col_modulo, (valor_modulo, rotulo_modulo) in zip(cols_modulos, modulos_intel):
        with col_modulo:
            tipo_botao = "primary" if st.session_state[chave_modulo_intel] == valor_modulo else "secondary"
            if st.button(
                rotulo_modulo,
                key=f"btn_inteligencia_{hub}_{valor_modulo}",
                type=tipo_botao,
                use_container_width=True
            ):
                st.session_state[chave_modulo_intel] = valor_modulo
                st.rerun()

    modulo_intel_ativo = st.session_state[chave_modulo_intel]

    if modulo_intel_ativo == "confiabilidade":
        html('<div class="section-title">🧭 Índice de Confiabilidade</div>')
        st.caption("Nota calculada com base em performance média, reincidência como ofensor, pendentes, On Hold e consistência no período selecionado.")
        indice = montar_indice_confiabilidade(df)
        if indice is None or indice.empty:
            st.info("Ainda não há dados suficientes para calcular o índice de confiabilidade.")
        else:
            qtd_confiavel = int((indice["Classificação"] == "🟢 Confiável").sum())
            qtd_atencao = int((indice["Classificação"] == "🟡 Atenção").sum())
            qtd_risco = int((indice["Classificação"] == "🔴 Alto risco").sum())
            media_indice = float(indice["Índice"].mean()) if len(indice) else 0
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("🟢 Confiáveis", f"{qtd_confiavel:,}".replace(",", "."))
            k2.metric("🟡 Atenção", f"{qtd_atencao:,}".replace(",", "."))
            k3.metric("🔴 Alto risco", f"{qtd_risco:,}".replace(",", "."))
            k4.metric("Média do índice", f"{media_indice:.1f}")

            filtro_confianca = st.selectbox(
                "Filtrar classificação",
                ["Todos", "🟢 Confiável", "🟡 Atenção", "🔴 Alto risco"],
                key=f"filtro_confianca_{hub}"
            )
            exibicao_indice = indice.copy()
            if filtro_confianca != "Todos":
                exibicao_indice = exibicao_indice[exibicao_indice["Classificação"] == filtro_confianca]

            cols_indice = [
                "Índice", "Classificação", "Recomendação", "driver_id", "motorista", "rotas", "dias_trabalhados",
                "performance_media", "ofensores", "% ofensores", "volume", "pendentes", "% pendentes", "on_hold", "% on hold", "ultima_data"
            ]
            cols_indice = [c for c in cols_indice if c in exibicao_indice.columns]
            st.dataframe(exibicao_indice[cols_indice].rename(columns={
                "driver_id": "Driver ID",
                "motorista": "Motorista",
                "rotas": "Rotas",
                "dias_trabalhados": "Dias trabalhados",
                "performance_media": "Performance média",
                "ofensores": "Vezes ofensor",
                "volume": "Volume",
                "pendentes": "Pendentes",
                "on_hold": "On Hold",
                "ultima_data": "Última data"
            }), use_container_width=True, hide_index=True)

            st.download_button(
                "📥 Exportar índice de confiabilidade",
                data=indice.to_csv(index=False, sep=";", encoding="utf-8-sig"),
                file_name=f"{hub}_indice_confiabilidade_{agora_brasil().strftime('%d-%m-%Y')}.csv",
                mime="text/csv",
                key=f"csv_indice_confiabilidade_{hub}",
                type="primary"
            )

    elif modulo_intel_ativo == "reuso":
        html('<div class="section-title">🔁 Motoristas reutilizados na semana</div>')
        df_reuso = df.copy()
        if "driver_id" not in df_reuso.columns:
            df_reuso["driver_id"] = ""
        agrupado = (
            df_reuso.groupby(["ano", "semana", "driver_id", "motorista"], dropna=False)
            .agg(
                rotas=("at", "nunique"),
                dias_trabalhados=("data", "nunique"),
                volume=("volume", "sum"),
                entregues=("entregues", "sum"),
                on_hold=("on_hold", "sum"),
                ofensores=("ofensora", "sum"),
                performance_media=("performance", "mean"),
            )
            .reset_index()
        )
        agrupado["performance_media"] = agrupado["performance_media"].round(1)
        agrupado["% ofensores"] = ((agrupado["ofensores"] / agrupado["rotas"].replace(0, 1)) * 100).round(1)
        agrupado = agrupado.sort_values(["ano", "semana", "rotas", "performance_media"], ascending=[False, False, False, False])
        st.dataframe(agrupado.rename(columns={
            "ano":"Ano", "semana":"Semana", "driver_id":"Driver ID", "motorista":"Motorista",
            "rotas":"Rotas", "dias_trabalhados":"Dias", "volume":"Volume", "entregues":"Entregues",
            "on_hold":"On Hold", "ofensores":"Vezes ofensor", "performance_media":"Performance média"
        }), use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Exportar reutilização semanal",
            data=agrupado.to_csv(index=False, sep=";", encoding="utf-8-sig"),
            file_name=f"{hub}_reutilizacao_semanal_{agora_brasil().strftime('%d-%m-%Y')}.csv",
            mime="text/csv",
            key=f"csv_reuso_{hub}",
            type="primary"
        )

    elif modulo_intel_ativo == "ofensores":
        html('<div class="section-title">🔥 Histórico de ofensores</div>')
        df_of = df[df["ofensora"]].copy()
        if df_of.empty:
            st.success("Nenhuma rota ofensora no período selecionado.")
        else:
            t1, t2, t3 = st.tabs(["Dia", "Semana", "Mês"])
            with t1:
                por_dia = (
                    df_of.groupby(["data", "driver_id", "motorista"], dropna=False)
                    .agg(vezes_ofensor=("at", "nunique"), volume=("volume", "sum"), performance_media=("performance", "mean"))
                    .reset_index()
                    .sort_values(["data", "vezes_ofensor"], ascending=[False, False])
                )
                por_dia["performance_media"] = por_dia["performance_media"].round(1)
                st.dataframe(por_dia.rename(columns={"data":"Data", "driver_id":"Driver ID", "motorista":"Motorista", "vezes_ofensor":"Vezes ofensor", "performance_media":"Performance média"}), use_container_width=True, hide_index=True)
            with t2:
                por_semana = (
                    df_of.groupby(["ano", "semana", "driver_id", "motorista"], dropna=False)
                    .agg(vezes_ofensor=("at", "nunique"), volume=("volume", "sum"), performance_media=("performance", "mean"))
                    .reset_index()
                    .sort_values(["ano", "semana", "vezes_ofensor"], ascending=[False, False, False])
                )
                por_semana["performance_media"] = por_semana["performance_media"].round(1)
                st.dataframe(por_semana.rename(columns={"ano":"Ano", "semana":"Semana", "driver_id":"Driver ID", "motorista":"Motorista", "vezes_ofensor":"Vezes ofensor", "performance_media":"Performance média"}), use_container_width=True, hide_index=True)
            with t3:
                por_mes = (
                    df_of.groupby(["ano", "mes", "driver_id", "motorista"], dropna=False)
                    .agg(vezes_ofensor=("at", "nunique"), volume=("volume", "sum"), performance_media=("performance", "mean"))
                    .reset_index()
                    .sort_values(["ano", "mes", "vezes_ofensor"], ascending=[False, False, False])
                )
                por_mes["performance_media"] = por_mes["performance_media"].round(1)
                st.dataframe(por_mes.rename(columns={"ano":"Ano", "mes":"Mês", "driver_id":"Driver ID", "motorista":"Motorista", "vezes_ofensor":"Vezes ofensor", "performance_media":"Performance média"}), use_container_width=True, hide_index=True)

    elif modulo_intel_ativo == "motorista":
        html('<div class="section-title">👤 Ficha do motorista</div>')
        motoristas = sorted(df["motorista"].dropna().unique().tolist())
        motorista_sel = st.selectbox("Selecione o motorista", motoristas, key=f"hist_motorista_{hub}")
        detalhe = df[df["motorista"] == motorista_sel].sort_values("data", ascending=False)
        if not detalhe.empty:
            # Garante que a ficha mostre a mesma quantidade de rotas que aparece na tabela.
            # Remove duplicidades exatas do mesmo fechamento e depois conta as linhas exibidas.
            chaves_detalhe = [c for c in ["data", "hub", "at", "driver_id"] if c in detalhe.columns]
            if chaves_detalhe:
                detalhe = detalhe.drop_duplicates(subset=chaves_detalhe, keep="last")
            rotas = len(detalhe)
            volume = int(detalhe["volume"].sum())
            media = float(detalhe["performance"].mean())
            ofens = int(detalhe["ofensora"].sum())
            m1, m2, m3, m4 = st.columns(4)
            indice_motorista = montar_indice_confiabilidade(detalhe)
            indice_txt = "-"
            classe_txt = "-"
            if indice_motorista is not None and not indice_motorista.empty:
                indice_txt = f"{float(indice_motorista.iloc[0].get('Índice', 0)):.1f}"
                classe_txt = str(indice_motorista.iloc[0].get("Classificação", "-"))

            m1.metric("Rotas", rotas)
            m2.metric("Volume", f"{volume:,}".replace(",", "."))
            m3.metric("Performance média", f"{media:.1f}%")
            m4.metric("Índice", indice_txt)
            st.info(f"Classificação do motorista: **{classe_txt}** | Vezes ofensor no período: **{ofens}**")
            cols = ["data", "janela", "at", "gaiola", "volume", "entregues", "on_hold", "pendentes", "performance", "ofensora"]
            cols = [c for c in cols if c in detalhe.columns]
            st.dataframe(detalhe[cols].rename(columns={
                "data":"Data", "janela":"Janela", "at":"AT", "gaiola":"Gaiola", "volume":"Volume",
                "entregues":"Entregues", "on_hold":"On Hold", "pendentes":"Pendentes", "performance":"Performance", "ofensora":"Ofensor"
            }), use_container_width=True, hide_index=True)

    elif modulo_intel_ativo == "base":
        html('<div class="section-title">📄 Base histórica completa</div>')
        cols = ["data", "hub", "janela", "driver_id", "motorista", "at", "gaiola", "cluster", "modalidade", "volume", "entregues", "on_hold", "pendentes", "performance", "ofensora", "nao_coletada"]
        cols = [c for c in cols if c in df.columns]
        st.dataframe(df[cols].sort_values(["data", "motorista"], ascending=[False, True]), use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Exportar base histórica completa",
            data=df[cols].to_csv(index=False, sep=";", encoding="utf-8-sig"),
            file_name=f"{hub}_historico_driver_{agora_brasil().strftime('%d-%m-%Y')}.csv",
            mime="text/csv",
            key=f"csv_hist_base_{hub}",
            type="primary"
        )


def ordenar_rotas(rotas, campo_ordenacao, ordem_desc):
    rotas_exibicao = rotas.copy()
    if campo_ordenacao == "Performance":
        rotas_exibicao.sort(key=lambda x: float(x.get("Performance", 0)), reverse=ordem_desc)
    elif campo_ordenacao == "Hora Bipada":
        rotas_exibicao.sort(key=lambda x: str(x.get("Hora Bipada", "")), reverse=ordem_desc)
    elif campo_ordenacao == "Motorista":
        rotas_exibicao.sort(key=lambda x: str(x.get("Motorista", "")), reverse=ordem_desc)
    elif campo_ordenacao == "Janela":
        rotas_exibicao.sort(key=lambda x: str(x.get("Janela", "")), reverse=ordem_desc)
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
@media (max-width: 768px) {
    .fixed-sidebar {
        position: relative !important;
        width: 100% !important;
        height: auto !important;
        padding: 14px !important;
    }

    .fixed-sidebar img {
        width: 160px !important;
        margin: 0 auto 18px auto !important;
    }

    .fixed-menu-btn {
        font-size: 14px !important;
        padding: 12px 14px !important;
        margin-bottom: 8px !important;
    }

    .fixed-footer {
        position: relative !important;
        bottom: auto !important;
        left: auto !important;
        right: auto !important;
        margin-top: 12px !important;
        padding: 14px !important;
        font-size: 13px !important;
    }

    .block-container {
        padding-left: 14px !important;
        padding-right: 14px !important;
        padding-top: 1rem !important;
        max-width: 100% !important;
    }

    .dashboard-box {
        padding: 18px !important;
    }

    .dashboard-box > div {
        flex-direction: column !important;
        align-items: flex-start !important;
        gap: 12px !important;
    }

    .dashboard-hub {
        font-size: 26px !important;
    }

    .metric,
    .metric.second,
    .progress-card {
        min-height: auto !important;
        padding: 18px !important;
        margin-bottom: 12px !important;
    }

    .metric-value {
        font-size: 24px !important;
    }

    .circle {
        width: 48px !important;
        height: 48px !important;
        font-size: 22px !important;
        flex: 0 0 48px !important;
    }

    div[data-testid="column"] {
        width: 100% !important;
        flex: 1 1 100% !important;
    }

    .ats-header-cell,
    .ats-cell {
        font-size: 12px !important;
        padding: 10px !important;
        min-height: auto !important;
    }

    .route-progress {
        min-width: 100px !important;
    }

    .wpp-button {
        max-width: 100% !important;
        min-width: 0 !important;
        font-size: 12px !important;
    }
}
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
    st.write(f"**Janela:** {rota.get('Janela', '-')}")
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
    st.write(f"**Taxa esperada de entrega:** {_esperada(rota)}")

    link_wpp = montar_link_whatsapp(st.session_state.get("hub", "LPE-12"), rota)
    if link_wpp:
        st.markdown(f'<a class="wpp-button" href="{link_wpp}" target="_blank">WhatsApp</a>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="sem-contato">Sem contato</span>', unsafe_allow_html=True)


def gerar_dataframe_ranking(rotas):
    linhas = []

    for rota in rotas:
        nome = str(rota.get("Motorista", "") or "").strip()
        if not nome or nome.upper() in ["NÃO BIPADA", "NAO BIPADA", "SEM MOTORISTA"]:
            continue

        total = int(rota.get("Total") or 0)
        entregues = int(rota.get("Entregues") or 0)

        if total <= 0:
            continue

        performance = (entregues / total) * 100

        linhas.append({
            "Motorista": nome.title(),
            "Performance": round(performance, 1),
            "Total": total,
            "Entregues": entregues,
            "Pendentes": int(rota.get("Pendentes") or 0),
            "AT": rota.get("AT", "")
        })

    if pd is None:
        return []

    df = pd.DataFrame(linhas)

    if df.empty:
        return df

    return df.sort_values(by=["Performance", "Entregues"], ascending=[False, False])


def gerar_csv_ranking(rotas):
    df = gerar_dataframe_ranking(rotas)

    if pd is None or df.empty:
        return None

    return df.to_csv(index=False, sep=";", encoding="utf-8-sig")






def render_ranking_hub(hub):
    rotas = st.session_state.rotas_por_hub.get(hub, [])

    html(f'<div class="section-title">🏆 Ranking Operacional - {hub}</div>')

    if not rotas:
        st.info("Nenhuma rota carregada para gerar ranking.")
        return

    if pd is None:
        st.error("Para usar o ranking, instale pandas.")
        return

    df = gerar_dataframe_ranking(rotas)

    if df.empty:
        st.info("Nenhum motorista com dados válidos para ranking.")
        return

    col1, col2 = st.columns([1, 1])

    with col1:
        minimo_ranking = st.number_input(
            "Performance mínima para melhores",
            min_value=0.0,
            max_value=100.0,
            value=97.0,
            step=1.0,
            key=f"ranking_min_{hub}"
        )

    with col2:
        limite_ofensores = st.number_input(
            "Performance máxima para ofensores",
            min_value=0.0,
            max_value=100.0,
            value=80.0,
            step=1.0,
            key=f"ranking_ofensor_{hub}"
        )

    melhores = df[df["Performance"] >= minimo_ranking].copy()
    ofensores = df[df["Performance"] < limite_ofensores].copy()

    csv_ranking = gerar_csv_ranking(rotas)
    if csv_ranking:
        st.download_button(
            label=f"📥 Exportar CSV Ranking - {hub}",
            data=csv_ranking,
            file_name=f"ranking_operacional_{hub}_{agora_brasil().strftime('%d_%m_%Y')}.csv",
            mime="text/csv",
            key=f"baixar_ranking_csv_{hub}",
            type="primary"
        )

    html('<div class="section-title">🏆 Melhores do Dia</div>')

    if melhores.empty:
        st.info("Nenhum motorista acima da performance mínima configurada.")
    else:
        melhores_lista = melhores.copy()
        melhores_lista["Faixa"] = melhores_lista["Performance"].apply(lambda x: int(float(x)))
        melhores_lista = melhores_lista.sort_values(["Faixa", "Performance", "Entregues"], ascending=[False, False, False])
        posicao = 1
        for faixa, grupo in melhores_lista.groupby("Faixa", sort=False):
            with st.expander(f"🏆 {int(faixa)}% — {len(grupo)} motoristas", expanded=True):
                for row in grupo.itertuples():
                    st.markdown(f"**{posicao:02d}. {row.Motorista}** — {row.Performance:.1f}%")
                    posicao += 1

    html('<div class="section-title">⚠️ Ofensores Operacionais</div>')

    if ofensores.empty:
        st.success("Nenhum ofensor abaixo do limite configurado.")
    else:
        ofensores_lista = ofensores.copy()
        ofensores_lista["Faixa"] = ofensores_lista["Performance"].apply(lambda x: int(float(x)))
        ofensores_lista = ofensores_lista.sort_values(["Faixa", "Performance", "Entregues"], ascending=[True, True, False])
        posicao = 1
        for faixa, grupo in ofensores_lista.groupby("Faixa", sort=False):
            with st.expander(f"⚠️ {int(faixa)}% — {len(grupo)} motoristas", expanded=True):
                for row in grupo.itertuples():
                    st.markdown(f"**{posicao:02d}. {row.Motorista}** — {row.Performance:.1f}%")
                    posicao += 1

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
    st.query_params["u"] = st.session_state.get("usuario_login", "")
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
    html(f'<div class="last-update-home">🕘 Última atualização: {agora_brasil().strftime("%d/%m/%Y %H:%M")}</div>')


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
    falta = entregues / volume if volume else 0
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

    def render_card_janela(nome_janela, icone, cor_barra):
        volume_j = int(dados.get(f"Volume {nome_janela}", 0) or 0)
        entregues_j = int(dados.get(f"Entregues {nome_janela}", 0) or 0)
        pendentes_j = int(dados.get(f"Pendentes {nome_janela}", 0) or 0)
        onhold_j = int(dados.get(f"Onhold {nome_janela}", 0) or 0)
        rotas_j = int(dados.get(f"Rotas {nome_janela}", 0) or 0)
        perf_j = entregues_j / volume_j if volume_j else 0
        html(f'''
        <div class="progress-card">
            <div class="progress-title">{icone} Janela {nome_janela}</div>
            <div class="progress-bg"><div class="fill {cor_barra}" style="width:{perf_j*100:.1f}%;"></div></div>
            <div class="progress-info"><span>Performance</span><b>{perf_j*100:.1f}%</b></div>
            <div class="progress-info"><span>Volume</span><b>{volume_j:,}</b></div>
            <div class="progress-info"><span>Entregues</span><b>{entregues_j:,}</b></div>
            <div class="progress-info"><span>Pendentes</span><b>{pendentes_j:,}</b></div>
            <div class="progress-info"><span>On Hold</span><b>{onhold_j:,}</b></div>
            <div class="progress-info"><span>Rotas</span><b>{rotas_j:,}</b></div>
        </div>
        '''.replace(",", "."))

    html('<div class="section-title">Performance por Janela</div>')
    janela_col1, janela_col2 = st.columns(2)
    with janela_col1:
        render_card_janela("AM", "🌅", "orange-bar")
    with janela_col2:
        render_card_janela("PM", "🌆", "blue-bar")

    if rotas_hub:
        linhas_csv = []
        for rota in rotas_hub:
            linhas_csv.append({
                "Hub": hub,
                "AT": rota.get("AT", ""),
                "Janela": rota.get("Janela", "-"),
                "Driver ID": rota.get("Driver ID", ""),
                "Motorista": rota.get("Motorista", ""),
                "Telefone": rota.get("Telefone", ""),
                "Modal": rota.get("Modal", ""),
                "Gaiola": rota.get("Gaiola", ""),
                "Bairro": rota.get("Bairro", ""),
                "Cluster": rota.get("Cluster", ""),
                "Hora Bipada": rota.get("Hora Bipada", ""),
                "Hora Atribuição": rota.get("Hora Atribuição", ""),
                "Cidade": rota.get("Cidade", ""),
                "Station": rota.get("Station", ""),
                "Total": rota.get("Total", 0),
                "Entregues": rota.get("Entregues", 0),
                "Pendentes": rota.get("Pendentes", 0),
                "On Hold": rota.get("On Hold", 0),
                "Performance": rota.get("Performance %", "0.0%"),
            })

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=list(linhas_csv[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(linhas_csv)

        st.download_button(
            label=f"📥 Exportar CSV - {hub}",
            data="\ufeff" + output.getvalue(),
            file_name=f"{hub}_motoristas_{agora_brasil().strftime('%d-%m-%Y_%H-%M')}.csv",
            mime="text/csv",
            key=f"exportar_csv_{hub}",
            type="primary",
            use_container_width=True
        )

    if rotas_hub:
        html('<div class="section-title">Lista de ATs do Hub</div>')
        col_ord1, col_ord2 = st.columns([2, 1])
        with col_ord1:
            campo_ordenacao = st.selectbox("Ordenar por", ["Progresso", "Taxa esperada", "Hora Bipada", "Motorista", "Janela", "AT", "Total", "Entregues", "Pendentes"], index=0, key=f"ordenar_{hub}")
        with col_ord2:
            ordem_desc = st.toggle("Decrescente", value=True, key=f"ordem_{hub}")
        rotas_exibicao = ordenar_rotas(rotas_hub, campo_ordenacao, ordem_desc)
        COLUNAS_ATS = [1.5, 1.0, 3.8, 2.4, 3.0, 2.8, 1.3, 1.5]
        for col, titulo in zip(st.columns(COLUNAS_ATS), ["AT", "Janela", "Motorista", "Hora bipada", "Progresso", "Taxa esperada", "Ação", "WhatsApp"]):
            with col:
                html(f'<div class="ats-header-cell center">{titulo}</div>')
        for i, rota in enumerate(rotas_exibicao):
            c_at, c_janela, c_motorista, c_hora, c_prog, c_esperada, c_btn, c_wpp = st.columns(COLUNAS_ATS)
            progresso_percentual = calcular_percentual_progresso(rota)
            taxa_esperada = calcular_taxa_esperada_entrega(rota)
            progresso_texto = f'{int(rota.get("Entregues") or 0)}/{int(rota.get("Total") or 0)}'
            with c_at: html(f'<div class="ats-cell at-code">{rota["AT"]}</div>')
            with c_janela: html(f'<div class="ats-cell center">{rota.get("Janela", "-")}</div>')
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

    if st.button(f"🔄 Recarregar configurações salvas do {hub}", key=f"reload_config_{hub}", type="primary"):
        carregar_hub_supabase(hub, atualizar_widgets=True)
        st.success(f"Configurações do {hub} recarregadas do Supabase.")
        time.sleep(0.5)
        st.rerun()

    config_hub = st.session_state.config_por_hub.get(hub, config_default())

    if f"bash_list_{hub}" not in st.session_state:
        st.session_state[f"bash_list_{hub}"] = config_hub.get("bash_list", "")
    if f"bash_v2_{hub}" not in st.session_state:
        st.session_state[f"bash_v2_{hub}"] = config_hub.get("bash_v2", "")
    ats_legado = config_hub.get("ats", "")
    if f"ats_{hub}" not in st.session_state:
        st.session_state[f"ats_{hub}"] = ats_legado
    if f"ats_am_{hub}" not in st.session_state:
        st.session_state[f"ats_am_{hub}"] = config_hub.get("ats_am", ats_legado)
    if f"ats_pm_{hub}" not in st.session_state:
        st.session_state[f"ats_pm_{hub}"] = config_hub.get("ats_pm", "")
    if f"database_{hub}" not in st.session_state:
        st.session_state[f"database_{hub}"] = config_hub.get("db_link", st.session_state.db_links_por_hub.get(hub, ""))

    bash_list = st.text_area("Bash LIST / AUTH", height=180, key=f"bash_list_{hub}")
    bash_v2 = st.text_area("Bash V2", height=240, key=f"bash_v2_{hub}")

    aba_ats_am, aba_ats_pm = st.tabs(["🌅 ATs AM", "🌆 ATs PM"])
    with aba_ats_am:
        ats_am_texto = st.text_area(
            "ATs AM para buscar",
            height=170,
            placeholder="Cole as ATs do AM, uma por linha ou separadas por vírgula.",
            key=f"ats_am_{hub}"
        )
    with aba_ats_pm:
        ats_pm_texto = st.text_area(
            "ATs PM para buscar",
            height=170,
            placeholder="Cole as ATs do PM, uma por linha ou separadas por vírgula.",
            key=f"ats_pm_{hub}"
        )

    link_database = st.text_input("Link da planilha Database do Hub", placeholder="Cole o link da aba Database. Coluna B = Nome | Coluna I = Telefone", key=f"database_{hub}")

    st.session_state.config_por_hub[hub] = {
        "bash_list": bash_list,
        "bash_v2": bash_v2,
        "ats": "\n".join([ats_am_texto, ats_pm_texto]).strip(),
        "ats_am": ats_am_texto,
        "ats_pm": ats_pm_texto,
        "db_link": link_database,
    }
    st.session_state.db_links_por_hub[hub] = link_database

    arquivo_database = st.file_uploader("Anexar arquivo Database do Hub (.xlsx, .xls ou .csv)", type=["xlsx", "xls", "csv"], key=f"database_file_{hub}")
    col_a, col_c, col_b = st.columns([1, 1, 2])
    with col_a:
        iniciar = st.button(f"🚀 Atualizar {hub}", use_container_width=True, key=f"iniciar_{hub}", type="primary")
    with col_c:
        carregar_contatos_btn = st.button("📱 Carregar contatos", use_container_width=True, key=f"carregar_contatos_{hub}", type="primary")
    with col_b:
        somente_v2 = st.checkbox("Buscar todas as páginas do V2", value=True, key=f"somente_v2_{hub}")

    if st.button(f"💾 Salvar configurações do {hub}", use_container_width=True, key=f"salvar_config_{hub}", type="primary"):
        ats_am_salvar = st.session_state.get(f"ats_am_{hub}", "")
        ats_pm_salvar = st.session_state.get(f"ats_pm_{hub}", "")
        st.session_state.config_por_hub[hub] = {
            "bash_list": st.session_state.get(f"bash_list_{hub}", ""),
            "bash_v2": st.session_state.get(f"bash_v2_{hub}", ""),
            "ats": "\n".join([ats_am_salvar, ats_pm_salvar]).strip(),
            "ats_am": ats_am_salvar,
            "ats_pm": ats_pm_salvar,
            "db_link": st.session_state.get(f"database_{hub}", ""),
        }
        st.session_state.db_links_por_hub[hub] = st.session_state.config_por_hub[hub]["db_link"]
        salvar_estado_persistido()
        st.success(f"Configurações do {hub} salvas na nuvem.")

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
            salvar_estado_persistido(hub)
            st.success(f"Contatos carregados para {hub}: {len(contatos_database)}")
        except Exception as e:
            st.error(f"Não foi possível carregar a Database: {e}")
            log(f"Não foi possível carregar contatos do hub {hub}: {e}")

    if iniciar:
        st.session_state.config_por_hub[hub] = {
            "bash_list": bash_list,
            "bash_v2": bash_v2,
            "ats": "\n".join([ats_am_texto, ats_pm_texto]).strip(),
            "ats_am": ats_am_texto,
            "ats_pm": ats_pm_texto,
            "db_link": link_database,
        }
        st.session_state.db_links_por_hub[hub] = link_database
        st.session_state.terminal = []
        ats_am = limpar_ats(ats_am_texto)
        ats_pm = limpar_ats(ats_pm_texto)
        ats = list(dict.fromkeys(ats_am + ats_pm))
        mapa_janelas = {at: "AM" for at in ats_am}
        mapa_janelas.update({at: "PM" for at in ats_pm})
        try:
            log(f"Iniciando processo do hub {hub}...")
            log(f"ATs digitadas: {len(ats)} | AM: {len(ats_am)} | PM: {len(ats_pm)}")
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
            mapa_v2 = processar_rotas_v2(lista_v2, ats, mapa_janelas=mapa_janelas)
            log(f"ATs encontradas no V2: {len(mapa_v2)}")
            if not mapa_v2:
                st.warning("Nenhuma AT encontrada no V2.")
            else:
                log("Consultando pacotes por AT em modo seguro...")
                metricas_lote = buscar_metricas_em_lote(bash_list, mapa_v2, max_workers=6)
                for at, metricas in metricas_lote.items():
                    rota = mapa_v2[at]

                    # Regra operacional SPX:
                    # Se o V2 informa a rota como concluída (status 5 + complete_time),
                    # a rota deve ser considerada 100% finalizada no Dashboard.
                    # Isso corrige casos em que o detalhe dos pacotes ainda retorna
                    # SP_Ready_Collection/SP_Collection_Collected como pendente,
                    # mesmo com a AT já completa no sistema.
                    if bool(rota.get("Rota Concluida V2", False)):
                        try:
                            total_v2 = int(rota.get("Total V2") or 0)
                        except Exception:
                            total_v2 = 0
                        total_final = total_v2 if total_v2 > 0 else int(metricas.get("Total") or 0)

                        rota["Total"] = total_final
                        rota["Entregues"] = total_final
                        rota["On Hold"] = 0
                        rota["Pendentes"] = 0
                        rota["Performance"] = 1 if total_final else 0
                        rota["Performance %"] = "100.0%" if total_final else "0.0%"
                    else:
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
                salvar_historico_driver_supabase(hub, rotas, limite_ofensor=95.0)
                salvar_estado_persistido(hub)
                st.success(f"Atualização do {hub} finalizada.")
                st.session_state.tela = "hub"; st.session_state.hub = hub
                token_atual = st.session_state.get("auth_token") or st.query_params.get("auth", "")
                if token_atual: st.query_params["auth"] = token_atual
                st.query_params["u"] = st.session_state.get("usuario_login", "")
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
    render_header(titulo="Consolidado", subtitulo="Visão consolidada dos hubs e cálculo de meta por janela AM/PM.")

    linhas_consolidado = []
    for hub_nome in hubs_visiveis_usuario():
        dados_hub = st.session_state.hubs.get(hub_nome, hub_default())
        volume = int(dados_hub.get("Volume", 0) or 0)
        entregues = int(dados_hub.get("Entregues", 0) or 0)
        linhas_consolidado.append({
            "Hub": hub_nome,
            "Volume": volume,
            "AM": int(dados_hub.get("Volume AM", 0) or 0),
            "PM": int(dados_hub.get("Volume PM", 0) or 0),
            "Entregues": entregues,
            "DS": f"{(entregues / volume * 100) if volume else 0:.1f}%",
            "Rotas": int(dados_hub.get("Total de Rotas", 0) or 0),
            "Atualizado": dados_hub.get("Última Atualização", "Sem atualização"),
        })
    if linhas_consolidado and pd is not None:
        st.dataframe(pd.DataFrame(linhas_consolidado), use_container_width=True, hide_index=True)
    elif linhas_consolidado:
        st.table(linhas_consolidado)

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
    hub_atual = st.session_state.get("hub", "LPE-12")

    if not usuario_pode_acessar_hub(hub_atual):
        render_acesso_negado(hub_atual)
        st.stop()

    # Recarrega do Supabase ao entrar/trocar de hub para buscar atualizações de outros analistas.
    if st.session_state.get("_ultimo_hub_recarregado") != hub_atual:
        carregar_hub_supabase(hub_atual, atualizar_widgets=True)
        st.session_state["_ultimo_hub_recarregado"] = hub_atual

    # Fechamento automático no fim do dia, sem interferir na navegação.
    auto_salvar_fechamento_driver(hub_atual, limite_ofensor=95.0)

    render_header(
        titulo=f"Dashboard {hub_atual}",
        subtitulo=f"Performance operacional em tempo real do hub {hub_atual}."
    )

    subtela_hub = render_nav_hub_unica(hub_atual)

    if subtela_hub == "dashboard":
        render_dashboard_hub(hub_atual)
    elif subtela_hub == "ranking":
        render_ranking_hub(hub_atual)
    elif subtela_hub == "inteligencia":
        render_inteligencia_operacional(hub_atual)
    elif subtela_hub == "config":
        render_configuracao_hub(hub_atual)
