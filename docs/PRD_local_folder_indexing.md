# PRD: Local Folder Indexing for Code Indexer Tab

**Author:** TrustBot Team  
**Date:** February 21, 2026  
**Status:** Draft  
**Version:** 1.0

---

## 1. Overview

### 1.1 Problem Statement

Currently, the **"1. Code Indexer"** tab in TrustBot only supports indexing code by cloning a remote Git repository. This requires:

- A valid Git URL and network access to the remote.
- GitPython installed on the system.
- Time spent cloning (even with `depth=1`).

When TrustBot runs on **AKS (Azure Kubernetes Service) pods**, the target codebase is often already available as a local volume mount at `/mnt/storage/`. Requiring users to clone a repo they already have locally is redundant and slow.

### 1.2 Proposed Solution

Add a **"Local Folder"** source option to the Code Indexer tab. Users can toggle between **Git Repository** (existing flow) and **Local Folder** (new flow). When "Local" is selected, the UI presents a folder path input pre-populated with the AKS mount path `/mnt/storage/`, which the user can override by browsing/selecting a specific folder. The indexing pipeline reads files directly from the local folder instead of cloning.

### 1.3 Goals

- Support local folder indexing without requiring Git.
- Pre-populate the AKS pod mount path (`/mnt/storage/`) as the default.
- Allow users to override the default path with a custom folder.
- Reuse the existing `CodeIndex.build()` and `build_call_graph_from_chunks()` pipeline.
- Rename the action button from "Clone and Index Repository" to "Index Codebase" to reflect both sources.

---

## 2. User Stories

| # | As a... | I want to... | So that... |
|---|---------|-------------|------------|
| US-1 | TrustBot user on AKS | Index a codebase already mounted at `/mnt/storage/` | I don't have to clone it again from Git |
| US-2 | TrustBot user | Choose between Git clone and local folder | I can use whichever source is available |
| US-3 | TrustBot user on AKS | Have the mount path pre-filled | I don't need to remember or type the AKS mount path |
| US-4 | TrustBot user | Override the default folder path | I can point to any local directory containing code |
| US-5 | TrustBot user | See a single "Index Codebase" button | The action is clear regardless of source type |

---

## 3. Functional Requirements

### 3.1 Source Selection Checkbox

| ID | Requirement |
|----|-------------|
| FR-1 | Add a **radio button group** (or checkbox toggle) with two options: **"Git Repository"** (default) and **"Local Folder"**. |
| FR-2 | The radio/checkbox must be placed **above** the existing Git URL / Branch row, within the "1. Code Indexer" tab. |
| FR-3 | When **"Git Repository"** is selected, the existing Git URL and Branch fields are **visible and enabled**. The local folder field is **hidden**. |
| FR-4 | When **"Local Folder"** is selected, the Git URL and Branch fields are **hidden**. The local folder path field is **visible and enabled**. |

### 3.2 Local Folder Path Input

| ID | Requirement |
|----|-------------|
| FR-5 | Add a **Textbox** input labeled **"Folder Path"** that appears when the "Local Folder" source is selected. |
| FR-6 | The folder path field must be **pre-populated** with the hardcoded default value: `/mnt/storage/`. |
| FR-7 | The user can **clear or edit** the pre-populated value to specify a different folder path. |
| FR-8 | When the user manually enters/selects a folder path, the hardcoded `/mnt/storage/` default is **replaced** (not appended). |

### 3.3 Button Rename

| ID | Requirement |
|----|-------------|
| FR-9 | Rename the primary action button from **"Clone and Index Repository"** to **"Index Codebase"**. |
| FR-10 | The button label remains **"Index Codebase"** regardless of which source is selected. |

### 3.4 Local Folder Indexing Flow

| ID | Requirement |
|----|-------------|
| FR-11 | When "Local Folder" is selected and "Index Codebase" is clicked, **validate** that the folder path is non-empty and the directory exists. |
| FR-12 | If validation fails, display an appropriate error message in the status area (e.g., "Folder path is empty" or "Folder does not exist: /path/to/folder"). |
| FR-13 | **Walk the selected folder** recursively, collecting all files matching `CODE_EXTENSIONS` (same extensions used by `CodeIndex.build()` and `chunk_codebase()`). |
| FR-14 | Pass the folder path to `CodeIndex.build(codebase_root=folder_path)` to build the SQLite index. |
| FR-15 | Pass the chunked files to `build_call_graph_from_chunks()` to extract call graph edges. |
| FR-16 | Store the edges via `CodeIndex.store_edges()`. |
| FR-17 | Wire the resulting `CodeIndex` into the `ValidationPipeline` via `pipeline.set_code_index()`. |
| FR-18 | Display a Markdown summary in the status area with: folder path, files processed, code chunks created, functions indexed, call graph edges, and duration. |

