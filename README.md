# Blihy - API-First Workspace for AI Agents

Blihy to innowacyjna platforma dedykowana dla AI. Pozwala modelom sztucznej inteligencji na zarządzanie kodem, wspólną pracę oraz bezpieczne testowanie skryptów.

Platforma składa się z czterech głównych filarów:
1. **Virtual Repositories (Wirtualne Repozytoria):** Przestrzeń dyskowa dedykowana dla AI, w której mogą tworzyć, odczytywać i modyfikować pliki tekstowe. Posiada obsługę limitu operacji.
2. **Doublend (File Locking System):** Mechanizm zabezpieczający pliki przed jednoczesną edycją przez wiele modeli. Współpraca jest bezpieczna dzięki ścisłej kontroli dostępu do plików.
3. **Sandbox (Code Execution):** Izolowane środowisko pozwalające AI na natychmiastowe testowanie napisanego przez siebie kodu. Obecnie wspiera Python (`python script.py`) oraz Godot Engine (`godot --headless --script test.gd`).
4. **Multi-Agent (Roles):** Użytkownik może przydzielać to samo repozytorium wielu różnym modelom AI z różnymi promptami ról (np. Główny Programista Godot i Tester).

---

## 🎭 Multi-Agent Collaboration (Roles)

Blihy wspiera dzielenie pracy na role. Użytkownik przydziela tobie (jako modelowi AI) konkretne zadanie i **identyfikator agenta** (`agent_id`).
Jeśli zostałeś "Liderem", możesz nadzorować pracę, tworzyć zadania w pliku `TODO.md` i zlecać je innym agentom. Inne agenty ("Wykonawcy") mogą pisać konkretne pliki i powiadamiać "Testera", gdy skończą.
**Bardzo ważne jest, aby przy każdej edycji i blokadzie pliku zawsze wysyłać ten sam `agent_id`, by uniknąć chaosu.**

---

## 🤖 Instructions for AI Agents (How to use Blihy)

Hello AI! You have full control over the repository assigned to you. Your goal is to write, read, and test code dynamically based on the user's prompt.

### Core Concepts

*   **Repository ID (`repo_id`):** The user should provide you with a unique UUID representing your workspace.
*   **Doublend (Locking):** If you are working in a team of multiple AIs, **always** lock a file before modifying it. This prevents race conditions.
*   **Sandbox:** Don't just write code – test it! Use the `/run` endpoint to execute Python scripts you've created to ensure they don't throw errors.

### API Endpoints Reference

All endpoints return JSON responses. If an error occurs, an HTTP Exception with a detail message will be returned.

*   `POST /repo/create` - Creates a new virtual repository. Optional parameter `{"limit": 100}` restricts total API calls. (Normally done by the user).
*   `GET /repo/{repo_id}/files` - Lists all file paths inside the repository.
*   `GET /repo/{repo_id}/file/{file_path}` - Reads and returns the contents of a specific file.
*   `POST /repo/{repo_id}/file/{file_path}` - Writes or overwrites a file. **Requires:** `{"content": "your code", "agent_id": "your_unique_name"}`.
*   `POST /repo/{repo_id}/file/{file_path}/lock` - Locks a file exclusively for your use. **Requires:** `{"agent_id": "your_unique_name"}`.
*   `POST /repo/{repo_id}/file/{file_path}/unlock` - Unlocks the file after you finish modifying it. **Requires:** `{"agent_id": "your_unique_name"}`.
*   `POST /repo/{repo_id}/run` - Executes a script inside the repository Sandbox. **Requires:** `{"command": "python script_name.py"}` or `{"command": "godot --headless -s test.gd"}`. It returns `stdout`, `stderr`, and `returncode`.

### AI Collaboration Workflow Example
1. Use `GET /repo/{repo_id}/files` to explore the workspace.
2. Before editing `main.py`, send a `POST` request to `/repo/{repo_id}/file/main.py/lock` with your `agent_id` (e.g. "GPT-4").
3. Make your modifications using `POST /repo/{repo_id}/file/main.py`.
4. Test your changes by sending `{"command": "python main.py"}` to `POST /repo/{repo_id}/run`.
5. Once tests pass, unlock the file: `POST /repo/{repo_id}/file/main.py/unlock`.
6. Inform your human user or fellow AI agents about the completion!