# BA Assist: Users, Roles and Project Documents

**Purpose:** Explain how a user, their role and their client/project assignment decide which documents and which ontology the system uses, and review whether the design is sound.
**Audience:** Architects, engineering, security reviewers, and admins who register users.
**Status:** Review of the code as built on 2026-10-04. Two design issues found (section 4); fixes proposed, not yet made.

---

## 1. The pieces

| Piece | Stored in | Contents |
|---|---|---|
| **Login** | Cosmos DB `payeriqdb` / `UserCredential`, one document per user | `username`, `passwordHash` (bcrypt), `role` (`user`, or `admin` / `superuser`), `isActive` |
| **Project assignment** | Same container, `type: "userMapping"`, one document per user × client × project | `UserName`, `ClientID`, `ProjectID`, `Role`, who created it and when |
| **Token** | Browser session storage | JWT signed with `AUTH_JWT_SECRET`, valid 8 hours: `sub` (username), `role` |
| **Session** | Cosmos DB `PayerIQ` / `Checkpoints` | Each generation's `client_id`, `project_id`, `owner`, drafts and files |
| **Documents** | Blob `sharepoint-docs`, indexed by Azure AI Search with each file's `storage_path` | Common files at the root; project files in a project folder |
| **Ontology** | Blob `<project-folder>/ontology.json`, or repo `ontology/<project-folder>.json` | The project's target model |

**Roles:**

