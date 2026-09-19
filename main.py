import os
import uuid
import shutil
import subprocess
import shlex
import tempfile
import asyncio
import json
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import litellm
from fastapi.responses import FileResponse as FastAPIFileResponse
from pydantic import BaseModel
from typing import List, Optional

description = """
**Blihy API** is the backend engine for an API-first workspace designed explicitly for AI agents.

## Core Features
* **Virtual Repositories:** Sandboxed folders where agents can read and write code.
* **Doublend (File Locking):** A cooperative mechanism for multi-agent workflows. Use `/lock` to claim a file before modifying it.
* **Sandbox (Execution):** Use the `/run` endpoint to execute Python scripts inside your virtual repo and verify they work.

**AI Agents:** Please read the instructions on the homepage (GET `/`) to learn how to operate inside your assigned `repo_id`.
"""

app = FastAPI(
    title="Blihy API",
    description=description.replace("GitHub", "Platforma dla agentów AI"),
    version="1.0.0",
    contact={
        "name": "Blihy System",
        "url": "http://localhost:8000/"
    }
)

app.mount("/static", StaticFiles(directory="static"), name="static")

REPOS_DIR = "repos"

# Ensure the root repos directory exists
os.makedirs(REPOS_DIR, exist_ok=True)

class RepoCreateRequest(BaseModel):
    limit: Optional[int] = None

class RepoCreateResponse(BaseModel):
    repo_id: str
    message: str
    limit: Optional[int] = None

class AgentConfig(BaseModel):
    agent_id: str
    role_description: str
    api_key: str
    model: str
    is_leader: bool = False

class RepoAgentsRequest(BaseModel):
    agents: List[AgentConfig]

class ChatRequest(BaseModel):
    message: str
    target_agent_id: str

class FileRequest(BaseModel):
    content: str
    agent_id: Optional[str] = None

class FileResponse(BaseModel):
    path: str
    content: str

class FileListResponse(BaseModel):
    files: List[str]

class RunRequest(BaseModel):
    command: str

class RunResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int

class LockRequest(BaseModel):
    agent_id: str

# Słownik do przechowywania blokad w pamięci (dla wersji produkcyjnej lepszy będzie Redis/DB)
# Struktura: locks[repo_id][file_path] = agent_id
locks = {}

# Słowniki limitów użycia API
repo_limits = {}
repo_usage = {}

# Słownik przechowujący konfigurację agentów w danym repo
# repo_agents[repo_id] = [AgentConfig, ...]
repo_agents = {}

def check_limit(repo_id: str):
    if repo_id in repo_limits and repo_limits[repo_id] is not None:
        if repo_usage.get(repo_id, 0) >= repo_limits[repo_id]:
            raise HTTPException(status_code=429, detail="API Usage Limit Exceeded for this repository.")
        repo_usage[repo_id] = repo_usage.get(repo_id, 0) + 1

