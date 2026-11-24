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


def test_clean_name_fixes_truncation_acronyms_and_numbered_twins():
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
    n1 = clean_name(t1, taken)
    n2 = clean_name(t2, taken)
    n3 = clean_name(t3, taken)
    n4 = clean_name(t4, taken)
    assert not n1.endswith("_med") and len(n1) <= 48
    assert n2 == "list_my_grupper"
    assert n3 == "list_ansatte_grupper"
    assert n4 == "get_ik_omraade_by_id"


def test_same_effect_siblings_merged_ops_and_list_detail_chains():
    tools = [
        Tool(
            "Arbeidsordrer_GetArbeidsordreDetaljer",
            "Executes GET /api/Arbeidsordrer/{arbeidsordreId}",
            {"type": "object", "properties": {"arbeidsordreId": {"type": "string"}}, "required": ["arbeidsordreId"]},
            {"type": "object", "properties": {"id": {}, "tittel": {}, "status": {}}},
            read_only=True,
            route="GET /api/Arbeidsordrer/{arbeidsordreId}",
            op_id="Arbeidsordrer_GetArbeidsordreDetaljer",
        ),
        Tool(
            "Arbeidsordrer_CreateArbeidsordre",
            "Executes POST /api/Arbeidsordrer",
            {"type": "object", "properties": {"tittel": {"type": "string"}}, "required": ["tittel"]},
            read_only=False,
            destructive=False,
            route="POST /api/Arbeidsordrer",
            op_id="Arbeidsordrer_CreateArbeidsordre",
        ),
        Tool(
            "Arbeidsordrer_UpdateArbeidsordre",
            "Executes PUT /api/Arbeidsordrer/{arbeidsordreId}",
            {"type": "object", "properties": {"arbeidsordreId": {"type": "string"}}, "required": ["arbeidsordreId"]},
            read_only=False,
            destructive=False,
            idempotent=True,
            route="PUT /api/Arbeidsordrer/{arbeidsordreId}",
            op_id="Arbeidsordrer_UpdateArbeidsordre",
        ),
        Tool(
            "arbeidsordrer_view",
            "Consolidated view",
            read_only=True,
            selector="view",
            operations={"paginert": "Arbeidsordrer_PaginertArbeidsordreListe", "count": "Arbeidsordrer_Count"},
        ),
    ]
    polished, _, diag = apply_e1_e2(tools)
    by = {t.name: t for t in polished}
    # Reads should NOT reference write tools; detail read should point to list view first
    assert "create_arbeidsordre" not in by["get_arbeidsordre_detaljer"].description
    assert "arbeidsordrer_view(view='paginert')" in by["get_arbeidsordre_detaljer"].description
    # Writes should point to sibling writes, not reads
    assert "update_arbeidsordre" in by["create_arbeidsordre"].description
    assert "get_arbeidsordre_detaljer" not in by["create_arbeidsordre"].description
    assert not diag["needs_human_name"]
    assert "get_arbeidsordre_detaljer" in infer_chains(polished)


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