### 3.5 Git Repository Flow (Existing — Unchanged)

| ID | Requirement |
|----|-------------|
| FR-19 | When "Git Repository" is selected and "Index Codebase" is clicked, the existing `clone_and_index_repo()` flow runs **unchanged**. |
| FR-20 | The only change to the Git flow is the button label (from "Clone and Index Repository" to "Index Codebase"). |

---

## 4. Technical Design

### 4.1 UI Changes (`trustbot/ui/app.py`)

#### 4.1.1 New UI Components

```
Tab "1. Code Indexer"
│
├── Markdown (existing instructions — updated text)
│
├── Radio("Source", choices=["Git Repository", "Local Folder"], value="Git Repository")
│
├── Row [visible when source == "Git Repository"]  ← existing row
│   ├── Textbox("Git Repository URL")
│   └── Textbox("Branch", default="main")
│
├── Row [visible when source == "Local Folder"]    ← NEW row
│   └── Textbox("Folder Path", value="/mnt/storage/")
│
├── Button("Index Codebase", variant="primary")    ← RENAMED
│
└── Markdown(status)
```

#### 4.1.2 Visibility Toggle Logic

Use Gradio's `gr.update(visible=...)` pattern. When the Radio value changes:

```python
def toggle_source(source):
    if source == "Local Folder":
        return gr.update(visible=False), gr.update(visible=True)
    else:
        return gr.update(visible=True), gr.update(visible=False)

source_radio.change(
    fn=toggle_source,
    inputs=[source_radio],
    outputs=[git_row, local_row],
)
```

#### 4.1.3 Button Click Handler

The button click handler inspects the current source selection and routes to the appropriate function:

```python
def _do_index(source, git_url, branch, folder_path):
    if source == "Local Folder":
        result = _run_async(index_local_folder(folder_path))
    else:
        result = _run_async(clone_and_index_repo(git_url, branch))
    return gr.update(interactive=True), result
```

### 4.2 New Async Handler: `index_local_folder()`

Add a new async function alongside the existing `clone_and_index_repo()`:

```python
async def index_local_folder(folder_path: str, progress=gr.Progress()):
    """Index code from a local folder."""
    nonlocal git_index
    
    folder = Path(folder_path.strip())
    if not folder_path.strip():
        return "Please enter a folder path."
    if not folder.exists():
        return f"Folder does not exist: {folder}"
    if not folder.is_dir():
        return f"Path is not a directory: {folder}"
    
    try:
        progress(0.1, desc="Scanning local folder...")
        
        # Chunk all code files
        from trustbot.indexing.chunker import chunk_codebase
        chunks = await asyncio.to_thread(chunk_codebase, folder)
        
        progress(0.4, desc=f"Found {len(chunks)} code chunks, building index...")
        
        # Build code index
        git_index_path = settings.codebase_root / ".trustbot_git_index.db"
        code_idx = CodeIndex(db_path=git_index_path)
        code_idx.build(codebase_root=folder)
        
        function_count = len([c for c in chunks if c.function_name])
        progress(0.6, desc=f"Building call graph from {function_count} functions...")
        
        # Build call graph
        from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks
        edges = await asyncio.to_thread(build_call_graph_from_chunks, chunks)
        
        # Store edges
        edge_tuples = [(e.from_chunk, e.to_chunk, e.confidence) for e in edges]
        code_idx.store_edges(edge_tuples)
        code_idx.close()
        
        progress(0.9, desc="Finalizing...")
        
        git_index = CodeIndex(db_path=git_index_path)
        if pipeline:
            pipeline.set_code_index(git_index)
        
        files_count = len(set(c.file_path for c in chunks))
        progress(1.0, desc="Done!")
        
        return (
            f"## Indexing Complete!\n\n"
            f"**Source**: Local Folder\n"
            f"**Path**: {folder}\n"
            f"**Files processed**: {files_count}\n"
            f"**Code chunks created**: {len(chunks)}\n"
            f"**Functions indexed**: {function_count}\n"
            f"**Call graph edges**: {len(edges)}\n\n"
            f"Codebase is ready. Switch to the **Validate** tab."
        )
    except Exception as e:
        logger.exception("Local folder indexing failed")
        return f"Error: {e}"
```

### 4.3 Affected Files

| File | Change |
|------|--------|
| `trustbot/ui/app.py` | Add Radio source selector, local folder Textbox, visibility toggle, `index_local_folder()` handler, rename button, update click wiring |
| No other files need changes | The existing `CodeIndex.build()`, `chunk_codebase()`, and `build_call_graph_from_chunks()` already accept arbitrary `codebase_root` paths |

### 4.4 Reused Existing Components (No Changes Needed)

