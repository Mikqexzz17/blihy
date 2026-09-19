import os
import uuid
import shutil
import subprocess
import tempfile
import asyncio
import json
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
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

    cmd_parts = run_req.command.split()
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
