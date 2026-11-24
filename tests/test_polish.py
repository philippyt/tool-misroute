from misroute.server.catalog import Tool, words
from misroute.server.polish import apply_e1_e2, clean_name, infer_chains, parallel_audiences, propose_clean


def test_words_splits_acronyms():
    assert words("IKOmraade_GetById") == ["ik", "omraade", "get", "by", "id"]
    assert words("IKSted_CreateIKSted") == ["ik", "sted", "create", "ik", "sted"]
    assert words("IArbeidsordre") == ["i", "arbeidsordre"]


def test_parallel_audiences_detects_external_collision():
    tools = [
        Tool("Arbeidsordrer_CreateArbeidsordre", route="POST /api/Arbeidsordrer", destructive=True),
        Tool("Arbeidsordrer_GetArbeidsordreDetaljer", route="GET /api/Arbeidsordrer/{id}", read_only=True),
        Tool("Ekstern_CreateArbeidsordre", route="POST /api/Ekstern/Arbeidsordre", destructive=True),
        Tool("Ekstern_GetArbeidsordreStatus", route="POST /api/Ekstern/Arbeidsordre/Status", read_only=True),
    ]
    found = parallel_audiences(tools)
    assert set(found) == {"Ekstern_CreateArbeidsordre", "Ekstern_GetArbeidsordreStatus"}


def test_clean_name_fixes_truncation_acronyms_parent_child_and_list_vs_get():
    taken: set[str] = set()
    t1 = Tool(
        "ArbeidsordreFrekvensGrupper_GetDetaljertArbeidsordreFrek",
        route="GET /api/ArbeidsordreFrekvensGrupper/{id}",
        op_id="ArbeidsordreFrekvensGrupper_GetDetaljertArbeidsordreFrekvensGruppeMedId",
        read_only=True,
    )
    t2 = Tool("Ansatte_GetGrupper", route="GET /api/Ansatte/me/grupper", op_id="Ansatte_GetGrupper", read_only=True)
    t3 = Tool(
        "Ansatte_GetGrupper2",
        route="GET /api/Ansatte/{ansattId}/Grupper",
        op_id="Ansatte_GetGrupper2",
        read_only=True,
    )
    t4 = Tool("IKOmraade_GetById", route="GET /api/IKOmraade/{id}", op_id="IKOmraade_GetById", read_only=True)
    t5 = Tool(
        "Skjema_GetById",
        route="GET /api/Arbeidsordrer/{id}/Skjema",
        op_id="Skjema_GetById",
        read_only=True,
    )
    t6 = Tool(
        "Ansatte_GetAnsatte",
        route="GET /api/Ansatte",
        op_id="Ansatte_GetAnsatte",
        output_schema={"type": "array", "items": {"type": "object", "properties": {"id": {}}}},
        read_only=True,
    )
    assert clean_name(t1, taken) == "get_arbeidsordre_frekvens_gruppe"
    assert clean_name(t2, taken) == "list_my_grupper"
    assert clean_name(t3, taken) == "list_ansatte_grupper"
    assert clean_name(t4, taken) == "get_ik_omraade_by_id"
    assert clean_name(t5, taken) == "get_arbeidsordre_skjema"
    assert clean_name(t6, taken) == "list_ansatte"


def test_infer_chains_matches_exact_compound_entity_and_rejects_downstream_id_tools():
    tools = [
        Tool(
            "get_arbeidsordre_detaljer",
            input_schema={"type": "object", "properties": {"arbeidsordreId": {}}, "required": ["arbeidsordreId"]},
            route="GET /api/Arbeidsordrer/{arbeidsordreId}",
            read_only=True,
        ),
        Tool(
            "get_arbeidsordre_frekvens_gruppe",
            input_schema={"type": "object", "properties": {"id": {}}, "required": ["id"]},
            route="GET /api/ArbeidsordreFrekvensGrupper/{id}",
            read_only=True,
        ),
        Tool(
            "get_ik_omraade_by_id",
            input_schema={"type": "object", "properties": {"id": {}}, "required": ["id"]},
            route="GET /api/IKOmraade/{id}",
            read_only=True,
        ),
        Tool(
            "get_ik_sted_by_ekstern_referanse",
            input_schema={"type": "object", "properties": {"eksternReferanse": {}}, "required": ["eksternReferanse"]},
            route="GET /api/IKSted/{eksternReferanse}",
            read_only=True,
        ),
        Tool(
            "list_ansatte_grupper",
            input_schema={"type": "object", "properties": {"ansattId": {}}, "required": ["ansattId"]},
            route="GET /api/Ansatte/{ansattId}/Grupper",
            read_only=True,
        ),
        Tool("list_ansatte", route="GET /api/Ansatte", read_only=True),
        Tool("list_my_grupper", route="GET /api/Ansatte/me/grupper", read_only=True),
        Tool("list_hendelser", route="POST /api/Hendelser/Paginert", read_only=True),
        Tool(
            "get_hendelse_detaljer",
            input_schema={"type": "object", "properties": {"hendelseId": {}}, "required": ["hendelseId"]},
            route="GET /api/Hendelser/{hendelseId}",
            read_only=True,
        ),
        Tool(
            "arbeidsordrer_view",
            read_only=True,
            selector="view",
            operations={"paginert": "Arbeidsordrer_Paginert", "count": "Arbeidsordrer_Count"},
        ),
        Tool(
            "vedlegg_by_parent",
            input_schema={"type": "object", "properties": {"parent": {}, "id": {}}, "required": ["parent", "id"]},
            read_only=True,
            selector="parent",
            operations={"arbeidsordrer": "Vedlegg_GetArbeidsordreVedlegg", "hendelser": "Vedlegg_GetHendelseVedlegg"},
        ),
        Tool(
            "reference_data",
            read_only=True,
            selector="kind",
            operations={
                "arbeidsordrefrekvensgrupper": "ArbeidsordreFrekvensGrupper_GetAll",
                "ikomraade": "IKOmraade_GetAll",
                "grupper": "Grupper_GetGrupper",
            },
        ),
    ]
    chains = infer_chains(tools)
    assert chains["get_arbeidsordre_detaljer"] == ["`arbeidsordrer_view(view='paginert')`"]
    assert chains["get_arbeidsordre_frekvens_gruppe"] == ["`reference_data(kind='arbeidsordrefrekvensgrupper')`"]
    assert chains["get_ik_omraade_by_id"] == ["`reference_data(kind='ikomraade')`"]
    assert "get_ik_sted_by_ekstern_referanse" not in chains
    assert chains["list_ansatte_grupper"] == ["`list_ansatte`"]
    assert chains["get_hendelse_detaljer"] == ["`list_hendelser`"]
    # Downstream tool vedlegg_by_parent requires id itself -> never suggested anywhere
    assert all("vedlegg_by_parent" not in hint for hints in chains.values() for hint in hints)


