import os
import shutil
import pytest
from fastapi.testclient import TestClient
from main import app, REPOS_DIR

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_and_teardown():
    # Setup
    os.makedirs(REPOS_DIR, exist_ok=True)
    yield
    # Teardown
    if os.path.exists(REPOS_DIR):
        shutil.rmtree(REPOS_DIR)

def test_create_repo():
    response = client.post("/repo/create")
    assert response.status_code == 200
    data = response.json()
    assert "repo_id" in data
    assert data["message"] == "Repository created successfully"
    assert os.path.exists(os.path.join(REPOS_DIR, data["repo_id"]))

def test_write_and_read_file():
    # Utworzenie repo
    response = client.post("/repo/create")
    repo_id = response.json()["repo_id"]

    # Zapis pliku
    file_content = "print('Hello Blihy Test')"
    write_response = client.post(
        f"/repo/{repo_id}/file/test_file.py",
        json={"content": file_content}
    )
    assert write_response.status_code == 200
    assert write_response.json()["message"] == "File test_file.py written successfully"

    # Odczyt pliku
    read_response = client.get(f"/repo/{repo_id}/file/test_file.py")
    assert read_response.status_code == 200
    data = read_response.json()
    assert data["path"] == "test_file.py"
    assert data["content"] == file_content

def test_list_files():
    # Utworzenie repo
    response = client.post("/repo/create")
    repo_id = response.json()["repo_id"]

    # Zapis kilku plików
    client.post(f"/repo/{repo_id}/file/file1.txt", json={"content": "1"})
    client.post(f"/repo/{repo_id}/file/subdir/file2.txt", json={"content": "2"})

    # Listowanie plików
    list_response = client.get(f"/repo/{repo_id}/files")
    assert list_response.status_code == 200
    files = list_response.json()["files"]

    # Kolejność może być różna, więc sortujemy do porównania (albo sprawdzamy zawieranie)
    assert len(files) == 2
    assert "file1.txt" in files
    # Ścieżki na Windows i Linux mogą się różnić w zależności od os.path.join,
    # w naszym kodzie użyliśmy relpath, więc powinno być 'subdir/file2.txt' lub 'subdir\\file2.txt'
    assert any("file2.txt" in f for f in files)

def test_access_denied_path_traversal():
    # Utworzenie repo
    response = client.post("/repo/create")
    repo_id = response.json()["repo_id"]

    # Próba zapisu pliku poza repo (URL encoding for '..')
    write_response = client.post(
        f"/repo/{repo_id}/file/..%2Foutside.txt",
        json={"content": "hack"}
    )
    assert write_response.status_code == 403

def test_get_nonexistent_file():
    # Utworzenie repo
    response = client.post("/repo/create")
    repo_id = response.json()["repo_id"]

    # Próba odczytu nieistniejącego pliku
    read_response = client.get(f"/repo/{repo_id}/file/nonexistent.txt")
    assert read_response.status_code == 404