| Role | How it's created | Can do |
|---|---|---|
| `user` | Admin's **Register user** page | Generate, refine and download only for the client/project pairs assigned to them; only their own sessions |
| `admin` / `superuser` | Set up directly in Cosmos DB (the Register page can't create admins) | Register users; use any project; open any session |

---

## 2. Walkthrough: user **test**, client **Excellus**, project **Payment Integrity**

```mermaid
sequenceDiagram
    autonumber
    actor A as Admin
    actor U as User "test"
    participant FE as Frontend
    participant API as Backend
    participant DB as Cosmos DB (payeriqdb)
    participant S as Azure AI Search
    participant B as Blob / ontology

    A->>FE: Register user: test · User · Excellus · Payment Integrity · password
    FE->>API: POST /v2/admin/users (admin token)
    API->>DB: create login {username: test, passwordHash: bcrypt, role: user}
    API->>DB: create mapping {UserName: test, ClientID: Excellus, ProjectID: Payment Integrity}

    U->>FE: log in (test / password)
    FE->>API: POST /v2/auth/login
    API->>DB: find login, check bcrypt hash
    API-->>FE: JWT {sub: test, role: user}
    FE->>API: GET /v2/auth/me/projects
    API->>DB: mappings for "test"
    API-->>FE: [Excellus / Payment Integrity]  (shown next to the user name)

    U->>FE: upload file, Generate
    FE->>API: POST /v2/generate (client_id=Excellus, project_id=Payment Integrity)
    API->>DB: is "test" mapped to Excellus / Payment Integrity?  → yes
    Note over API: session saved with client, project, owner = test
    API->>API: folder = "payment-integrity"
    API->>S: list documents; keep root + payment-integrity/ only
    API->>S: per document: hybrid search, filter storage_path eq '<that file>'
    API->>B: payment-integrity/ontology.json (or repo copy)
    API-->>FE: STTM for Payment Integrity only
```

| Step | What happens | Code |
|---|---|---|
| 1–4 | The admin registers **test** with role **User** for **Excellus / Payment Integrity** and sets a password (8+ characters, e.g. `Admin@123`). Two documents are created: the login (password stored only as a bcrypt hash) and the mapping. | `admin_router.py`, `auth_service.save_user_mapping` |
| 5–8 | **test** logs in. The backend checks the bcrypt hash and returns a signed token holding the username and role, **not** the projects. | `auth_service.login` |
| 9–11 | The page asks for test's assignments and shows "Excellus / Payment Integrity" next to the user name. There's no project picker. | `/v2/auth/me/projects` |
| 12–14 | On Generate, the page sends the client and project. The backend **re-checks the mapping in Cosmos DB on every call**, so removing an assignment takes effect immediately. A user not assigned gets 403. | `check_project_access` |
| 15 | The session records client, project and owner. Refine, status and download later check that the caller is the owner (or an admin); anyone else gets 404. | `_check_session_access` |
| 16–18 | The project becomes a folder name, **"Payment Integrity" → `payment-integrity`**. Search lists every indexed file, keeps those at the container root plus those in `payment-integrity/`, and queries each with a filter on its exact Blob path. The ontology is loaded from the same folder name. | `project_folder`, `documents_for_project`, `retrieve_grounding`, `get_ontology` |

**What makes this safe:** the folder is derived **on the server** from the session's project, *after* the access check. The browser never sends a folder or a search filter, so a user can't ask for another project's documents.

---

## 3. What's in the right direction

| Design choice | Why it's right |
|---|---|
| Passwords checked only on the server, stored as bcrypt hashes | No credentials or Cosmos keys in the browser |
| Generic "invalid username or password" | Logins can't be used to discover usernames |
| Token holds identity, not permissions; assignments re-checked per request | Revoking access is immediate, without waiting 8 hours for the token to expire |
| Admins can't be created through the API | A compromised admin page can't mint new admins |
| Folder and search filter computed server-side after the access check | The client can't widen its own search |
| Search filters on the exact Blob path, not the file name | Two projects' files with the same name can't leak into each other |
| No fallback to the whole index, or to another project's ontology | A missing project folder returns nothing rather than someone else's data |
| Sessions owned by the user who started them | One user can't refine or download another's STTM by guessing a session id |

---

## 4. Issues found

### Issue 1: the client is ignored when choosing the folder (high)

The folder comes from **ProjectID only**:

| Client | Project | Folder used today |
|---|---|---|
| Excellus | Payment Integrity | `payment-integrity` |
| AmeriHealth | Payment Integrity | `payment-integrity`  ← **same folder** |

If two clients ever have a project with the same name, they read **each other's project documents and ontology**. For a payer platform serving several clients, that's a data-segregation problem. (Access checks are fine: a user still needs a mapping. The leak is in *which files* the mapped project resolves to.)

**Fix:** make the folder **client + project**:
```
sharepoint-docs/
├── <common files>                         ← every client, every project
├── excellus/
│   ├── <client-wide files>                ← every Excellus project (optional)
│   ├── payment-integrity/ontology.json …
│   └── medical-claims/ontology.json …
└── amerihealth/
    └── req/ontology.json …
```
Search would keep root + `excellus/` + `excellus/payment-integrity/`; the ontology would come from `excellus/payment-integrity/ontology.json`.

### Issue 2: client and project IDs are free text (medium)

The Register page offers a fixed list (`clients.js`), but the **backend accepts any text**, and the folder is derived from it:

| ProjectID as saved | Folder | Result |
|---|---|---|
| `Payment Integrity` (from the dropdown) | `payment-integrity` | ✅ correct documents and ontology |
| `paymentintegrity` (typed via the API, or a list edit) | `paymentintegrity` | ❌ no project documents, **no ontology**, and no error: the STTM is silently generated without the project's model |
| `Payment  Integrity ` (extra spaces) | `payment-integrity` | Folder OK, but the access check compares exact text, so it may not match the user's mapping |

The list of valid clients and projects also lives **only in the frontend**, so the backend can't reject a bad one.

**Fix:** keep one **project registry** on the backend, the single source of truth, e.g. a Cosmos container or a `projects.json` in Blob:

```json
{ "clientId": "excellus", "clientName": "Excellus",
  "projectId": "payment-integrity", "projectName": "Payment Integrity",
  "folder": "excellus/payment-integrity", "active": true }
```
- The Register page loads its dropdowns from it (`GET /v2/admin/projects`), replacing the hard-coded `clients.js`.
- Mappings store the **ids** (`excellus`, `payment-integrity`); screens show the names.
- The backend rejects an unknown client/project when registering a user or generating.
- The folder comes from the registry, not from string manipulation.
- At startup (or on demand) the backend can report a registered project with no folder or no ontology, instead of failing silently.

### Smaller points

| # | Point | Recommendation |
|---|---|---|
| 3 | A user mapped to several projects only gets the **first** one on the main page | Show a project selector only for users with more than one assignment |
| 4 | Admins have no assignment, so the main page shows "no project assigned" for them | Give admins a project selector (they may use any project) |
| 5 | A missing project folder or ontology is only logged | Show it to the user ("this project has no ontology yet") and report it to admins |
| 6 | `Role` is stored on both the login and each mapping, and only the login's is used | Keep role on the login only, or define per-project roles deliberately |
| 7 | `tenantId` is always `default` and unused | Remove, or replace with the client id once Issue 1 is fixed |
| 8 | Generated files are stored on the server disk without a client/project path | Store outputs in Blob under the same client/project folder |

---

## 5. Verdict

**The direction is right.** Identity, access checks and the server-side folder filter follow good practice, and every request is checked against Cosmos DB. Two things need fixing before more clients are onboarded:

1. **Folder = client + project** (Issue 1), so two clients can never share project documents or ontologies.
2. **A backend project registry with stable ids** (Issue 2), so a typo can't silently switch off a project's ontology, and the Register page and backend use the same list.

Both are contained changes: the registry, the folder function, the Register page's dropdown source, and moving existing Blob files into `excellus/payment-integrity/` and `excellus/medical-claims/`. Existing mappings would be migrated from names to ids once.

---

## 6. Code map

| File | Role |
|---|---|
| [app/services/auth_service.py](app/services/auth_service.py) | Login, bcrypt, token, user mappings, `has_project_access`, admin roles |
| [app/auth_router.py](app/auth_router.py) | `/v2/auth/login`, `/v2/auth/me/projects`, `require_auth`, `check_project_access` |
| [app/admin_router.py](app/admin_router.py) | `POST /v2/admin/users` (admin only) |
| [app/routergenerator.py](app/routergenerator.py) | Access check on generate; session ownership on refine / status / download |
| [app/services/azure_search_service.py](app/services/azure_search_service.py) | `project_folder`, `documents_for_project`, path-filtered retrieval |
| [app/services/ontology_service.py](app/services/ontology_service.py) | Per-project ontology from the same folder name |
| `P_Frontend_ReactJs/src/data/clients.js` | Today's client/project list (frontend only) |
| `P_Frontend_ReactJs/src/pages/AdminRegisterPage.jsx` | Register user page |