def test_propose_clean_excludes_singletons_and_infers_enrichments():
    tools = [
        Tool("Statuser_GetAll", route="GET /api/Statuser", read_only=True),
        Tool("Graderinger_GetAll", route="GET /api/Graderinger", read_only=True),
        Tool("FDVOmraader_GetAll", route="GET /api/FDVOmraader", read_only=True),
        Tool("Ansatte_GetGrupper", route="GET /api/Ansatte/me/grupper", read_only=True),
        Tool(
            "Org_GetInfo",
            route="GET /api/Org",
            output_schema={"type": "object", "properties": {"id": {}, "name": {}}},
            read_only=True,
        ),
        Tool(
            "Items_Get",
            route="GET /api/Items/{id}",
            output_schema={"type": "object", "properties": {"id": {}, "fdvOmraadeId": {}}},
            read_only=True,
        ),
    ]
    plan = propose_clean(tools)
    ref_ops = set(plan["tools"]["reference_data"]["operations"].values())
    assert ref_ops == {"Statuser_GetAll", "Graderinger_GetAll", "FDVOmraader_GetAll"}
    assert any(e["field"] == "fdvOmraadeId" and e["enabled"] is False for e in plan.get("enrich", []))
    polished, _, diag = apply_e1_e2(tools)
    assert polished and not diag["needs_human_name"]


def test_preserves_domain_arrays_ignores_generic_overlap_and_matches_merged_member_schemas():
    from misroute.server.polish import _item_fields, static_siblings

    gruppe = Tool(
        "get_gruppe",
        output_schema={
            "type": "object",
            "properties": {"id": {}, "navn": {}, "medlemmer": {"type": "array", "items": {}}},
        },
        read_only=True,
    )
    skjema = Tool(
        "get_arbeidsordre_skjema",
        output_schema={
            "type": "object",
            "properties": {"id": {}, "navn": {}, "sjekkpunkter": {"type": "array", "items": {}}},
        },
        read_only=True,
    )
    paginated = Tool(
        "list_hendelser",
        output_schema={
            "type": "object",
            "properties": {"sidenummer": {}, "hendelser": {"type": "array", "items": {}}, "beskrivelse": {}},
        },
        read_only=True,
    )
    assert "medlemmer" in _item_fields(gruppe)
    assert "sjekkpunkter" in _item_fields(skjema)
    assert "hendelser" not in _item_fields(paginated) and "sidenummer" not in _item_fields(paginated)

    # Generic overlap (id, navn) must not link unrelated tools
    sibs, probes = static_siblings([gruppe, skjema])
    assert not sibs and not probes

    # Merged tool inherits member response fields from source_tools, including envelope arrays
    src_vedlegg = Tool(
        "Vedlegg_GetArbeidsordreVedlegg",
        output_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {}, "contentType": {}, "size": {}, "url": {}}},
                }
            },
        },
        read_only=True,
    )
    merged_vedlegg = Tool(
        "vedlegg_by_parent",
        read_only=True,
        selector="parent",
        operations={"arbeidsordrer": "Vedlegg_GetArbeidsordreVedlegg"},
    )
    attachments = Tool(
        "list_attachments_metadata",
        output_schema={
            "type": "object",
            "properties": {
                "continuationToken": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {}, "contentType": {}, "size": {}, "lokalId": {}}},
                },
            },
        },
        read_only=True,
    )
    assert "contentType" in _item_fields(src_vedlegg) and "files" not in _item_fields(src_vedlegg)
    assert "contentType" in _item_fields(attachments) and "attachments" not in _item_fields(attachments)
    sibs2, probes2 = static_siblings([merged_vedlegg, attachments], source_tools=[src_vedlegg, attachments])
    assert any(p[:2] == ("vedlegg_by_parent", "list_attachments_metadata") for p in probes2)
    assert any(t.name == "list_attachments_metadata" for t in sibs2["vedlegg_by_parent"])
