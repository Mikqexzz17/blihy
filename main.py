import os
import uuid
import shutil
import subprocess
from fastapi import FastAPI, HTTPException
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
    description=description,
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

class RepoCreateResponse(BaseModel):
    repo_id: str
    message: str

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

@app.get("/", include_in_schema=False)
def serve_home():
    """Zwraca stronę główną (Landing Page)."""
    return FastAPIFileResponse("static/index.html")

@app.post("/repo/create", response_model=RepoCreateResponse)
def create_repo():
    """Tworzy nowe wirtualne repozytorium dla agentów AI."""
    repo_id = str(uuid.uuid4())
    repo_path = os.path.join(REPOS_DIR, repo_id)

    try:
        os.makedirs(repo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create repository: {str(e)}")

    return RepoCreateResponse(repo_id=repo_id, message="Repository created successfully")

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
def run_command(repo_id: uuid.UUID, run_req: RunRequest):
    """Uruchamia skrypt python w danym repozytorium (bezpieczny Sandbox)."""
    repo_path = os.path.abspath(os.path.join(REPOS_DIR, str(repo_id)))

    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Repository not found")

    cmd_parts = run_req.command.split()
    if not cmd_parts or cmd_parts[0] not in ["python", "python3"]:
         raise HTTPException(status_code=403, detail="Only 'python' commands are allowed for safety.")

    if len(cmd_parts) > 1:
        target_file = cmd_parts[1]
        abs_target = os.path.abspath(os.path.join(repo_path, target_file))
        if os.path.commonpath([repo_path]) != os.path.commonpath([repo_path, abs_target]):
             raise HTTPException(status_code=403, detail="Access denied. Cannot run files outside repo.")

    try:
        # Uruchomienie bez shell=True i z izolacją
        result = subprocess.run(
            cmd_parts,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10
        )
        return RunResponse(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=408, detail="Command execution timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Command execution error: {str(e)}")

@app.post("/repo/{repo_id}/file/{file_path:path}/lock")
def lock_file(repo_id: uuid.UUID, file_path: str, req: LockRequest):
    """Zamyka plik do edycji tylko dla podanego agent_id."""
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
    return {"message": f"File {file_path} locked successfully by agent {req.agent_id}"}

@app.post("/repo/{repo_id}/file/{file_path:path}/unlock")
def unlock_file(repo_id: uuid.UUID, file_path: str, req: LockRequest):
    """Odblokowuje plik z edycji."""
    r_id = str(repo_id)
    if r_id in locks and file_path in locks[r_id]:
        if locks[r_id][file_path] == req.agent_id:
            del locks[r_id][file_path]
            return {"message": f"File {file_path} unlocked successfully"}
        else:
            raise HTTPException(status_code=403, detail="You cannot unlock a file locked by another agent")

    return {"message": "File is not locked"}

# Zmieniona kolejność - endpoint z generycznym parametrem {file_path:path}
# musi znajdować się na końcu, aby nie porywał żądań do /lock i /unlock.
@app.post("/repo/{repo_id}/file/{file_path:path}")
def write_file(repo_id: uuid.UUID, file_path: str, file_data: FileRequest):
    """Tworzy lub nadpisuje plik w repozytorium."""
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
        return {"message": f"File {file_path} written successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error writing file: {str(e)}")