# Menedżer połączeń WebSocket
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, repo_id: str):
        await websocket.accept()
        if repo_id not in self.active_connections:
            self.active_connections[repo_id] = []
        self.active_connections[repo_id].append(websocket)

    def disconnect(self, websocket: WebSocket, repo_id: str):
        if repo_id in self.active_connections:
            self.active_connections[repo_id].remove(websocket)
            if not self.active_connections[repo_id]:
                del self.active_connections[repo_id]

    async def broadcast(self, repo_id: str, message: dict):
        if repo_id in self.active_connections:
            for connection in self.active_connections[repo_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    pass

manager = ConnectionManager()

@app.websocket("/repo/{repo_id}/ws")
async def websocket_endpoint(websocket: WebSocket, repo_id: str):
    """Endpoint nasłuchiwania w czasie rzeczywistym dla konsoli aktywności."""
    await manager.connect(websocket, repo_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, repo_id)

@app.get("/", include_in_schema=False)
def serve_home():
    """Zwraca stronę główną (Landing Page)."""
    return FastAPIFileResponse("static/index.html")

async def process_agent_message(repo_id: str, repo_path: str, agent: AgentConfig, user_message: str, other_agents: List[AgentConfig]):
    """Funkcja w tle wykonująca zapytania do LLM."""
    try:
        # Wspólna wiedza o Godocie
        godot_knowledge = (
            "GODOT ENGINE MASTER KNOWLEDGE:\\n"
            "1. You have full access to create and modify a Godot 4 project.\\n"
            "2. Always ensure a `project.godot` file is created at the root if it doesn't exist.\\n"
            "3. You understand the difference between `.gd` (GDScript), `.tscn` (PackedScene), and `.tres` (Resources).\\n"
            "4. When creating `.tscn` files, write the raw text format correctly (e.g., `[gd_scene load_steps=...]`, `[node name=...]`).\\n"
            "5. Connect signals correctly inside `.tscn` files or via code (`_ready()` -> `connect()`).\\n"
            "6. Structure the project logically (e.g., `res://scenes/`, `res://scripts/`).\\n"
            "7. The sandbox runs `godot --headless`. Therefore, any tests should ideally automatically quit (`get_tree().quit()`) after successful validation, or run indefinitely for manual UI testing if requested.\\n"
        )

        if agent.is_leader:
            # Prompt dla lidera
            sub_agents_info = "\\n".join([f"- {a.agent_id} (Model: {a.model}): {a.role_description}" for a in other_agents])
            system_prompt = (
                f"You are the LEADER AI for a Godot 4 project. Your role: {agent.role_description}.\\n"
                f"{godot_knowledge}\\n"
                f"You have the following team members to delegate work to:\\n{sub_agents_info}\\n"
                "Your job is to architect the game, figure out what files are needed, break down the user's request into actionable tasks, and delegate them to your team members based on their roles.\\n"
                "You MUST respond STRICTLY in valid JSON format, with no other text, containing tasks for your sub-agents:\\n"
                '{"tasks": [{"agent_id": "Agent-Name", "task_description": "Specific instruction on what .gd or .tscn file to create and how it should work"}]}'
            )
        else:
            # Prompt dla wykonawcy
            system_prompt = (
                f"You are a WORKER AI for a Godot 4 project. Your role: {agent.role_description}.\\n"
                f"{godot_knowledge}\\n"
                "You must write the requested Godot code, scenes, or configurations based on the instructions.\\n"
                "To create or modify a file, you MUST use the following format exactly in your response:\\n"
                "[WRITE:filename.ext]\\n"
                "your content here\\n"
                "[/WRITE]\\n"
                "You can write multiple files by repeating the [WRITE] block.\\n"
                "If the instruction is to create a gun and jumping mechanics, actually write the character controller in `.gd` and the node structure in `.tscn`."
            )

        # Wywołanie modelu LLM
        response = await litellm.acompletion(
            model=agent.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            api_key=agent.api_key
        )

        llm_reply = response.choices[0].message.content

        # Broadcast odpowiedzi LLM
        await manager.broadcast(repo_id, {
            "type": "chat",
            "sender": agent.agent_id,
            "target": "User" if not agent.is_leader else "Team",
            "message": llm_reply
        })

        if agent.is_leader:
            # Parsowanie JSON i delegowanie zadań
            try:
                # Oczyszczenie z markdowna (jesli model doda ```json)
                json_str = llm_reply.replace("```json", "").replace("```", "").strip()
                tasks_data = json.loads(json_str)
                for task in tasks_data.get("tasks", []):
                    target_id = task.get("agent_id")
                    target_task = task.get("task_description")
                    target_agent = next((a for a in other_agents if a.agent_id == target_id), None)
                    if target_agent:
                        await manager.broadcast(repo_id, {
                            "type": "chat",
                            "sender": agent.agent_id,
                            "target": target_id,
                            "message": f"Delegated task: {target_task}"
                        })
                        # Rekurencyjne wywołanie podwykonawcy w tle
                        asyncio.create_task(process_agent_message(repo_id, repo_path, target_agent, target_task, []))
            except Exception as e:
                await manager.broadcast(repo_id, {
                    "type": "error",
                    "message": f"Leader JSON parsing error: {str(e)}"
                })
        else:
            # Parsowanie znaczników [WRITE:file]
            pattern = r"\[WRITE:(.+?)\](.*?)\[/WRITE\]"
            matches = re.finditer(pattern, llm_reply, re.DOTALL)
            for match in matches:
                file_name = match.group(1).strip()
                file_content = match.group(2).strip()

                # Bezpieczny zapis
                full_path = os.path.join(repo_path, file_name)
                abs_repo_path = os.path.abspath(repo_path)
                abs_full_path = os.path.abspath(full_path)

                if os.path.commonpath([abs_repo_path]) == os.path.commonpath([abs_repo_path, abs_full_path]):
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(file_content)

                    await manager.broadcast(repo_id, {
                        "type": "write",
                        "message": f"Agent {agent.agent_id} wrote to {file_name}"
                    })

    except Exception as e:
        await manager.broadcast(repo_id, {
            "type": "error",
            "message": f"Agent {agent.agent_id} error: {str(e)}"
        })

def cleanup_temp_file(path: str):
    """Zadanie w tle usuwające plik tymczasowy po wysłaniu."""
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass

@app.post("/repo/create", response_model=RepoCreateResponse)
def create_repo(req: Optional[RepoCreateRequest] = None):
    """Tworzy nowe wirtualne repozytorium dla agentów AI."""
    repo_id = str(uuid.uuid4())
    repo_path = os.path.join(REPOS_DIR, repo_id)

    try:
        os.makedirs(repo_path)
        if req and req.limit is not None:
            repo_limits[repo_id] = req.limit
            repo_usage[repo_id] = 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create repository: {str(e)}")

    limit_val = repo_limits.get(repo_id)
    return RepoCreateResponse(repo_id=repo_id, message="Repository created successfully", limit=limit_val)

@app.post("/repo/{repo_id}/agents")
def configure_agents(repo_id: uuid.UUID, req: RepoAgentsRequest):
    """Zapisuje konfigurację agentów (Role, API Keys) dla danego projektu."""
    r_id = str(repo_id)
    repo_path = os.path.join(REPOS_DIR, r_id)
    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    repo_agents[r_id] = req.agents
    return {"message": "Agents configured successfully"}

@app.get("/repo/{repo_id}/agents")
def get_agents(repo_id: uuid.UUID):
    r_id = str(repo_id)
    if r_id not in repo_agents:
        return {"agents": []}

    # Nie zwracamy kluczy API na zewnątrz w GET
    safe_agents = []
    for a in repo_agents[r_id]:
        safe_agents.append({
            "agent_id": a.agent_id,
            "role_description": a.role_description,
            "model": a.model,
            "is_leader": a.is_leader
        })
    return {"agents": safe_agents}

@app.post("/repo/{repo_id}/chat")
async def chat_with_agent(repo_id: uuid.UUID, req: ChatRequest, background_tasks: BackgroundTasks):
    """Główny endpoint czatu z danym agentem."""
    check_limit(str(repo_id))
    r_id = str(repo_id)
    repo_path = os.path.join(REPOS_DIR, r_id)

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    if r_id not in repo_agents:
        raise HTTPException(status_code=400, detail="Agents not configured for this repo")

    # Znajdź agenta
    target_agent = next((a for a in repo_agents[r_id] if a.agent_id == req.target_agent_id), None)
    if not target_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Ustal listę innych agentów do przekazania liderowi
    other_agents = [a for a in repo_agents[r_id] if a.agent_id != target_agent.agent_id]

    # Broadcast wiadomości od użytkownika
    await manager.broadcast(r_id, {
        "type": "chat",
        "sender": "User",
        "target": target_agent.agent_id,
        "message": req.message
    })

    # Task w tle - wołanie LLM
    background_tasks.add_task(process_agent_message, r_id, repo_path, target_agent, req.message, other_agents)

    return {"message": "Message sent to agent."}

@app.post("/repo/{repo_id}/clone", response_model=RepoCreateResponse)
def clone_repo(repo_id: uuid.UUID):
    """Klonuje istniejące repozytorium do nowego UUID, np. jako zapis zakończonego projektu."""
    r_id = str(repo_id)
    source_path = os.path.join(REPOS_DIR, r_id)

    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Source repository not found")

    new_repo_id = str(uuid.uuid4())
    new_repo_path = os.path.join(REPOS_DIR, new_repo_id)

    try:
        shutil.copytree(source_path, new_repo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")

    return RepoCreateResponse(repo_id=new_repo_id, message="Repository cloned successfully. Ready for next phase.")

@app.get("/repo/{repo_id}/download")
def download_repo(repo_id: uuid.UUID, background_tasks: BackgroundTasks):
    """Kompresuje całe repozytorium do pliku ZIP i zwraca do pobrania."""
    repo_path = os.path.join(REPOS_DIR, str(repo_id))

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    # Utworzenie tymczasowego pliku zip
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_file.close()

    # shutil.make_archive dołącza rozszerzenie automatycznie, więc ucinamy '.zip'
    base_name = temp_file.name[:-4]

    try:
        shutil.make_archive(base_name, 'zip', repo_path)
    except Exception as e:
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP: {str(e)}")

    # Zlecenie usunięcia pliku tymczasowego po jego zwróceniu
    background_tasks.add_task(cleanup_temp_file, temp_file.name)

    return FastAPIFileResponse(
        path=temp_file.name,
        media_type="application/zip",
        filename=f"blihy_repo_{repo_id}.zip"
    )

@app.get("/repo/{repo_id}/files", response_model=FileListResponse)
def list_files(repo_id: uuid.UUID):
    """Zwraca listę plików w danym repozytorium."""
    repo_path = os.path.join(REPOS_DIR, str(repo_id))
    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    all_files = []
    for root, _, files in os.walk(repo_path):
        for file in files:
            # Upewniamy się, że ścieżka jest względna w stosunku do repo_path
            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, repo_path).replace(os.sep, '/')
            all_files.append(relative_path)

    return FileListResponse(files=all_files)

@app.get("/repo/{repo_id}/file/{file_path:path}", response_model=FileResponse)
def get_file(repo_id: uuid.UUID, file_path: str):
    """Pobiera zawartość konkretnego pliku w repozytorium."""
    repo_path = os.path.join(REPOS_DIR, str(repo_id))
    full_path = os.path.join(repo_path, file_path)

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    # Zabezpieczenie przed path traversal
    abs_repo_path = os.path.abspath(repo_path)
    abs_full_path = os.path.abspath(full_path)
    if os.path.commonpath([abs_repo_path]) != os.path.commonpath([abs_repo_path, abs_full_path]):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return FileResponse(path=file_path, content=content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")

@app.post("/repo/{repo_id}/run", response_model=RunResponse)
async def run_command(repo_id: uuid.UUID, run_req: RunRequest):
    """Uruchamia skrypt python w danym repozytorium (bezpieczny Sandbox)."""
    check_limit(str(repo_id))
    repo_path = os.path.abspath(os.path.join(REPOS_DIR, str(repo_id)))

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    cmd_parts = shlex.split(run_req.command)
    if not cmd_parts or cmd_parts[0] not in ["python", "python3", "godot"]:
         raise HTTPException(status_code=403, detail="Only 'python' or 'godot' commands are allowed for safety.")

    # Weryfikacja docelowego pliku (ochrona przed path traversal)
    if cmd_parts[0] in ["python", "python3"] and len(cmd_parts) > 1:
        target_file = cmd_parts[1]
        if not target_file.startswith("-"): # Ignorujemy flagi CLI
            abs_target = os.path.abspath(os.path.join(repo_path, target_file))
            if os.path.commonpath([repo_path]) != os.path.commonpath([repo_path, abs_target]):
                 raise HTTPException(status_code=403, detail="Access denied. Cannot run files outside repo.")

    try:
        # Konfiguracja Dockera zależna od silnika
        if cmd_parts[0] == "godot":
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{repo_path}:/app",
                "-w", "/app",
                "barichello/godot-ci:4.3"
            ] + cmd_parts
        else:
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{repo_path}:/app",
                "-w", "/app",
                "python:3.10-slim"
            ] + cmd_parts

        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=15 # Trochę dłuższy timeout na wypadek pobierania obrazu
        )

        await manager.broadcast(str(repo_id), {
            "type": "run",
            "message": f"Executed command in Docker: {run_req.command}",
            "status": "success" if result.returncode == 0 else "error"
        })
        return RunResponse(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=408, detail="Command execution timed out (limit 15s)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Command execution error: {str(e)}")

