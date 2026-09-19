import os
import uuid
import shutil
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="Blihy API", description="API-first GitHub for AI agents with Doublend support")

REPOS_DIR = "repos"

# Ensure the root repos directory exists
os.makedirs(REPOS_DIR, exist_ok=True)

class RepoCreateResponse(BaseModel):
    repo_id: str
    message: str

class FileRequest(BaseModel):
    content: str

class FileResponse(BaseModel):
    path: str
    content: str

class FileListResponse(BaseModel):
    files: List[str]

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

    # Tworzenie katalogów, jeśli nie istnieją
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(file_data.content)
        return {"message": f"File {file_path} written successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error writing file: {str(e)}")
