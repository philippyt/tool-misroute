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
    arr = {"type": "array", "items": {"type": "object", "properties": {"id": {}, "name": {}}}}
    tools = [
        Tool("Statuser_GetAll", route="GET /api/Statuser", output_schema=arr, read_only=True),
        Tool("Graderinger_GetAll", route="GET /api/Graderinger", output_schema=arr, read_only=True),
        Tool("FDVOmraader_GetAll", route="GET /api/FDVOmraader", output_schema=arr, read_only=True),
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
    assert any(t.name == "vedlegg_by_parent" for t in sibs2["list_attachments_metadata"])


def test_merged_description_qualified_id_and_primitive_array_return():
    skjema = Tool(
        "Skjema_GetById",
        "Executes GET /api/Arbeidsordrer/{id}/Skjema",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        output_schema={"type": "object", "properties": {"id": {}, "navn": {}}},
        read_only=True,
        route="GET /api/Arbeidsordrer/{id}/Skjema",
        op_id="Skjema_GetById",
    )
    id_list = Tool(
        "ArbeidsordreFrekvensGrupper_GetIder",
        "Executes GET /api/ArbeidsordreFrekvensGrupper/{id}/arbeidsordreIGruppe",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        output_schema={"type": "array", "items": {"type": "integer"}},
        read_only=True,
        route="GET /api/ArbeidsordreFrekvensGrupper/{id}/arbeidsordreIGruppe",
        op_id="ArbeidsordreFrekvensGrupper_GetIder",
    )
    ref = Tool(
        "reference_data",
        "reference data. Set kind to choose: grupper: The endpoint is access based.",
        read_only=True,
        selector="kind",
        operations={"grupper": "Grupper_GetAll", "statuser": "Statuser_GetAll"},
    )
    out, renames, _ = apply_e1_e2([skjema, id_list, ref])
    by = {t.name: t for t in out}
    assert "Requires: id (arbeidsordrer)." in by["get_arbeidsordre_skjema"].description
    assert "Returns: list of IDs." in by[renames["ArbeidsordreFrekvensGrupper_GetIder"]].description
    assert by["reference_data"].description.startswith("Reference data. Set `kind` to choose: grupper, statuser.")
    assert "access based" not in by["reference_data"].description


def test_strict_twin_matching_and_head_noun_filtering():
    from misroute.server.polish import static_siblings

    tools = [
        Tool("Arbeidsordrer_CreateArbeidsordre", route="POST /api/Arbeidsordrer", destructive=False),
        Tool(
            "Arbeidsordrer_GetArbeidsordreDetaljer",
            route="GET /api/Arbeidsordrer/{arbeidsordreId}",
            input_schema={"type": "object", "properties": {"arbeidsordreId": {"type": "integer"}}},
            output_schema={"type": "object", "properties": {"id": {}, "beskrivelse": {}, "status": {}}},
            read_only=True,
        ),
        Tool("Ekstern_CreateArbeidsordre", route="POST /api/Ekstern/Arbeidsordre", destructive=False),
        Tool(
            "Ekstern_GetArbeidsordreStatus",
            route="POST /api/Ekstern/Arbeidsordre/Status",
            input_schema={"type": "object", "properties": {"ids": {"type": "array"}}},
            output_schema={"type": "array", "items": {"type": "object", "properties": {"id": {}, "statusNavn": {}}}},
            read_only=True,
        ),
    ]
    plan = propose_clean(tools, split_audiences=True)
    assert "duplicating `Arbeidsordrer_CreateArbeidsordre`" in plan["exclude"]["Ekstern_CreateArbeidsordre"]
    assert "no verified primary twin" in plan["exclude"]["Ekstern_GetArbeidsordreStatus"]
    assert any(v["op_id"] == "Ekstern_GetArbeidsordreStatus" for v in plan.get("verify", []))

    # Modifier-only overlap ("arbeidsordre") must not pair frekvens_gruppe with update_arbeidsordre
    w1 = Tool("create_arbeidsordre_frekvens_gruppe", destructive=False)
    w2 = Tool("update_arbeidsordre", destructive=True)
    sibs, probes = static_siblings([w1, w2])
    assert not sibs and not probes

    # Generic binary-sibling note works for any domain noun (not just attachment/vedlegg)
    doc_meta = Tool("Documents_List", route="GET /api/documents", read_only=True, op_id="Documents_List")
    out, _, _ = apply_e1_e2(
        [doc_meta],
        excluded={"Documents_Download": "binary response; excluded by default unless wrapped with a file/URL handler"},
    )
    assert "Returns metadata only; raw file download endpoints are excluded." in out[0].description


def test_dev_spec_generalization_fixes():
    from misroute.server import merge, openapi
    from misroute.server.polish import static_siblings

    # 1. FastAPI snake_case names stay verb-first (never scrambled into asset_get / backfill_create)
    taken: set[str] = set()
    fa_get = Tool("get_asset", route="GET /api/v2/assets/{asset_id}", read_only=True, op_id="get_asset")
    fa_list = Tool(
        "get_assets",
        route="GET /api/v2/assets",
        output_schema={"type": "object", "properties": {"assets": {"type": "array", "items": {}}}},
        read_only=True,
        op_id="get_assets",
    )
    fa_create = Tool("create_backfill", route="POST /api/v2/backfills", destructive=False, op_id="create_backfill")
    assert clean_name(fa_get, taken) == "get_asset"
    assert clean_name(fa_list, taken) == "list_assets"
    assert clean_name(fa_create, taken) == "create_backfill"

    # 2. Whole-token POST read detection: createWithList is a write, Paginert/Status are reads
    assert not openapi.reads("post", "/user/createWithList", {"operationId": "createUsersWithListInput"})
    assert openapi.reads("post", "/api/Arbeidsordrer/Paginert", {"operationId": "Arbeidsordrer_Paginert"})
    assert openapi.reads("post", "/api/Ekstern/Hendelse/Status", {"operationId": "Ekstern_GetHendelseStatus"})

    # 3. merge.propose never names a *_view group after a {param} parent
    param_sub = [
        Tool("a", route="GET /dags/{dag_id}/links", read_only=True),
        Tool("b", route="GET /dags/{dag_id}/tries", read_only=True),
        Tool("parent", route="GET /dags/{dag_id}", read_only=True),
    ]
    assert not any("dag_id" in k for k in merge.propose(param_sub)["tools"])

    # 4. CRUD delete pairs are dropped, create<->update is kept
    c = Tool("add_pet", route="POST /pet", destructive=False)
    u = Tool("update_pet", route="PUT /pet", destructive=True)
    d = Tool("delete_pet", route="DELETE /pet/{petId}", destructive=True)
    _, probes = static_siblings([c, u, d])
    assert [p[:2] for p in probes] == [("add_pet", "update_pet")]

    # 5. Structural uploads() ignores JSON properties merely named "binary"
    qdrant_body = {
        "type": "object",
        "properties": {
            "quantization_config": {"type": "object", "properties": {"binary": {"type": "object"}}},
            "file": {"type": "string", "format": "binary"},
        },
    }
    assert openapi.uploads(qdrant_body) == ["file"]

    # 6. Path-key pointers ({collection_name}) and verb-last list tools (droplets_list)
    from misroute.server.polish import split_by_tag

    col_list = Tool(
        "list_collections",
        route="GET /collections",
        output_schema={"type": "array", "items": {"type": "object", "properties": {"name": {}}}},
        read_only=True,
    )
    col_get = Tool(
        "collection_info",
        route="GET /collections/{collection_name}",
        input_schema={
            "type": "object",
            "properties": {"collection_name": {"type": "string"}},
            "required": ["collection_name"],
        },
        read_only=True,
    )
    drop_list = Tool("droplets_list", route="GET /v2/droplets", read_only=True)
    drop_get = Tool(
        "droplets_get",
        route="GET /v2/droplets/{droplet_id}",
        input_schema={"type": "object", "properties": {"droplet_id": {"type": "integer"}}, "required": ["droplet_id"]},
        read_only=True,
    )
    ch = infer_chains([col_list, col_get, drop_list, drop_get])
    assert ch["collection_info"] == ["`list_collections`"]
    assert ch["droplets_get"] == ["`droplets_list`"]

    # 7. Tag-based toolset splitting and findings.json builder
    from misroute.server.polish import append_discover_findings, build_findings

    doc = {
        "paths": {"/v2/droplets": {"get": {"tags": ["Droplets"]}}, "/collections": {"get": {"tags": ["Collections"]}}}
    }
    sets = split_by_tag([col_list, drop_list], doc)
    assert set(sets) == {"collections", "droplets"}
    fdoc = build_findings(
        [col_list, col_get],
        {"verify": [{"op_id": "Features_Get", "reason": "octet-stream"}]},
        {"probe_candidates": [("a", "b", "domain stem overlap")], "needs_human_name": ["bad_2"]},
        toolsets={"mega": [col_list] * 35},
    )
    kinds = {f["kind"] for f in fdoc["findings"]}
    assert {"collision", "verify_endpoint", "name_review", "oversized_toolset"} <= kinds
    assert all(d["status"] == "draft" for d in fdoc["drafts"])
    merged_f = append_discover_findings(
        fdoc, {"flagged": [{"pair": ["a", "b"], "errors": 7, "trials": 96, "error_rate": 0.07, "example": "get a"}]}
    )
    assert any(f["source"] == "discover" and f["tools"] == ["a", "b"] for f in merged_f["findings"])
