# BA Assist: Users, Roles and Project Documents

**Purpose:** Explain how a user, their role and their client/project assignment decide which documents and which ontology the system uses, how the design was reviewed, and how to add a client or project.
**Audience:** Architects, engineering, security reviewers, and admins who register users.
**Status:** As built on 2026-10-04. The two issues found in review (client ignored in folder names; free-text project ids) are **fixed** (section 5).

---

## 1. The pieces

| Piece | Stored in | Contents |
|---|---|---|
| **Project registry** | [`app/config/projects.json`](app/config/projects.json) | Every client and project the system serves: stable ids (`excellus`, `payment-integrity`) and display names (`Excellus`, `Payment Integrity`). **The single source of truth**: the Register page, access checks, search folders and ontologies all use it. |
| **Login** | Cosmos DB `payeriqdb` / `UserCredential`, one document per user | `username`, `passwordHash` (bcrypt), `role` (`user`, or `admin` / `superuser`), `isActive` |
| **Project assignment** | Same container, `type: "userMapping"`, one per user × project | `UserName`, `ClientID`, `ProjectID` (registry ids), who created it and when |
| **Token** | Browser session storage | JWT signed with `AUTH_JWT_SECRET`, valid 8 hours: `sub` (username), `role`. Holds **no** project list. |
| **Session** | Cosmos DB `PayerIQ` / `Checkpoints` | Each generation's client, project, owner, drafts and files |
| **Documents** | Blob `sharepoint-docs`, indexed by Azure AI Search with each file's `storage_path` | Common files at the root; client and project files in `<client>/` and `<client>/<project>/` |
| **Ontology** | Blob `<client>/<project>/ontology.json`, or repo `ontology/<client>/<project>.json` | The project's target model |

**Roles:**