@app.post("/repo/{repo_id}/file/{file_path:path}/lock")
async def lock_file(repo_id: uuid.UUID, file_path: str, req: LockRequest):
    """Zamyka plik do edycji tylko dla podanego agent_id."""
    check_limit(str(repo_id))
    r_id = str(repo_id)
    repo_path = os.path.join(REPOS_DIR, r_id)
    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    if r_id not in locks:
        locks[r_id] = {}

    if file_path in locks[r_id]:
        if locks[r_id][file_path] == req.agent_id:
            return {"message": "File is already locked by you"}
        else:
            raise HTTPException(status_code=409, detail=f"File is locked by another agent: {locks[r_id][file_path]}")

    locks[r_id][file_path] = req.agent_id
    await manager.broadcast(r_id, {
        "type": "lock",
        "message": f"Agent {req.agent_id} locked file {file_path}"
    })
    return {"message": f"File {file_path} locked successfully by agent {req.agent_id}"}

@app.post("/repo/{repo_id}/file/{file_path:path}/unlock")
async def unlock_file(repo_id: uuid.UUID, file_path: str, req: LockRequest):
    """Odblokowuje plik z edycji."""
    check_limit(str(repo_id))
    r_id = str(repo_id)
    if r_id in locks and file_path in locks[r_id]:
        if locks[r_id][file_path] == req.agent_id:
            del locks[r_id][file_path]
            await manager.broadcast(r_id, {
                "type": "unlock",
                "message": f"Agent {req.agent_id} unlocked file {file_path}"
            })
            return {"message": f"File {file_path} unlocked successfully"}
        else:
            raise HTTPException(status_code=403, detail="You cannot unlock a file locked by another agent")

    return {"message": "File is not locked"}

