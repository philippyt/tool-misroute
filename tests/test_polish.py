from misroute.server.catalog import Tool
from misroute.server.polish import apply_e1_e2, clean_name, parallel_audiences, propose_clean


def test_parallel_audiences_detects_external_collision():
    tools = [
        Tool("Arbeidsordrer_CreateArbeidsordre", route="POST /api/Arbeidsordrer", destructive=True),
        Tool("Arbeidsordrer_GetArbeidsordreDetaljer", route="GET /api/Arbeidsordrer/{id}", read_only=True),
        Tool("Ekstern_CreateArbeidsordre", route="POST /api/Ekstern/Arbeidsordre", destructive=True),
        Tool("Ekstern_GetArbeidsordreStatus", route="POST /api/Ekstern/Arbeidsordre/Status", read_only=True),
    ]
    found = parallel_audiences(tools)
    assert set(found) == {"Ekstern_CreateArbeidsordre", "Ekstern_GetArbeidsordreStatus"}


def test_clean_name_strips_redundant_controller_and_avoids_truncation():
    taken: set[str] = set()
    t1 = Tool(
        "ArbeidsordreFrekvensGrupper_OpprettArbeidsordreFrekvensG",
        op_id="ArbeidsordreFrekvensGrupper_OpprettArbeidsordreFrekvensGruppe",
    )
    t2 = Tool("Arbeidsordrer_GetArbeidsordreDetaljer", op_id="Arbeidsordrer_GetArbeidsordreDetaljer")
    n1 = clean_name(t1, taken)
    n2 = clean_name(t2, taken)
    assert n1 == "create_arbeidsordre_frekvens_gruppe"
    assert n2 == "get_arbeidsordre_detaljer"
    assert len(n1) <= 48 and len(n2) <= 48


def test_apply_e1_e2_adds_grounded_descriptions_and_boundary_clauses():
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
            destructive=True,
            route="POST /api/Arbeidsordrer",
            op_id="Arbeidsordrer_CreateArbeidsordre",
        ),
    ]
    polished, rmap = apply_e1_e2(tools)
    assert rmap["Arbeidsordrer_GetArbeidsordreDetaljer"] == "get_arbeidsordre_detaljer"
    assert rmap["Arbeidsordrer_CreateArbeidsordre"] == "create_arbeidsordre"
    by = {t.name: t for t in polished}
    assert not by["get_arbeidsordre_detaljer"].description.startswith("Executes ")
    assert "arbeidsordreId" in by["get_arbeidsordre_detaljer"].description
    assert "Not for" in by["get_arbeidsordre_detaljer"].description
    assert "`create_arbeidsordre`" in by["get_arbeidsordre_detaljer"].description
    assert "`get_arbeidsordre_detaljer`" in by["create_arbeidsordre"].description


def test_propose_clean_excludes_caller_scoped_from_reference_data():
    tools = [
        Tool("Statuser_GetAll", route="GET /api/Statuser", read_only=True),
        Tool("Graderinger_GetAll", route="GET /api/Graderinger", read_only=True),
        Tool("FDVOmraader_GetAll", route="GET /api/FDVOmraader", read_only=True),
        Tool("Ansatte_GetGrupper", route="GET /api/Ansatte/me/grupper", read_only=True),
        Tool("Virksomhet_GetVirksomhetDetaljer", route="GET /api/Virksomhet", read_only=True),
    ]
    plan = propose_clean(tools)
    ref_ops = set(plan["tools"]["reference_data"]["operations"].values())
    assert ref_ops == {"Statuser_GetAll", "Graderinger_GetAll", "FDVOmraader_GetAll"}