| Role | How it's created | Can do |
|---|---|---|
| `user` | Admin's **Register user** page | Generate, refine and download only for the projects assigned to them; only their own sessions |
| `admin` / `superuser` | Set up directly in Cosmos DB (the Register page can't create admins) | Register users; use any **registered** project; open any session |

---

## 2. Folder layout in Blob

```
sharepoint-docs/
├── STTM_Data_Ingestion_Template.xlsx, Feature / User Story templates,
│   instruction document, Payer Data Dictionary …     ← common: every client, every project
├── excellus/
│   ├── <optional client-wide files>                  ← every Excellus project
│   ├── payment-integrity/
│   │   ├── ontology.json
│   │   └── <project-only documents>
│   └── medical-claims/
│       └── ontology.json …
├── amerihealth/
│   └── req/ …
└── scan/
    └── qnxt-to-edw-mapping/ …
```

A project's folder is always **`<clientId>/<projectId>`** from the registry. Search for Excellus / Payment Integrity reads only:

| Folder | Read? |
|---|---|
| root | ✅ common files |
| `excellus/` | ✅ Excellus-wide files |
| `excellus/payment-integrity/` | ✅ the project's own files |
| `excellus/medical-claims/` | ❌ another Excellus project |
| `amerihealth/…` (even a project also called "Payment Integrity") | ❌ another client |

---

## 3. Walkthrough: user **test**, client **Excellus**, project **Payment Integrity**

```mermaid
sequenceDiagram
    autonumber
    actor A as Admin
    actor U as User "test"
    participant FE as Frontend
    participant API as Backend
    participant R as Project registry
    participant DB as Cosmos DB (payeriqdb)
    participant S as Azure AI Search
    participant B as Blob / ontology

    A->>FE: open Register user
    FE->>API: GET /v2/admin/projects
    API->>R: registered clients and projects
    API-->>FE: Excellus → Payment Integrity, Medical Claims, …  (dropdowns)
    A->>FE: test · User · Excellus · Payment Integrity · password
    FE->>API: POST /v2/admin/users {ClientID: excellus, ProjectID: payment-integrity}
    API->>R: resolve → registered? yes
    API->>DB: create login {test, bcrypt hash, role user} + mapping {excellus, payment-integrity}

    U->>FE: log in
    FE->>API: POST /v2/auth/login
    API-->>FE: JWT {sub: test, role: user}
    FE->>API: GET /v2/auth/me/projects
    API->>DB: mappings for test
    API->>R: resolve each mapping
    API-->>FE: [{excellus, "Excellus", payment-integrity, "Payment Integrity"}]

    U->>FE: upload file, Generate
    FE->>API: POST /v2/generate (client_id=excellus, project_id=payment-integrity)
    API->>R: resolve → excellus/payment-integrity  (unknown → 400)
    API->>DB: is test mapped to this project? (no → 403)
    API->>S: keep root + excellus/ + excellus/payment-integrity/; search each file by exact path
    API->>B: excellus/payment-integrity/ontology.json
    API-->>FE: STTM for Excellus / Payment Integrity only
```

| Steps | What happens | Code |
|---|---|---|
| 1–3 | The Register page loads its **ClientID / ProjectID dropdowns from the backend registry**, so it can only offer projects the backend accepts. | `GET /v2/admin/projects` |
| 4–7 | The admin saves **test** (role User) for Excellus / Payment Integrity with a password (8+ characters, e.g. `Admin@123`). The backend resolves the project (unknown → 400), then creates the login (bcrypt hash only) and the mapping, **stored with registry ids**. | `auth_service.save_user_mapping` |
| 8–10 | **test** logs in and gets a token holding only username and role. | `auth_service.login` |
| 11–14 | The page asks for test's assignments: ids to send back, names to show ("Excellus / Payment Integrity"). | `/v2/auth/me/projects` |
| 15–17 | On Generate, the backend **resolves** the client/project through the registry (unknown or misspelled → **400** with the name sent), then **re-checks the mapping in Cosmos DB** (not assigned → 403). Revoking an assignment takes effect immediately. | `_resolve_project`, `check_project_access` |
| 18 | Search keeps only root, client and project folders, and queries each file with a filter on its **exact Blob path**. | `documents_for_project`, `retrieve_grounding` |
| 19 | The ontology comes from the same folder. | `ontology_service.get_ontology(folder)` |
| later | Refine, status and download check that the caller owns the session (or is an admin), and re-check project access. | `_check_session_access` |

**What makes this safe:** the folder is computed **on the server** from the registry, after the access check. The browser never sends a folder or a search filter.

---

## 4. What's in the right direction

| Design choice | Why it's right |
|---|---|
| Passwords checked only on the server, stored as bcrypt hashes | No credentials or Cosmos keys in the browser |
| Generic "invalid username or password" | Logins can't be used to discover usernames |
| Token holds identity, not permissions; assignments re-checked per request | Revoking access is immediate |
| Admins can't be created through the API | A compromised admin page can't mint admins |
| Folder and search filter computed server-side after the access check | The browser can't widen its own search |
| Search filters on the exact Blob path | Files with the same name in two folders can't leak into each other |
| No fallback to the whole index or another project's ontology | A missing folder returns nothing rather than someone else's data |
| Sessions owned by their user | Nobody can refine or download another user's STTM by guessing an id |

---

## 5. Issues found in review, and how they were fixed

### Issue 1: the client was ignored in the folder name (high) — **fixed**

| | Before | Now |
|---|---|---|
| Excellus / Payment Integrity | `payment-integrity/` | `excellus/payment-integrity/` |
| AmeriHealth / Payment Integrity | `payment-integrity/` ← **same folder** | `amerihealth/payment-integrity/` (if registered) |

**Risk removed:** two clients with a project of the same name can no longer read each other's documents or ontology. Search now admits only root + the client's folder + the project's folder (`documents_for_project`), and the ontology path includes the client.

### Issue 2: client and project ids were free text (medium) — **fixed**

| Request | Before | Now |
|---|---|---|
| `Excellus` / `Payment Integrity` | folder `payment-integrity` ✅ | `excellus/payment-integrity` ✅ |
| `excellus` / `paymentintegrity` | folder `paymentintegrity`: no documents, **no ontology, no error** | resolves to `excellus/payment-integrity` ✅ |
| `Excellus` / `Paymnet Integrity` (typo) | silently generated without the project's model | **400** "Unknown client/project 'Excellus' / 'Paymnet Integrity'." |
| `AmeriHealth` / `Payment Integrity` (not registered for that client) | shared Excellus's folder | **400**, even for an admin |
| Register a user to an unknown project | accepted | **400** "… Add it to app/config/projects.json first." |

**How:**
- **One registry** ([`app/config/projects.json`](app/config/projects.json), read by [`project_registry.py`](app/services/project_registry.py)). Ids and names are matched ignoring case, spaces, hyphens and underscores. At load, the registry **refuses** two entries that would match alike, so ambiguity can't creep in.
- **Every entry point resolves through it:** registering a user, the access check, `/v2/generate`, the older `/generate`, `/v2/ontology`, search and ontology loading.
- **Mappings store registry ids.** Mappings and sessions saved earlier with display names ("Excellus" / "Payment Integrity") still resolve, so **no data migration was needed**. A duplicate assignment is detected whatever spelling the earlier one used.
- **The Register page reads the registry** (`GET /v2/admin/projects`, admin only). The hard-coded list in the frontend's `clients.js` is gone.
- **No more silent gaps:** at startup the backend lists every registered project with no ontology file, e.g. "Excellus / Correspondence mapping (excellus/correspondence-mapping) has no local ontology file". A session whose project no longer resolves gets **no** search results (event `project_unresolved`), never a search across all clients.

**Verified:**
- 7 registry unit tests; the access tests updated for the new behaviour.
- An API run-through covering every row of the table above.
- A live STTM for `excellus` / `payment-integrity`: ontology loaded from `excellus/payment-integrity`, 23 rows, no violations.

### Smaller points still open

| # | Point | Recommendation |
|---|---|---|
| 3 | A user assigned to several projects only gets the **first** one on the main page | Show a project selector only for users with more than one assignment |
| 4 | Admins have no assignment, so the main page shows "no project assigned" for them | Give admins a project selector (they may use any registered project) |
| 5 | A project without an ontology is reported at startup but not shown to the user | Also show it in the UI ("this project has no ontology yet") |
| 6 | `Role` is stored on both the login and each mapping; only the login's is used | Keep it on the login, or define per-project roles deliberately |
| 7 | `tenantId` is always `default` and unused | Remove, or use the client id |
| 8 | Generated files are stored on the server disk without a client/project path | Store outputs in Blob under `<client>/<project>/outputs/` |
| 9 | The registry is a file in the repo, so a new project needs a deploy | Fine at today's rate; move it to Cosmos DB or Blob if admins should add projects themselves |

---

## 6. How to add a client or project

1. **Register it:** add the client and/or project to [`app/config/projects.json`](app/config/projects.json):
   ```json
   { "clientId": "excellus", "clientName": "Excellus",
     "projects": [ { "projectId": "dental-claims", "projectName": "Dental Claims" } ] }
   ```
   Use a lowercase id with hyphens; it becomes the folder name. Then deploy.
2. **Create its Blob folder** `sharepoint-docs/excellus/dental-claims/` and upload the project-only documents. Common documents stay at the root.
3. **Give it an ontology:** copy `ontology/_template.json`, fill it from the project's documents and a sample vendor file, mark items `proposed`, and upload it as `excellus/dental-claims/ontology.json`. Keep the approved copy in the repo at `ontology/excellus/dental-claims.json`.
4. **Run the search indexer** (or wait for its schedule) so the new files are searchable with their folder path.
5. **Assign users** on the Register page: the new project now appears in the dropdown.

Check with `GET /v2/ontology?client_id=excellus&project_id=dental-claims` that the ontology loads, and from where.

---

## 7. Code map

| File | Role |
|---|---|
| [app/config/projects.json](app/config/projects.json) | The project registry (data) |
| [app/services/project_registry.py](app/services/project_registry.py) | `resolve`, `all_projects`, `clients`, the folder rule `<client>/<project>` |
| [app/services/auth_service.py](app/services/auth_service.py) | Login, bcrypt, token, mappings (`save_user_mapping` validates against the registry), `has_project_access` |
| [app/auth_router.py](app/auth_router.py) | `/v2/auth/login`, `/v2/auth/me/projects` (ids + names), `require_auth`, `check_project_access` |
| [app/admin_router.py](app/admin_router.py) | `GET /v2/admin/projects`, `POST /v2/admin/users` (admin only) |
| [app/routergenerator.py](app/routergenerator.py) | `_resolve_project` (400 for unknown), access check on generate, session ownership |
| [app/graph.py](app/graph.py) | `_project_folder` for retrieval and ontology; no grounding for an unresolvable project |
| [app/services/azure_search_service.py](app/services/azure_search_service.py) | `documents_for_project` (root + client + project), path-filtered retrieval |
| [app/services/ontology_service.py](app/services/ontology_service.py) | `get_ontology(folder)` |
| `P_Frontend_ReactJs/src/pages/AdminRegisterPage.jsx` | Register page, dropdowns from the registry |
| [tests/test_project_registry.py](tests/test_project_registry.py), [tests/test_project_access.py](tests/test_project_access.py) | Registry and access tests |
