"""Integração de romaneios Google Sheets com gestão de ocorrências no Streamlit.

Requer a tabela SQL em ocorrencias_schema.sql. Os romaneios permanecem somente-leitura.
"""
import csv
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from io import StringIO, BytesIO
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st

COLUNAS = {
    # No romaneio PM, a coluna de motoristas chama-se NOME.
    "motorista": ["NOME", "DRIVER", "DRIVER NAME", "MOTORISTA", "NOME DO MOTORISTA", "NOME DRIVER"],
    "ocorrencia": ["OCORRENCIA", "OCORRÊNCIA", "MOTIVO", "TIPO DE OCORRENCIA", "STATUS OCORRENCIA"],
    "data_ocorrencia": ["DATA", "DATA OCORRENCIA", "DATA DA OCORRENCIA", "DATE"],
    "turno": ["TURNO", "CICLO", "SHIFT"],
    "reversao": ["REVERSAO", "REVERSÃO"],
    "observacao": ["OBS", "OBSERVACAO", "OBSERVAÇÃO", "OBSERVACOES", "JUSTIFICATIVA"],
}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c)).upper()).strip()


def parse_google_link(url):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.hostname != "docs.google.com":
        raise ValueError("Informe um link de Google Planilhas (docs.google.com).")
    m = re.search(r"^/spreadsheets/d/([\w-]+)", parsed.path)
    if not m:
        raise ValueError("Não foi possível identificar o ID da planilha.")
    parametros = parse_qs(parsed.query)
    fragmentos = parse_qs(parsed.fragment)
    gid = (parametros.get("gid") or fragmentos.get("gid") or ["0"])[0]
    if not gid.isdigit():
        raise ValueError("O gid da aba precisa ser numérico.")
    return m.group(1), gid


