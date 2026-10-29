from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

app = MCPServer("saksbehandling")


@app.tool(description="Written comments that caseworkers posted on a case.", annotations=READ)
def get_case_comments(case_id: str) -> dict:
    return {"case_id": case_id, "comments": [{"author": "kari", "text": "ringte kunden"}]}


@app.tool(description="Timeline events of a case such as status changes and assignments.", annotations=READ)
def get_case_events(case_id: str) -> dict:
    return {"case_id": case_id, "events": [{"type": "status", "to": "open"}]}


@app.tool(description="Change the status of a case, for example open or closed.", annotations=WRITE)
def update_case(case_id: str, status: str) -> dict:
    return {"case_id": case_id, "status": status}


@app.tool(description="Change the tags and labels attached to a case.", annotations=WRITE)
def update_case_meta(case_id: str, tags: list[str]) -> dict:
    return {"case_id": case_id, "tags": tags}


@app.tool(description="Search cases by free text in titles and notes.", annotations=READ)
def search_cases(query: str, cursor: str | None = None) -> dict:
    return {"items": [{"id": "s1"}], "next_cursor": None}


@app.tool(description="List all open cases, newest first, one page at a time.", annotations=READ)
def list_cases(cursor: str | None = None) -> dict:
    return {"items": [{"id": "c1"}], "next_cursor": None}


@app.tool(description="GET /api/dokumenter/{id}", annotations=READ)
def get_document(document_id: str) -> dict:
    return {"id": document_id}


@app.tool(description="", annotations=READ)
def get_attachment(attachment_id: str) -> dict:
    return {"id": attachment_id}


@app.tool(description="Customer contact details, phone number and postal address.", annotations=READ)
def get_customer(customer_id: str) -> dict:
    return {"id": customer_id, "name": "Ola Nordmann"}


if __name__ == "__main__":
    app.run()