| Component | Why It Works As-Is |
|-----------|-------------------|
| `CodeIndex.build(codebase_root=folder)` | Already accepts any `Path` as the root directory; walks the tree and chunks files |
| `chunk_codebase(folder)` | Already accepts any directory path |
| `build_call_graph_from_chunks(chunks)` | Operates on chunk objects, source-agnostic |
| `CodeIndex.store_edges()` | Stores edges regardless of origin |
| `pipeline.set_code_index()` | Wires any `CodeIndex` instance into the pipeline |

---

## 5. UI Mockup (Text)

### 5.1 State: Git Repository Selected (Default)

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. Code Indexer                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ### Step 1: Index Your Codebase                                     │
│  Clone a git repository or select a local folder to build a local    │
│  code index. This index is used by Agent 2 during validation.        │
│                                                                      │
│  Source:  (●) Git Repository  ( ) Local Folder                       │
│                                                                      │
│  ┌──────────────────────────────────────┐ ┌──────────────┐           │
│  │ Git Repository URL                   │ │ Branch       │           │
│  │ https://github.com/username/repo.git │ │ main         │           │
│  └──────────────────────────────────────┘ └──────────────┘           │
│                                                                      │
│  ┌─────────────────────────┐                                         │
│  │   Index Codebase        │                                         │
│  └─────────────────────────┘                                         │
│                                                                      │
│  Status: ...                                                         │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 State: Local Folder Selected

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. Code Indexer                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ### Step 1: Index Your Codebase                                     │
│  Clone a git repository or select a local folder to build a local    │
│  code index. This index is used by Agent 2 during validation.        │
│                                                                      │
│  Source:  ( ) Git Repository  (●) Local Folder                       │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │ Folder Path                                               │        │
│  │ /mnt/storage/                                             │        │
│  └──────────────────────────────────────────────────────────┘        │
│                                                                      │
│  ┌─────────────────────────┐                                         │
│  │   Index Codebase        │                                         │
│  └─────────────────────────┘                                         │
│                                                                      │
│  Status: ...                                                         │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. Detailed Behavior Specification

### 6.1 Source Toggle Behavior

| Action | Result |
|--------|--------|
| Page loads | "Git Repository" is selected by default. Git URL + Branch fields visible. Local folder field hidden. |
| User clicks "Local Folder" | Git URL + Branch fields hide. Folder Path field appears with value `/mnt/storage/`. |
| User clicks "Git Repository" | Folder Path field hides. Git URL + Branch fields reappear (preserving any previously entered values). |

### 6.2 Default Folder Path Behavior

| Scenario | Folder Path Value |
|----------|-------------------|
| User selects "Local Folder" for the first time | `/mnt/storage/` (hardcoded default) |
| User clears the field and types a custom path | Custom path (e.g., `/home/user/myrepo`) |
| User toggles back to Git, then back to Local | Retains whatever value was last entered (Gradio Textbox preserves state) |

### 6.3 Button Click Behavior

| Source | Validation | Action |
|--------|-----------|--------|
| Git Repository | `git_url` must be non-empty | Calls existing `clone_and_index_repo(git_url, branch)` |
| Local Folder | `folder_path` must be non-empty AND directory must exist | Calls new `index_local_folder(folder_path)` |

### 6.4 Error Messages

| Condition | Message |
|-----------|---------|
| Local Folder selected, path is empty | "Please enter a folder path." |
| Local Folder selected, path does not exist | "Folder does not exist: `/path/provided`" |
| Local Folder selected, path is a file (not dir) | "Path is not a directory: `/path/provided`" |
| Git selected, URL is empty | "Please enter a Git repository URL." (existing) |

---

## 7. Data Flow Diagram

```
                    ┌──────────────┐
                    │   User       │
                    └──────┬───────┘
                           │ selects source + clicks "Index Codebase"
                           ▼
                ┌─────────────────────┐
                │  Source = ?          │
                └──────┬──────┬───────┘
         "Git Repo"    │      │   "Local Folder"
                       ▼      ▼
          ┌────────────────┐  ┌─────────────────────┐
          │clone_and_index │  │ index_local_folder   │
          │_repo()         │  │ ()                   │
          │                │  │                      │
          │ 1. git clone   │  │ 1. validate path     │
          │ 2. chunk files │  │ 2. chunk files       │
          │ 3. build index │  │ 3. build index       │
          │ 4. call graph  │  │ 4. call graph        │
          │ 5. store edges │  │ 5. store edges       │
          └───────┬────────┘  └──────────┬──────────┘
                  │                      │
                  ▼                      ▼
          ┌──────────────────────────────────────┐
          │  CodeIndex (SQLite)                   │
          │  .trustbot_git_index.db               │
          │  ┌──────────────┐ ┌──────────────┐   │
          │  │ code_index   │ │ call_edges   │   │
          │  └──────────────┘ └──────────────┘   │
          └──────────────────┬───────────────────┘
                             │
                             ▼
          ┌──────────────────────────────────────┐
          │  pipeline.set_code_index(git_index)   │
          │  → Ready for Agent 2 validation       │
          └──────────────────────────────────────┘
```