def _google_credentials():
    sa = st.secrets.get("GOOGLE_SERVICE_ACCOUNT")
    if not sa:
        return None
    if isinstance(sa, str):
        sa = json.loads(sa)
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError("Instale google-auth para ler planilhas privadas (pip install google-auth).") from exc
    credentials = service_account.Credentials.from_service_account_info(
        dict(sa), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    credentials.refresh(Request())
    return credentials


def _sheet_csv(spreadsheet_id, gid):
    """Localiza OCORRÊNCIAS pelo título, sem depender do gid copiado no link."""
    cred = _google_credentials()
    if cred:
        headers = {"Authorization": f"Bearer {cred.token}"}
        root = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        response = requests.get(root, headers=headers,
                                params={"fields": "sheets(properties(sheetId,title))"}, timeout=25)
        response.raise_for_status()
        sheets = [x.get("properties", {}) for x in response.json().get("sheets", [])]
        # Exige correspondência exata (normalizada) para evitar importar outra aba por engano.
        selected = next((x for x in sheets if _norm(x.get("title")) == "OCORRENCIAS"), None)
        if selected is None:
            names = ", ".join(x.get("title", "") for x in sheets[:20])
            raise ValueError(f"Não encontrei a aba OCORRÊNCIAS. Abas disponíveis: {names or 'nenhuma'}. "
                             "Renomeie a aba para OCORRÊNCIAS e tente novamente.")
        title = selected["title"]
        actual_gid = str(selected["sheetId"])
        # A1 com aspas permite nomes com espaços/acentos e evita URL montada à mão.
        a1 = "'" + title.replace("'", "''") + "'"
        response = requests.get(root + "/values/" + requests.utils.quote(a1, safe=""),
                                headers=headers, params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=40)
        response.raise_for_status()
        return response.json().get("values", []), title, actual_gid

    # Planilhas públicas: primeiro consulta o CSV indicado pelo gid.
    # Para planilhas privadas, credenciais são obrigatórias para identificar título da aba.
    uri = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    response = requests.get(uri, params={"format": "csv", "gid": gid}, timeout=40)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if "html" in content_type or response.text.lstrip().lower().startswith("<!doctype html"):
        raise PermissionError("Planilha privada: configure GOOGLE_SERVICE_ACCOUNT no Streamlit Secrets.")
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig", errors="replace")))), f"gid={gid}", gid


def _find_header(rows):
    """Detecta linha de cabeçalho mesmo quando há títulos mesclados acima."""
    for i, row in enumerate(rows[:100]):
        normalized = [_norm(v) for v in row]
        motorista = any(h in {_norm(v) for v in COLUNAS["motorista"]} for h in normalized)
        motivo = any(h in {_norm(v) for v in COLUNAS["ocorrencia"]} for h in normalized)
        if motorista and motivo:
            return i
    # Falha amigável: expõe diagnóstico sem derrubar o Streamlit.
    sample = " | ".join(" / ".join(str(x) for x in r[:8]) for r in rows[:6])[:700]
    raise ValueError("A aba OCORRÊNCIAS foi encontrada, mas não localizei as colunas "
                     "Motorista e Ocorrência/Motivo nas primeiras 100 linhas. "
                     f"Amostra para conferência: {sample or 'aba vazia'}")


def _guess_columns(headers):
    norm_headers = [_norm(x) for x in headers]
    return {key: next((headers[i] for i, h in enumerate(norm_headers) if h in [_norm(x) for x in aliases]), None)
            for key, aliases in COLUNAS.items()}


def _records(rows, headers_idx, mapping, hub, ciclo, dia, sheet_id, gid):
    headers = [str(h).strip() or f"Coluna_{i+1}" for i, h in enumerate(rows[headers_idx])]
    items = []
    for index in range(headers_idx + 1, len(rows)):
        values = list(rows[index])
        data = {h: str(values[i]).strip() if i < len(values) else "" for i, h in enumerate(headers)}
        motorista = data.get(mapping["motorista"], "").strip()
        motivo = data.get(mapping["ocorrencia"], "").strip()
        if not motorista or not motivo:
            continue
        numero_linha = index + 1
        key = hashlib.sha256(f"{sheet_id}:{gid}:{numero_linha}".encode()).hexdigest()
        items.append({
            "source_key": key, "hub": hub, "ciclo": ciclo,
            "data_romaneio": dia.isoformat(), "spreadsheet_id": sheet_id, "gid": gid,
            "linha": numero_linha, "motorista": motorista, "ocorrencia": motivo,
            "data_ocorrencia": data.get(mapping.get("data_ocorrencia"), "") or dia.strftime("%d/%m/%Y"),
            "turno": data.get(mapping.get("turno"), "") or ciclo,
            "reversao": data.get(mapping.get("reversao"), ""),
            "observacao": data.get(mapping.get("observacao"), ""),
            "bruto": data, "ativo": True, "atualizado_em": datetime.now().astimezone().isoformat(),
        })
    return items


def _get_all(sb, hub, active_only=True):
    data = []
    offset = 0
    while True:
        q = sb.table("ocorrencias_romaneio").select("*").eq("hub", hub)
        if active_only:
            q = q.eq("ativo", True)
        page = q.range(offset, offset + 999).execute().data or []
        data.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return data


def _save(sb, items, hub, sheet_id, gid):
    # Faz upserts por chave de origem. Nunca duplica ao reler o mesmo romaneio.
    existing = (sb.table("ocorrencias_romaneio")
                .select("source_key,linha")
                .eq("hub", hub).eq("spreadsheet_id", sheet_id).eq("gid", gid).execute().data or [])
    present = {item["source_key"] for item in items}
    for off in range(0, len(items), 200):
        sb.table("ocorrencias_romaneio").upsert(items[off:off+200], on_conflict="source_key").execute()
    # Linhas removidas da origem ficam inativas, não são apagadas do histórico.
    missing = [r["source_key"] for r in existing if r["source_key"] not in present]
    for key in missing:
        sb.table("ocorrencias_romaneio").update({"ativo": False}).eq("source_key", key).execute()
    return len(items), len(missing)


def render_ocorrencias(hub, get_supabase, usuario_pode_acessar_hub):
    st.markdown("### 📋 Ocorrências | Integração Google Sheets")
    st.caption("Importe o romaneio pelo link, consulte uma prévia e sincronize o histórico sem editar a planilha original.")
    if not usuario_pode_acessar_hub(hub):
        st.error("Você não possui acesso a este hub.")
        return
    sb = get_supabase()
    if sb is None:
        st.error("Configure o Supabase para armazenar ocorrências entre sessões e usuários.")
        return
    with st.container(border=True):
        a, b = st.columns([3, 1])
        with a:
            url = st.text_input("Link do romaneio Google Sheets", key=f"occ_url_{hub}",
                                placeholder="https://docs.google.com/spreadsheets/d/.../edit?gid=...")
        with b:
            ciclo = st.selectbox("Ciclo", ["AM", "PM"], index=1, key=f"occ_ciclo_{hub}")
        c, d = st.columns(2)
        with c:
            dia = st.date_input("Data do romaneio", value=date.today(), format="DD/MM/YYYY", key=f"occ_dia_{hub}")
        with d:
            st.markdown("**📌 Instruções para importar**")
            st.caption("1. Compartilhe o romaneio com o e-mail abaixo, como **Leitor**:")
            st.code("spx-ocorrencias@spx-intelligence.iam.gserviceaccount.com", language=None)
            st.caption("2. Deixe o nome da aba exatamente como **OCORRÊNCIAS** (com acento). "
                       "O sistema localizará a aba automaticamente, sem depender do gid do link.")
        if st.button("🔎 Ler e conferir ocorrências", key=f"occ_fetch_{hub}", type="primary"):
            try:
                spreadsheet_id, gid = parse_google_link(url)
                with st.spinner("Lendo romaneio com acesso somente-leitura..."):
                    rows, nome_aba, gid_real = _sheet_csv(spreadsheet_id, gid)
                    idx = _find_header(rows)
                    headers = [str(x).strip() or f"Coluna_{i+1}" for i, x in enumerate(rows[idx])]
                st.session_state[f"occ_preview_{hub}"] = {
                    "rows": rows, "idx": idx, "headers": headers,
                    "mapping": _guess_columns(headers), "sheet_id": spreadsheet_id,
                    "gid": gid_real, "nome_aba": nome_aba,
                    "ciclo": ciclo, "dia": dia.isoformat(), "url": url,
                }
                st.success(f"Aba identificada: {nome_aba}. Selecione os campos e confira a prévia.")
            except Exception as exc:
                st.session_state.pop(f"occ_preview_{hub}", None)
                st.error(f"Não foi possível ler o romaneio: {exc}")
    preview = st.session_state.get(f"occ_preview_{hub}")
    if preview:
        if url != preview["url"] or ciclo != preview["ciclo"] or dia.isoformat() != preview["dia"]:
            st.warning("Link, ciclo ou data alterado. Clique novamente em 'Ler e conferir ocorrências'.")
            return
        headers = preview["headers"]
        map_editado = {}
        fields = {"motorista":"Motorista", "ocorrencia":"Ocorrência", "data_ocorrencia":"Data da ocorrência",
                  "turno":"Turno", "reversao":"Reversão", "observacao":"Observação"}
        cols = st.columns(3)
        for i, (key, label) in enumerate(fields.items()):
            options = ([""] if key not in ("motorista", "ocorrencia") else []) + headers
            suggested = preview["mapping"].get(key)
            with cols[i%3]:
                map_editado[key] = st.selectbox(f"Coluna: {label}", options,
                                                index=options.index(suggested) if suggested in options else 0,
                                                key=f"occ_map_{hub}_{key}")
        if not map_editado["motorista"] or not map_editado["ocorrencia"]:
            st.warning("Motorista e Ocorrência são campos obrigatórios.")
            return
        records = _records(preview["rows"], preview["idx"], map_editado,
                           hub, ciclo, dia, preview["sheet_id"], preview["gid"])
        st.info(f"{len(records)} ocorrência(s) com motorista e motivo preenchidos encontradas.")
        st.dataframe([{k:r[k] for k in ("linha", "motorista", "ocorrencia", "data_ocorrencia", "turno")}
                      for r in records[:100]], use_container_width=True, hide_index=True)
        if st.button(f"✅ Sincronizar {len(records)} ocorrência(s)", key=f"occ_sync_{hub}", type="primary"):
            try:
                if not records:
                    st.warning("Nenhuma ocorrência válida encontrada. Nenhum dado foi alterado.")
                else:
                    count, inactive = _save(sb, records, hub, preview["sheet_id"], preview["gid"])
                    st.success(f"{count} registros sincronizados. {inactive} linha(s) de origem desativada(s). Sem duplicar importações anteriores.")
            except Exception as exc:
                st.error(f"Falha ao salvar no Supabase. Confirme se executou o SQL de criação da tabela. Detalhe: {exc}")
    st.divider()
    _render_historical_import(sb, hub)
    st.divider()
    _render_analytics(sb, hub)


# ===========================================================================
# Histórico Excel: importação não destrutiva de base antiga
# ===========================================================================
def _as_date(value):
    """Aceita datas Excel, datetime e textos brasileiros, sem inventar datas inválidas."""
    if value is None or str(value).strip() in ("", "NaT", "nan", "None"):
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    if isinstance(value, (int, float)) and 20000 <= float(value) <= 90000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    raw = str(value).strip()
    if re.fullmatch(r"\d{5}(?:\.0)?", raw):
        return (datetime(1899, 12, 30) + timedelta(days=float(raw))).date()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            pass
    return None


def _legacy_records(file_bytes, filename, hub):
    """Somente aba de ocorrências; NOTAS PARA O PLANEJAMENTO não é ocorrência diária."""
    import pandas as pd
    workbook = pd.ExcelFile(BytesIO(file_bytes))
    sheets = list(workbook.sheet_names)
    target = next((name for name in sheets if _norm(name) == "OCORRENCIAS"), None)
    if target is None:
        target = next((name for name in sheets if "OCORREN" in _norm(name) and "NOTA" not in _norm(name)), None)
    if target is None:
        raise ValueError("Não encontrei a aba OCORRENCIAS. Abas disponíveis: " + ", ".join(sheets))
    frame = pd.read_excel(workbook, sheet_name=target, dtype=object)
    columns = {_norm(c): c for c in frame.columns}
    def col(*aliases):
        return next((columns[a] for a in aliases if a in columns), None)
    driver_col = col("DRIVER", "NOME", "MOTORISTA", "DRIVER NAME")
    motivo_col = col("OCORRENCIA", "MOTIVO", "TIPO DE OCORRENCIA")
    data_col = col("DATA", "DATA OCORRENCIA", "DATA DA OCORRENCIA")
    if not all((driver_col, motivo_col, data_col)):
        raise ValueError("A base precisa ter colunas DATA, DRIVER/NOME e OCORRENCIA.")
    turno_col = col("TURNO", "CICLO")
    rev_col = col("REVERSAO")
    obs_col = col("OBS", "OBSERVACAO")
    # Origem determinística por nome de arquivo e linha: importar o mesmo arquivo novamente atualiza.
    normalized_file = _norm(filename).lower()
    source_id = "historico-xlsx:" + hashlib.sha256(normalized_file.encode()).hexdigest()[:20]
    records, skipped = [], 0
    for row_idx, record in enumerate(frame.to_dict(orient="records"), start=2):
        def cell(field):
            v = record.get(field) if field else ""
            if v is None or pd.isna(v):
                return ""
            return str(v).strip()
        driver, reason = cell(driver_col), cell(motivo_col)
        day = _as_date(record.get(data_col))
        if not driver or not reason or not day:
            skipped += 1
            continue
        turno = cell(turno_col).upper()
        ciclo = turno if turno in ("AM", "PM") else "N/I"
        source_key = hashlib.sha256(f"{hub}:{source_id}:{target}:{row_idx}".encode()).hexdigest()
        records.append({
            "source_key": source_key, "hub": hub, "ciclo": ciclo,
            "data_romaneio": day.isoformat(), "spreadsheet_id": source_id,
            "gid": target, "linha": row_idx, "motorista": driver,
            "ocorrencia": reason, "data_ocorrencia": day.strftime("%d/%m/%Y"),
            "turno": turno or "N/I", "reversao": cell(rev_col),
            "observacao": cell(obs_col), "bruto": {"origem": "Excel histórico", "arquivo": filename,
                                                "aba": target, "linha": row_idx},
            "ativo": True, "atualizado_em": datetime.now().astimezone().isoformat()
        })
    return records, skipped, target


def _historical_commit(sb, records, hub):
    """Confronta ocorrências Google existentes para reduzir duplicação entre origens."""
    existing = _get_all(sb, hub, active_only=False)
    source_keys = {r.get("source_key") for r in existing}
    # Correspondência exata de data, motorista, motivo e turno; preservar multiplicidade.
    from collections import Counter
    def fingerprint(x):
        day = _as_date(x.get("data_ocorrencia")) or _as_date(x.get("data_romaneio"))
        return (str(day), _norm(x.get("motorista")), _norm(x.get("ocorrencia")),
                _norm(x.get("turno")) if _norm(x.get("turno")) in ("AM", "PM") else "")
    google_counts = Counter(fingerprint(x) for x in existing
                            if x.get("ativo", True) and not str(x.get("spreadsheet_id", "")).startswith("historico-xlsx:"))
    # Mantém a mesma correspondência para reimportações. Não rejeita linhas já importadas.
    already = 0
    overlaps = 0
    to_save = []
    for record in records:
        key = record["source_key"]
        fp = fingerprint(record)
        if key in source_keys:
            already += 1
            continue
        if google_counts[fp] > 0:
            google_counts[fp] -= 1
            overlaps += 1
            continue
        to_save.append(record)
    for offset in range(0, len(to_save), 200):
        sb.table("ocorrencias_romaneio").upsert(to_save[offset:offset+200], on_conflict="source_key").execute()
    return len(to_save), already, overlaps


def _render_historical_import(sb, hub):
    st.markdown("### 🗃️ Importar histórico antigo")
    st.caption("Importe a planilha geral do LPE-12 uma vez para unir os registros antigos aos novos romaneios.")
    with st.expander("📥 Importar base Excel (.xlsx)", expanded=False):
        st.info("A importação usa a aba OCORRENCIAS e não altera a planilha original. "
                "Linhas sem motorista, motivo ou data válida são ignoradas e contabilizadas.")
        upload = st.file_uploader("Selecionar planilha histórica", type=["xlsx"], key=f"occ_legacy_upload_{hub}")
        if upload is None:
            return
        raw = upload.getvalue()
        if len(raw) > 20 * 1024 * 1024:
            st.error("Arquivo acima do limite de 20 MB para esta importação.")
            return
        file_hash = hashlib.sha256(raw).hexdigest()
        cache_key = f"occ_legacy_{hub}"
        if st.session_state.get(cache_key, {}).get("hash") != file_hash:
            try:
                with st.spinner("Analisando o Excel histórico..."):
                    records, skipped, aba = _legacy_records(raw, upload.name, hub)
                st.session_state[cache_key] = {"hash": file_hash, "records": records,
                                               "skipped": skipped, "aba": aba, "name": upload.name}
            except Exception as exc:
                st.error(f"Não foi possível analisar o Excel: {exc}")
                return
        info = st.session_state[cache_key]
        a,b,c = st.columns(3)
        a.metric("Ocorrências válidas", f"{len(info['records']):,}".replace(",", "."))
        b.metric("Linhas ignoradas", info["skipped"])
        c.metric("Aba identificada", info["aba"])
        st.dataframe([{k: r.get(k) for k in ("data_romaneio", "motorista", "ocorrencia", "turno", "observacao")}
                      for r in info["records"][:30]], hide_index=True, use_container_width=True)
        st.warning("Confira a prévia antes de importar. Registros de planilhas Google já sincronizados "
                   "serão comparados por data, motorista, ocorrência e turno para reduzir sobreposições. "
                   "Diferenças de grafia ou turno não informado podem exigir revisão posterior.")
        confirmed = st.checkbox("Conferi a prévia e autorizo importar os registros para o Supabase.",
                                key=f"occ_legacy_confirm_{hub}")
        if st.button("📥 Importar histórico para o dashboard", type="primary", disabled=not confirmed,
                     key=f"occ_legacy_commit_{hub}"):
            if not info["records"]:
                st.warning("Não há linhas válidas para importar.")
            else:
                try:
                    with st.spinner("Salvando registros históricos..."):
                        inserted, already, overlaps = _historical_commit(sb, info["records"], hub)
                    st.success(f"Histórico processado! {inserted} registros adicionados; "
                               f"{already} já importados; {overlaps} correspondências com Google ignoradas.")
                    st.session_state[f"occ_refresh_{hub}"] = datetime.now().isoformat()
                except Exception as exc:
                    st.error(f"Falha ao salvar histórico: {exc}")


# ===========================================================================
# Dashboard gerencial e ficha individual do motorista
# ===========================================================================
def _render_analytics(sb, hub):
    import pandas as pd
    from collections import Counter
    st.markdown("### 📊 Inteligência de ocorrências")
    try:
        data = _get_all(sb, hub)
    except Exception as exc:
        st.warning(f"Histórico indisponível: {exc}")
        return
    if not data:
        st.info("Ainda não existem registros sincronizados. Importe o histórico Excel ou um romaneio.")
        return
    df = pd.DataFrame(data)
    for col in ("motorista", "ocorrencia", "turno", "observacao", "reversao", "ciclo"):
        if col not in df:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    df["driver_id"] = df["motorista"].map(_norm)
    df["motivo_norm"] = df["ocorrencia"].map(_norm)
    df["dia"] = pd.to_datetime(df["data_romaneio"], errors="coerce")
    df = df[df["dia"].notna()].copy()
    if df.empty:
        st.warning("Não há registros com datas válidas.")
        return
    st.markdown("#### 🔎 Filtros")
    c1,c2,c3 = st.columns([1.25, 1.1, 1.4])
    all_days = sorted(df["dia"].dt.date.unique())
    with c1:
        selection = st.date_input("Período", value=(all_days[0], all_days[-1]),
                                  min_value=all_days[0], max_value=all_days[-1],
                                  format="DD/MM/YYYY", key=f"occ_period_{hub}")
    with c2:
        options = sorted([x for x in df["turno"].unique() if x and x != "N/I"])
        shift = st.selectbox("Turno", ["Todos"] + options, key=f"occ_filter_shift_{hub}")
    with c3:
        top_reasons = sorted(df["ocorrencia"].dropna().unique())
        reason = st.selectbox("Ocorrência", ["Todas"] + top_reasons, key=f"occ_filter_reason_{hub}")
    filtered = df
    if isinstance(selection, tuple) or isinstance(selection, list):
        if len(selection) == 2:
            filtered = filtered[filtered["dia"].dt.date.between(selection[0],selection[1])]
    elif isinstance(selection, date):
        filtered = filtered[filtered["dia"].dt.date == selection]
    if shift != "Todos":
        filtered = filtered[filtered["turno"] == shift]
    if reason != "Todas":
        filtered = filtered[filtered["ocorrencia"] == reason]
    unique = filtered["driver_id"].nunique()
    count_by = filtered.groupby("driver_id").size() if not filtered.empty else pd.Series(dtype=int)
    recurring = int((count_by > 1).sum())
    last_7 = filtered[filtered["dia"] >= df["dia"].max() - pd.Timedelta(days=6)]
    x1,x2,x3,x4 = st.columns(4)
    x1.metric("📋 Ocorrências", f"{len(filtered):,}".replace(",", "."))
    x2.metric("👥 Motoristas distintos", unique)
    x3.metric("🔁 Reincidentes", recurring)
    x4.metric("📅 Últimos 7 dias da base", len(last_7))
    st.caption("Reincidência = motorista com duas ou mais ocorrências no período filtrado. "
               "Motoristas são identificados pelo nome normalizado; homônimos podem aparecer juntos.")
    tabs = st.tabs(["📈 Visão gerencial", "👤 Perfil do driver", "🗂️ Registros detalhados"])
    with tabs[0]:
        if filtered.empty:
            st.info("Nenhuma ocorrência para os filtros escolhidos.")
        else:
            left,right = st.columns([1.1,1])
            with left:
                st.markdown("**Evolução diária**")
                daily = filtered.groupby(filtered["dia"].dt.date).size().rename("Ocorrências")
                st.line_chart(daily, height=290)
            with right:
                st.markdown("**Principais motivos**")
                reasons = filtered.groupby("motivo_norm").size().sort_values(ascending=False).head(10)
                st.bar_chart(reasons, horizontal=True, height=290)
            left,right = st.columns([1.1,1])
            with left:
                st.markdown("**Top 15 motoristas por ocorrências**")
                names = filtered.groupby("driver_id")["motorista"].first()
                ranks = filtered.groupby("driver_id").size().sort_values(ascending=False).head(15)
                ranking = pd.DataFrame({"Motorista": [names.get(x,x) for x in ranks.index],
                                        "Ocorrências": ranks.values})
                ranking.index = range(1,len(ranking)+1)
                st.dataframe(ranking, use_container_width=True, height=355)
            with right:
                st.markdown("**Ocorrências por mês**")
                monthly = filtered.groupby(filtered["dia"].dt.to_period("M")).size()
                monthly.index = monthly.index.astype(str)
                st.bar_chart(monthly, height=300)
    with tabs[1]:
        names = df.groupby("driver_id")["motorista"].first()
        counts = df.groupby("driver_id").size().sort_values(ascending=False)
        options = list(counts.index)
        search = st.text_input("Pesquisar motorista pelo nome", placeholder="Digite parte do nome...",
                               key=f"occ_search_driver_{hub}")
        if search.strip():
            match = _norm(search)
            options = [x for x in options if match in x]
        if not options:
            st.info("Nenhum motorista encontrado.")
        else:
            chosen = st.selectbox("Selecione o driver", options=options,
                                  format_func=lambda x: f"{names[x]}  ·  {counts[x]} ocorrência(s)",
                                  key=f"occ_profile_driver_{hub}")
            profile = df[df["driver_id"] == chosen].sort_values("dia", ascending=False)
            title = names[chosen]
            st.markdown(f"#### 👤 {title}")
            p1,p2,p3,p4 = st.columns(4)
            p1.metric("Total histórico", len(profile))
            p2.metric("Tipos de ocorrência", profile["motivo_norm"].nunique())
            p3.metric("Primeira ocorrência", profile["dia"].min().strftime("%d/%m/%Y"))
            p4.metric("Última ocorrência", profile["dia"].max().strftime("%d/%m/%Y"))
            left,right = st.columns(2)
            with left:
                st.markdown("**Motivos mais frequentes**")
                st.bar_chart(profile.groupby("motivo_norm").size().sort_values(ascending=False).head(10), horizontal=True)
            with right:
                st.markdown("**Ocorrências por mês**")
                monthly = profile.groupby(profile["dia"].dt.to_period("M")).size()
                monthly.index = monthly.index.astype(str)
                st.bar_chart(monthly)
            st.markdown("**Linha do tempo completa do motorista**")
            detail = profile[["dia","turno","ocorrencia","reversao","observacao"]].copy()
            detail["dia"] = detail["dia"].dt.strftime("%d/%m/%Y")
            detail.columns = ["Data", "Turno", "Ocorrência", "Reversão", "Observação"]
            st.dataframe(detail, use_container_width=True, hide_index=True)
            st.download_button("📥 Exportar perfil CSV", detail.to_csv(index=False, sep=";").encode("utf-8-sig"),
                               file_name=f"perfil_driver_{hub}.csv", mime="text/csv", key=f"occ_export_profile_{hub}")
    with tabs[2]:
        cols = ["data_romaneio","ciclo","motorista","ocorrencia","data_ocorrencia","turno","reversao","observacao"]
        detail = filtered[cols].copy() if not filtered.empty else df.iloc[0:0][cols].copy()
        detail = detail.rename(columns={"data_romaneio":"Data romaneio","ciclo":"Ciclo", "motorista":"Motorista",
                                        "ocorrencia":"Ocorrência","data_ocorrencia":"Data ocorrência", "turno":"Turno",
                                        "reversao":"Reversão","observacao":"Observação"})
        st.dataframe(detail, use_container_width=True, hide_index=True, height=440)
        st.download_button("📥 Exportar registros filtrados CSV", detail.to_csv(index=False, sep=";").encode("utf-8-sig"),
                           file_name=f"ocorrencias_{hub}.csv", mime="text/csv", key=f"occ_export_all_{hub}")