# Zmieniona kolejność - endpoint z generycznym parametrem {file_path:path}
# musi znajdować się na końcu, aby nie porywał żądań do /lock i /unlock.
@app.post("/repo/{repo_id}/file/{file_path:path}")
async def write_file(repo_id: uuid.UUID, file_path: str, file_data: FileRequest):
    """Tworzy lub nadpisuje plik w repozytorium."""
    check_limit(str(repo_id))
    repo_path = os.path.join(REPOS_DIR, str(repo_id))
    full_path = os.path.join(repo_path, file_path)

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    # Zabezpieczenie przed path traversal
    abs_repo_path = os.path.abspath(repo_path)
    abs_full_path = os.path.abspath(full_path)
    if os.path.commonpath([abs_repo_path]) != os.path.commonpath([abs_repo_path, abs_full_path]):
        raise HTTPException(status_code=403, detail="Access denied")

    # Sprawdzanie systemu blokad ("Doublend" locking)
    repo_locks = locks.get(str(repo_id), {})
    if file_path in repo_locks:
        lock_owner = repo_locks[file_path]
        if file_data.agent_id != lock_owner:
            raise HTTPException(status_code=409, detail=f"File {file_path} is locked by agent {lock_owner}")

    # Tworzenie katalogów, jeśli nie istnieją
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(file_data.content)

        agent_display = file_data.agent_id if file_data.agent_id else "Unknown Agent"
        await manager.broadcast(str(repo_id), {
            "type": "write",
            "message": f"Agent {agent_display} wrote to file {file_path}"
        })
        return {"message": f"File {file_path} written successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error writing file: {str(e)}")
