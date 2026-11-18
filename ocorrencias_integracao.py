"""Integração de romaneios Google Sheets com gestão de ocorrências no Streamlit.

Requer a tabela SQL em ocorrencias_schema.sql. Os romaneios permanecem somente-leitura.
"""
import csv
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from io import StringIO
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st

COLUNAS = {
    "motorista": ["DRIVER", "DRIVER NAME", "MOTORISTA", "NOME DO MOTORISTA", "NOME DRIVER"],
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
    cred = _google_credentials()
    if cred:
        # Busca metadados para resolver o título da aba pelo gid, sem supor seu nome.
        headers = {"Authorization": f"Bearer {cred.token}"}
        root = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        meta = requests.get(root, headers=headers, params={"fields": "sheets(properties(sheetId,title))"}, timeout=25)
        meta.raise_for_status()
        abas = meta.json().get("sheets", [])
        title = next((a["properties"]["title"] for a in abas if str(a.get("properties", {}).get("sheetId")) == gid), None)
        if title is None:
            raise ValueError(f"A aba gid={gid} não foi encontrada nesse romaneio.")
        res = requests.get(root + "/values/" + requests.utils.quote(title, safe=""),
                           headers=headers, params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=40)
        res.raise_for_status()
        return res.json().get("values", []), title
    # Leitura sem credenciais apenas se a empresa já tiver autorizado acesso externo.
    uri = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    res = requests.get(uri, params={"format": "csv", "gid": gid}, timeout=40)
    res.raise_for_status()
    content_type = res.headers.get("Content-Type", "").lower()
    if "html" in content_type or res.text.lstrip().lower().startswith("<!doctype html"):
        raise PermissionError("O romaneio não está disponível para leitura anônima. Configure uma conta de serviço autorizada.")
    return list(csv.reader(StringIO(res.content.decode("utf-8-sig", errors="replace")))), f"gid={gid}"


def _find_header(rows):
    for i, row in enumerate(rows[:45]):
        normalized = [_norm(v) for v in row]
        hits = sum(any(_norm(x) in normalized for x in choices) for choices in COLUNAS.values())
        if hits >= 2 and any(x in normalized for x in ["DRIVER", "MOTORISTA", "DRIVER NAME"]):
            return i
    raise ValueError("Não encontrei os cabeçalhos da aba de ocorrências. Confira o gid ou selecione a aba correta.")


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
            st.caption("Atenção: o link deve apontar para a aba de ocorrências. Se apontar para outra aba, altere o gid no Google Sheets.")
        if st.button("🔎 Ler e conferir ocorrências", key=f"occ_fetch_{hub}", type="primary"):
            try:
                spreadsheet_id, gid = parse_google_link(url)
                with st.spinner("Lendo romaneio com acesso somente-leitura..."):
                    rows, nome_aba = _sheet_csv(spreadsheet_id, gid)
                    idx = _find_header(rows)
                    headers = [str(x).strip() or f"Coluna_{i+1}" for i, x in enumerate(rows[idx])]
                st.session_state[f"occ_preview_{hub}"] = {
                    "rows": rows, "idx": idx, "headers": headers,
                    "mapping": _guess_columns(headers), "sheet_id": spreadsheet_id,
                    "gid": gid, "nome_aba": nome_aba,
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
    st.markdown("#### 📊 Histórico e indicadores")
    try:
        data = _get_all(sb, hub)
    except Exception as exc:
        st.warning(f"Histórico ainda indisponível: {exc}")
        return
    if not data:
        st.info("Ainda não há ocorrências sincronizadas neste hub.")
        return
    from collections import Counter
    drivers = Counter(_norm(x.get("motorista")) for x in data)
    motivos = Counter(_norm(x.get("ocorrencia")) for x in data)
    c1, c2, c3 = st.columns(3)
    c1.metric("Ocorrências", len(data))
    c2.metric("Motoristas distintos (por nome)", len(drivers))
    c3.metric("Motoristas reincidentes", sum(v > 1 for v in drivers.values()))
    f1, f2 = st.columns(2)
    with f1:
        st.markdown("**Tipos de ocorrência**")
        st.bar_chart(dict(motivos.most_common(15)))
    with f2:
        st.markdown("**Top motoristas por recorrência**")
        st.bar_chart(dict(drivers.most_common(15)))
    st.markdown("**Registros sincronizados**")
    cols = ["data_romaneio", "ciclo", "motorista", "ocorrencia", "data_ocorrencia", "turno", "observacao"]
    import pandas as pd
    df = pd.DataFrame([{key: item.get(key, "") for key in cols} for item in data])
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button("📥 Exportar histórico CSV", data=df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"ocorrencias_{hub}.csv", mime="text/csv", key=f"occ_export_{hub}")