---

## 8. AKS / Kubernetes Considerations

| Aspect | Detail |
|--------|--------|
| **Mount Path** | `/mnt/storage/` is the standard AKS persistent volume mount point. This is hardcoded as the default value. |
| **Volume Type** | Typically an Azure Disk or Azure Files PVC mounted into the pod. |
| **Permissions** | The pod's service account must have read access to `/mnt/storage/`. The indexer only reads files (no writes to the mount). |
| **File System** | The SQLite index DB is written to `settings.codebase_root / ".trustbot_git_index.db"`, NOT to `/mnt/storage/`. This avoids polluting the source mount. |
| **Large Codebases** | Local folder indexing skips the clone step, so it's significantly faster than Git clone for large repos already on disk. |

---

## 9. Edge Cases

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| EC-1 | `/mnt/storage/` is empty (no files) | Index completes with 0 files, 0 functions, 0 edges. Status shows the summary with zero counts. |
| EC-2 | Folder contains no code files (only `.txt`, `.md`, etc.) | Index completes with 0 functions. No error. |
| EC-3 | Folder contains thousands of files | Indexing proceeds normally; progress updates shown. May take longer. |
| EC-4 | Folder path has trailing whitespace | Trimmed before use. |
| EC-5 | Folder path uses backslashes on Windows | Works as-is; `Path()` handles normalization. |
| EC-6 | User switches from Local to Git mid-indexing | Button is disabled during indexing; toggle has no effect on in-progress operation. |
| EC-7 | `/mnt/storage/` doesn't exist (non-AKS env) | User sees error: "Folder does not exist: /mnt/storage/". User can type a valid local path. |
| EC-8 | User indexes via Local, then indexes via Git | Second indexing overwrites the same `.trustbot_git_index.db`. Pipeline uses the latest index. |

---

## 10. Acceptance Criteria

| # | Criterion | Pass Condition |
|---|-----------|---------------|
| AC-1 | Radio button is visible on the Code Indexer tab | Two options: "Git Repository" and "Local Folder" |
| AC-2 | Default selection is "Git Repository" | Git URL + Branch fields visible; folder path hidden |
| AC-3 | Selecting "Local Folder" shows folder input | Folder Path textbox appears with value `/mnt/storage/` |
| AC-4 | Selecting "Local Folder" hides Git fields | Git URL and Branch textboxes are hidden |
| AC-5 | Folder path is editable | User can clear `/mnt/storage/` and type a custom path |
| AC-6 | Button reads "Index Codebase" | Old label "Clone and Index Repository" is gone |
| AC-7 | Local indexing works end-to-end | Clicking "Index Codebase" with a valid local path indexes files and shows summary |
| AC-8 | Pipeline receives the index | After local indexing, `pipeline.has_index` returns `True` and validation works |
| AC-9 | Git flow still works | Selecting "Git Repository" and clicking "Index Codebase" clones and indexes as before |
| AC-10 | Error handling for invalid paths | Empty path, non-existent path, and file-not-directory all show clear error messages |

---

## 11. Out of Scope

- **File browser/picker widget**: Gradio does not support native OS folder pickers in a web context. The user types or pastes the path. (A future enhancement could add a directory listing dropdown.)
- **Multiple folder selection**: Only one folder path at a time.
- **Selective file filtering UI**: All code files (matching `CODE_EXTENSIONS`) in the folder are indexed. No UI for include/exclude patterns.
- **Persistent source preference**: The source selection resets to "Git Repository" on page reload.

---

## 12. Implementation Estimate

| Task | Effort |
|------|--------|
| Add Radio + visibility toggle UI | ~30 min |
| Add folder path Textbox with default | ~10 min |
| Implement `index_local_folder()` async handler | ~45 min |
| Update button click wiring to route by source | ~20 min |
| Rename button label | ~2 min |
| Update tab instruction text | ~5 min |
| Manual testing (both flows) | ~30 min |
| **Total** | **~2.5 hours** |

---

## 13. Future Enhancements

- **Directory browser**: Add a tree-view or dropdown that lists subdirectories of `/mnt/storage/` so users can pick without typing.
- **Remember last source**: Persist the user's last-used source selection in browser local storage.
- **Progress granularity**: Show per-file progress during local folder indexing.
- **Incremental local indexing**: Only re-index files that changed since the last indexing run.
