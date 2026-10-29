        clusters_escolhidos = st.multiselect(
            "Escolha os clusters", clusters,
            placeholder="Selecione um ou mais clusters",
            key=f"capt_clusters_multiplos_v3_{hub}"
        )
    with c3:
        filtro_ats = st.selectbox("ATs", ["Todas", "0 ATs", "1+ ATs"], key=f"capt_ats_{hub}")
    with c4:
        filtro_status = st.selectbox("Status", ["Todos"]+CAPTACAO_STATUS, key=f"capt_status_{hub}")
    filtro_prior = st.checkbox("Somente clusters prioritários (histórico de reutilização)", key=f"capt_prior_{hub}")
    filtro_clusters_chaves = {captacao_chave_cluster(c) for c in clusters_escolhidos}
    prioritarios_chaves = {captacao_chave_cluster(c) for c in CAPTACAO_PRIORITARIOS}

    filtrados=[]
    for r in registros:
        if busca and normalizar_nome(busca) not in normalizar_nome(r["nome"]) and busca.strip() not in str(r["driver_id"]):
            continue
        chaves_motorista = {captacao_chave_cluster(c) for c in r["clusters_lista"]}
        if filtro_clusters_chaves and not (filtro_clusters_chaves & chaves_motorista or "all" in chaves_motorista):
            continue
        if filtro_ats == "0 ATs" and r["ats"] != 0:
            continue
        if filtro_ats == "1+ ATs" and r["ats"] == 0:
            continue
        if filtro_status != "Todos" and r["status"] != filtro_status:
            continue
        if filtro_prior and not (prioritarios_chaves & chaves_motorista or "all" in chaves_motorista):
            continue
        filtrados.append(r)
    st.caption(f"{len(filtrados)} motorista(s) encontrados. Você pode selecionar vários clusters; será exibido quem atende pelo menos um deles. 'ALL' indica cobertura geral.")
    if not filtrados:
        st.info("Nenhum motorista corresponde aos filtros.")
        return
    visiveis = [{"ID": r["driver_id"],"Motorista":r["nome"],"ATs concluídas":r["ats"],"Clusters":" | ".join(r["clusters_lista"]),"Status":r["status"],"Observações":r["observacao"]} for r in filtrados]
    st.dataframe(visiveis, use_container_width=True, hide_index=True, height=380)
    dados_csv = StringIO()
    gravador = csv.DictWriter(dados_csv, fieldnames=list(visiveis[0]), delimiter=";")
    gravador.writeheader()
    gravador.writerows(visiveis)
    st.download_button("📥 Exportar resultado CSV", data="\ufeff"+dados_csv.getvalue(), file_name=f"captacao_{hub}.csv", mime="text/csv", key=f"capt_csv_{hub}")

    st.markdown("#### Atualizar acompanhamento")
    id_lookup = {f"{r['nome']} — ID {r['driver_id']}":r for r in filtrados}
    escolhido = st.selectbox("Selecione o motorista", list(id_lookup), key=f"capt_selecionar_{hub}")
    driver = id_lookup[escolhido]
    with st.form(f"capt_form_{hub}_{driver['driver_id']}"):
        status = st.selectbox("Status da captação", CAPTACAO_STATUS, index=CAPTACAO_STATUS.index(driver["status"]) if driver["status"] in CAPTACAO_STATUS else 0)
        observacao = st.text_area("Observação operacional (não inclua telefone, CPF ou outros dados pessoais)", value=driver["observacao"], max_chars=500)
        if st.form_submit_button("💾 Salvar acompanhamento", type="primary"):
            captacao_salvar_status(hub,driver["driver_id"],status,observacao)
            st.success("Acompanhamento salvo neste ambiente.")
            st.rerun()
    st.info("Para obter o telefone, abra o cadastro individual no SPX e utilize a visualização autorizada. Nenhum número é coletado em lote nesta versão.")
    st.caption("Armazenamento atual: SQLite local (captacao_drivers.sqlite3). Em hospedagem na nuvem com disco temporário, os registros de acompanhamento podem ser perdidos em reinicializações; para uso multiusuário, migre esta tabela para o Supabase com políticas de acesso por hub.")


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

    # Enquanto esta sessão do navegador estiver aberta, verifica a agenda a cada minuto.
    render_motor_atualizacao_automatica(hub_atual)

    aba_dashboard, aba_ranking, aba_inteligencia, aba_aceite, aba_captacao, aba_config = st.tabs([
        f"📊 Dashboard {hub_atual}",
        f"🏆 Ranking {hub_atual}",
        f"📈 Inteligência {hub_atual}",
        f"📲 Aceite de Rotas {hub_atual}",
        f"🎯 Captação {hub_atual}",
        f"⚙️ Configuração {hub_atual}"
    ])

    with aba_dashboard:
        render_dashboard_hub(hub_atual)

    with aba_ranking:
        render_ranking_hub(hub_atual)

    with aba_inteligencia:
        render_inteligencia_operacional(hub_atual)

    with aba_aceite:
        render_aceite_rotas(hub_atual)

    with aba_captacao:
        render_captacao_drivers(hub_atual)

    with aba_config:
        render_configuracao_hub(hub_atual)
