from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.auth_router import require_auth
from app.auth_router import router as auth_router
from app.config.model_config import (
    DEFAULT_DEPLOYMENT,
    FALLBACK_DEPLOYMENT,
    resolve_deployment,
    validate_startup_config,
)
from app.routergenerator import (
    init_graph_resources,
    router as graph_router,
    shutdown_graph_resources,
)
from app.services.azure_search_service import retrieve_grounding
from app.services.file_extraction import extract_and_log
from app.services.openai_service import client, create_completion_with_failover
from app.services.prompt_templates import build_system_message, build_user_message, resolve_format_instruction
from app.services import telemetry

load_dotenv()

# Must run before FastAPI() is instantiated -- configure_azure_monitor()'s
# auto-instrumentation patches the FastAPI class itself, so any app created
# afterward picks up request/response tracing automatically.
telemetry.configure()

app = FastAPI(
    title="PayerIQ Business Analyst API",
    description="Backend API for PayerIQ integrating Azure OpenAI for automated requirement document generation.",
    version="1.0.0",
)

# CORS configuration allowing cross-origin calls from your Azure Static Web App.
# No wildcard here on purpose -- allow_credentials=True means Starlette must
# reflect a specific request Origin, and it would happily reflect ANY origin
# if "*" were still in this list, letting any site make credentialed calls
# against this API from a visitor's browser. Add new legitimate frontends
# here explicitly instead.
origins = [
    "https://icy-water-06fd47710.3.azurestaticapps.net",
    "http://localhost:8000",
    "http://localhost:3000",
    "http://localhost:5173",  # Vite's default dev server port -- the frontend actually runs here locally
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fail fast on boot if no default model deployment is configured, rather than
# discovering it on the first /generate request. See app/config/model_config.py
# for the single source of truth on which deployment(s) this service can use.
validate_startup_config()


@app.on_event("startup")
async def _startup():
    # Opens the Cosmos DB client and runs checkpointer.setup() for the LangGraph
    # pipeline mounted below at /v2. Deliberately separate from
    # validate_startup_config() above: that one is a pure config check, this
    # one makes a live connection and belongs in the async startup hook, not
    # at import time.
    await init_graph_resources()


@app.on_event("shutdown")
async def _shutdown():
    await shutdown_graph_resources()


app.include_router(graph_router, prefix="/v2")
app.include_router(auth_router, prefix="/v2/auth")


@app.get("/")
def read_root():
    """Health check root endpoint to verify API operation."""
    return {"status": "healthy", "service": "PayerIQ API"}


@app.get("/health")
async def health_check():
    """
    On-demand check that each configured model deployment actually responds.
    Not run at startup -- a live Azure call there would block boot and take the
    whole service down on a transient Azure hiccup. Hit this manually or wire
    it into an uptime monitor instead.
    """
    result = {"status": "healthy", "deployments": {}}
    for role, deployment in (("default", DEFAULT_DEPLOYMENT), ("fallback", FALLBACK_DEPLOYMENT)):
        if not deployment:
            continue
        try:
            await client.chat.completions.create(
                model=deployment,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            result["deployments"][role] = {"name": deployment, "reachable": True}
        except Exception as e:
            result["status"] = "degraded"
            result["deployments"][role] = {"name": deployment, "reachable": False, "error": str(e)}
    return result


@app.post("/generate")
async def generate(
    project_name: Optional[str] = Form(""),
    prompt: str = Form(...),
    formats: List[str] = Form(...),
    files: List[UploadFile] = File(default=[]),
    model: Optional[str] = Form(None),
    uploaded_by: Optional[str] = Form(None),
    _auth: dict = Depends(require_auth),
):
    """Processes uploaded source files and sends prompts to Azure OpenAI to return formatted analysis documents.

    Requires the same Bearer JWT as the /v2 routes -- this endpoint used to
    accept requests with no auth at all. It still does not run Prompt
    Shields or Groundedness (see /v2/generate for that); this fix closes the
    auth gap only, not the guardrail gap, per the scope agreed with the
    caller of this change."""
    try:
        try:
            requested_deployment = resolve_deployment(model)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        extracted_texts = []
        for f in files:
            extracted = await extract_and_log(f, uploaded_by or project_name or "unknown")
            if extracted:
                filename, text = extracted
                extracted_texts.append(f"--- {filename} ---\n{text}")

        source_text = "\n\n".join(extracted_texts)
        project = project_name or "Untitled Project"

        # Pull enterprise knowledge base grounding from Azure AI Search
        knowledge_base_context, grounding_sources = await retrieve_grounding(
            prompt, project
        )

        system_msg = build_system_message(knowledge_base_context)

        outputs = {}
        for fmt in formats:
            instruction = resolve_format_instruction(fmt)
            user_msg = build_user_message(project, prompt, source_text, instruction)
            resp = await create_completion_with_failover(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                deployment=requested_deployment,
            )
            outputs[fmt] = resp.choices[0].message.content.strip()

        return {
            "project": project,
            "outputs": outputs,
            "grounding_sources": grounding_sources,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))