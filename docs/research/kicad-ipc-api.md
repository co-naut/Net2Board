# KiCad IPC API: designs, comparison, and lessons for Net2Board

Date: 2026-10-12. Question: what does KiCad's IPC API (and the design space around it) look like, how does it compare to Net2Board's file-based `kicad-cli` approach, and what should Net2Board adopt? Method: primary sources only — the four official KiCad dev-docs pages, the `kicad-python` (kipy) reference, and this repo's own code; every claim cited inline. One fetch failed (the kipy docs index returned empty); all kipy claims below come from the full `kicad.html`/`board.html` pages, so nothing rests on it.

Related tickets: the three "adopt now" items are filed as public issues (see §Recommendation).

## TL;DR

- Installed `kicad-cli` is **10.0.7**. IPC headless (`kicad-cli api-server`) requires **KiCad 11** — not available here.
- **There is no run-DRC job in the IPC API at any version.** kipy's job surface is exports only. DRC stays `kicad-cli pcb drc` regardless of KiCad version.
- Keep the current design (pure writer + CLI oracle). Adopt three small, version-independent hygiene items; defer everything IPC-shaped until a KiCad 11 install exists.

## Part 1 — Codebase findings

### Module layout (shallow tree)

```
src/net2board/
├── model/__init__.py      # Board root aggregate; frozen dataclasses (ADR-0001..0004)
│   └── constraints.py     # Constraint/relations; anchor_parts, constraint_ref, net_ref
├── geometry/__init__.py   # Point, Rectangle, Shape; integer-nm measures
├── ir/
│   ├── __init__.py        # NetlistIR/CompIR/NetIR/FootprintIR/PadIR
│   ├── parsers.py         # .net + .kicad_mod readers (ADR-0007, ADR-0015)
│   └── _sexpr.py          # minimal s-expr reader (atoms stay strings; no float)
├── boardspec/__init__.py  # BoardSpec.from_files() — the one filesystem door (line 134)
├── build/__init__.py      # build_board(spec) — pure IR→Board join (ADR-0007/0008)
├── export/__init__.py     # export_pcb (line 69), export_dsn (line 362) — pure writers
├── drc/__init__.py        # pure in-process checks; CHECKS registry; run_drc
├── solver/                # _solve, _legalize, _evaluator (ADR-0013, ADR-0016)
├── objective/__init__.py
├── errors.py              # Net2BoardError root (ADR-0008/0013)
└── examples/session.py    # HARNESS ONLY: the kicad-cli oracle shell-out
```

### 1. Board model and identity

- Everything is a frozen dataclass; every mutation returns a new `Board` (`src/net2board/model/__init__.py:1-18`, 164-449). `Pad` (69), `Footprint` (93), `Net` (108), `Component` (120), `Placement` (128), `Board` (164).
- **Identity scheme: none per object.** "All coordinates are integer nanometres (ADR-0005). Objects are referenced from outside by string refs `"<kind>:<id>"` (ADR-0004)" (`model/__init__.py:16-17`). ADR-0004 pins the grammar: `"placement:R1"`, `"net:VCC"`, `"constraint:<id>"` (`docs/adr/0004-board-object-refs-as-strings.md:3-5`). Lookups go through private ref-keyed indexes (`_component_by_ref`, `_placement_index`, `_constraint_index`, `_net_index`, `model/__init__.py:198-233`).
- **No UUIDs/KIIDs anywhere.** A repo-wide grep for `uuid` in `src/` and `tests/` finds only one docstring mention — the exporter deliberately writes the Reference property "anchor, no effects/uuid" (`export/__init__.py:197`).
- **DRC violation → model mapping does not use `violations[].items[].uuid` at all.** Three mechanisms instead:
  1. **Type-string collision**: `ViolationType` enum values deliberately equal the oracle's type strings (`drc/__init__.py:92-103`, ADR-0009 §Type at `docs/adr/0009-violation-struct-and-drc-pipeline.md:25`); the slow tier compares **per-type counts**, never items (`tests/test_oracle_ecc83.py:95-99`, 175-194).
  2. **Net names scraped from description text**: the DRC JSON "carries no structured net field," so the ratsnest reader regexes `[GND]` out of `item["description"]` and asserts loudly when the token is missing (`tests/test_oracle_ecc83.py:102-123`).
  3. **Reference property as join key**: the export writes the placement ref into the footprint's `Reference` property — "the join key SES re-import matches on" (`export/__init__.py:8-10`, 192-207).

### 2. The `.kicad_pcb` exporter

- Emits `(version 20241229)` via `FILE_FORMAT_VERSION = 20241229` (`export/__init__.py:48`, emitted at line 84), plus `(generator "net2board")` / `(generator_version "1")` (49-50, 85-86). Docstring scope: "canonical `.kicad_pcb` text (KiCad 9/10)" (line 70).
- **Writer-only.** There is no `.kicad_pcb` reader in the codebase — the only KiCad files parsed are `.net` and `.kicad_mod` (`ir/parsers.py:1, 59, 81`). So "round-tripping" and "unknown fields" are non-issues by construction: Net2Board never re-reads its own or KiCad's board files; the oracle is the only reader.
- **Determinism posture**: `export_pcb(board) -> str` is "a pure function: no filesystem, no clock, no randomness — the same Board always serializes to the same bytes" (`export/__init__.py:3-5`); canonical ordering everywhere (nets by sorted name, footprints by ref, net 0 reserved, lines 5-8, 98-106); six-decimal integer mm rendering, no floats (297-306).
- **Schema-evolution posture is "probe and pin"**: behaviors are pinned per installed release in comments — "probed on kicad-cli 10.0.6" appears at `export/__init__.py:19-20` (courtyard layer pairing), `:160` (`(locked yes)` parses, `IsLocked()` reads True), `:247-256` (pad world-rotation token). The loader-leniency assumption is documented in `docs/agents/kicad-cli.md:34` ("KiCad's parser is lenient… the minimum that exercises the question"). The version pin lives in exactly one constant, but the *assumptions behind it* are scattered prose, each enforced only by scattered slow-tier assertions (e.g. `tests/test_export.py:326`).

### 3. The DRC pipeline

Two tiers (ADR-0010):

- **Fast tier (engine)**: `run_drc(board)` — pure, ordered `CHECKS` registry, explicit canonical sort (`drc/__init__.py:652-659`, 679-690). Six checks today, one (`courtyards_overlap`) with an oracle analogue, five Net2Board-specific (ADR-0018, #42).
- **Slow tier (harness)**: export → `kicad-cli pcb drc --format json --severity-all -o report.json board.kicad_pcb` → parse JSON:
  - Command constant: `_DRC_COMMAND = ("kicad-cli", "pcb", "drc", "--format", "json", "--severity-all")` — defined **three times** (`examples/session.py:660`, `tests/test_oracle_tinytapeout.py:43`, `tests/test_oracle_ecc83.py:52`).
  - Invocation: `subprocess.run(..., check=False)`; `returncode != 0` → `RuntimeError` with stdout/stderr embedded (`session.py:673-683`); JSON read and `len(report["violations"])` counted (`session.py:684`).
  - Exit-code contract: 0 = parsed (violations may exist in JSON), 3 = parse failure with **no** JSON written, 5 = violations under `--exit-code-violations` (`docs/agents/kicad-cli.md:23-28`; exit 3 asserted against a truncated file at `tests/test_oracle_ecc83.py:197-215`).
  - `unconnected_items[]` flows in only as net-name sets via the description regex (above).
  - Version provenance: `_oracle_version()` shells `kicad-cli version` into the report's `Provenance` block (`session.py:687-691`, 853-855) — matching the repo doctrine "the installed version is the one that runs" (`docs/agents/kicad-cli.md:3`).

### 4. Where a "live KiCad" seam sits

The engine is **pure by enforced rule**: a meta-test bans the tokens `subprocess`, `os.system`, `os.popen`, `kicad_sch` anywhere in `src/net2board/` **except** `examples/session.py`, which is explicitly designated "a harness duty beside the examples, never an engine path" (`tests/test_import_from_files.py:211-228`). The only filesystem door is `BoardSpec.from_files` (`boardspec/__init__.py:134-167`).

So the seam exists **de facto** (module boundary + purity test) but **not de jure**: there is no `Oracle`/`KiCad` Protocol. The oracle surface is exactly two functions (`kicad_drc_candidate`, `session.py:663-684`; `_oracle_version`, `:687-691`) plus the triplicated command constant and the per-type counting helpers duplicated in the two slow-tier test modules. An IPC adapter would replace precisely these — but would have to be invented as a port first.

## Part 2 — KiCad API design patterns (primary sources)

| # | Pattern | Substance | Source |
|---|---|---|---|
| 1 | **Client–server, protobuf envelope over Unix socket** | Protobuf messages + NNG transport over UNIX sockets (named pipes on Windows); each running KiCad is a server; socket `api.sock`, PID-suffixed per extra instance. Request-reply only; no async notifications. | [IPC overview](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html); [For KiCad Developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-kicad-developers/index.html) |
| 2 | **Synchronous UI-thread execution; busy/retry** | API requests are handled like user input on the single UI thread: clients must anticipate timeouts when the user blocks KiCad and retry; opening multiple connections gains nothing (KiCad serializes them). `AS_BUSY` is returned during blocking ops (e.g. `interactive_move`, non-blocking `refill_zones(block=False)`). | IPC overview; for-kicad-developers; [kipy Board](https://docs.kicad.org/kicad-python/board.html) |
| 3 | **Stable identity: KIID everywhere** | `KIID` is a first-class type in `base_types.proto`; every board item carries one; clients address items by KIID (`get_items_by_id(KIID)`, `flip_items_by_id`), and open documents are addressed by `DocumentSpecifier`. `Commit` objects also carry a KIID. | for-kicad-developers ("Common data types"); [kipy KiCad](https://docs.kicad.org/kicad-python/kicad.html); kipy Board |
| 4 | **Transactional editing: commit / save / revert** | Headless `open_document`/`create_document`/`close_document` + explicit `save()`; edits go through commits; one commit-in-progress per client (a second commit attempt returns `AS_BAD_REQUEST`: "the client … already has a commit in progress"). | kipy KiCad; for-kicad-developers (error example) |
| 5 | **API evolution policy** | Follow protobuf best practices: never rename/reorder/re-meaning fields; additive-only; deprecated fields survive ≥1 major version and are never removed in a bugfix release; field indices never reused; enums start with `<PREFIX>_UNKNOWN = 0` and values are only ever deprecated, never deleted; `// Since: X.Y.Z` annotations ride into client bindings. | IPC overview; for-kicad-developers ("Adding, Modifying, and Removing API surface", "Enumerations") |
| 6 | **Error model: `ApiResponseStatus` + `tl::expected`** | Handler signature returns `HANDLER_RESULT<T> = tl::expected<T, ApiResponseStatus>` — value or `{status_code, error_message}`; `AS_UNHANDLED` lets the dispatcher try the next registered handler; unhandled message types surface as errors to the client. Dispatch is a dynamic **handler registry** (`API_HANDLER` subclasses; `API_HANDLER_COMMON` for frame-less handlers, `API_HANDLER_EDITOR` → `API_HANDLER_PCB`/`_SCH`). | for-kicad-developers |
| 7 | **Conceptual, not internal, message shapes** | Don't mirror C++ inheritance in protobuf: `Track`/`Via` are separate flat messages, deliberately; wrap plain scalars in container messages; booleans become enums with `_UNKNOWN = 0`; all distances are **64-bit integer nanometres**. | for-kicad-developers ("Protocol Buffers" practices, "Object Serialization", "Common data types") |
| 8 | **Plugin lifecycle** | `plugin.json` (schema at go.kicad.org/api/schemas/v1) under `${KICAD_DOCUMENTS_HOME}/<version>/plugins`; KiCad launches plugins as separate processes and sets `KICAD_API_SOCKET` + `KICAD_API_TOKEN` (token doubles as a restart detector); Python plugins each get a KiCad-managed venv; non-Python "executable plugins" supported; `EnableAPILogging` writes full protobuf traffic to `logs/api.log`. | [For Add-on Developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html) |
| 9 | **Version capability matrix** | **KiCad 9/10**: GUI instances only — "Support for running in headless mode through `kicad-cli` was added for KiCad 11"; no plotting/exporting via IPC until 11 ("Users of previous versions may make use of `kicad-cli` called from plugins"); PCB editor only until 11 adds schematic. **KiCad 11**: headless `kicad-cli api-server`, exports as jobs, schematic editor, document create/open/close. | for-addon-developers ("API Status and Goals") |
| 10 | **kipy surface (and its limits)** | `KiCad(headless=True, kicad_cli_path=…, file_path=…)` spawns/connects to `kicad-cli api-server`; `load_all_libraries()` + `wait_for_libraries()` are headless-only; `Board` offers `get_items`, `get_items_by_id(KIID)` (since 10.0.0), `get_items_by_net` (since 10.0.1), `place_footprint_from_library` (KiCad 11), `import_netlist(…) → ImportNetlistResult`, `refill_zones(block=True)`, `get_/set_design_rules` (KiCad 11), and fifteen `export_*` jobs → `JobResult{status, succeeded, output_paths, message}` (gerbers, drill, pdf, svg, position, ipc2581, odb, …). **There is no run-DRC job** — the Board-jobs list contains only export/plot jobs. `check_version()` compares binding vs. connected KiCad. | [kipy kicad.html](https://docs.kicad.org/kicad-python/kicad.html); [kipy board.html](https://docs.kicad.org/kicad-python/board.html) |
| 11 | **Legacy SWIG bindings (retiring)** | Deprecated since KiCad 9, "current plan is to remove the SWIG bindings in KiCad 11.0"; raw binding layer over internals, "unstable and will generally change between KiCad major versions"; embedded interpreter, PCB editor only; also usable standalone (load/save files) — but with no stability guarantee. | [PCB Python Bindings](https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html) |
| 12 | **No standalone library, ever** | "There are no plans to support a standalone library for loading and manipulating KiCad files separate from the client-server (IPC) model." | for-addon-developers |

## Part 3 — Synthesis

### 1. The design space

**(a) File-based `kicad-cli` subprocess — Net2Board today.** Export canonical `.kicad_pcb`, shell `pcb drc --format json`, parse. *Pros:* zero shared state, fully scriptable and CI-safe, deterministic bytes, exit codes give a clean parse-acceptance signal (0/3/5), the engine stays pure (subprocess ban, `tests/test_import_from_files.py:211-228`), and behavior is pinned by probing the *installed* binary rather than trusting docs (`docs/agents/kicad-cli.md:3`). *Cons:* full process + load + check per invocation — the harness explicitly measures this as the wrapper baseline: "an engine that shelled to the oracle for every candidate would pay exactly this, times every candidate" (`session.py:663-668`); no live/incremental queries; the JSON forces fragile scraping (net names out of description prose, `tests/test_oracle_ecc83.py:108-111`); the DRC JSON's `items[].uuid` is unusable as a join key without model IDs; version coupling is real and managed only by re-probing. *Maturity gate:* none — this is the only design that works on KiCad 9–10 headless and is KiCad's own recommended fallback for pre-11 automation (for-addon-developers, matrix point 2).

**(b) IPC API client–server (headless `kicad-cli api-server`, KiCad 11+).** kipy `KiCad(headless=True)` → protobuf over socket; `open_document` → `get_board()` → item CRUD by KIID, `place_footprint_from_library`, `import_netlist`, `get/set_design_rules`, `export_*` jobs → `JobResult`. *Pros:* KiCad becomes the parse and geometry authority (no re-implemented transform semantics — the exporter's mirror/bake rules at `export/__init__.py:14-20, 244-256` exist precisely because a file must pre-compute what a live editor would do on load); stable, additive-only protobuf surface; KIID addressing; design-rule read/write; gerber/drill exports as structured results. *Cons:* **blocked on KiCad 11 — installed is 10.0.7**; synchronous UI-thread semantics require retry/busy handling even headless; still **no DRC job in kipy** (pattern 10), so DRC would *remain* a CLI call; server lifecycle and version/capability drift (`// Since` and "with KiCad 11" annotations everywhere) must be managed; kipy's own release cadence is a second coupling. *Maturity:* IPC stable since 9, headless + exports since 11; kipy API marked pre-1.0 (`0.x.y`).

**(c) Legacy SWIG `pcbnew` bindings.** Embedded interpreter; standalone file mode exists. *Pros:* deepest internal access; already proven in this repo's own test rig — the SES re-import test uses system `python3 + pcbnew` (`tests/test_dsn_freerouting.py:108-112`), and that same file records the operational pain: "the venv cannot import the 3.14-built bindings." *Cons:* deprecated in 9, scheduled for removal in 11 (pattern 11); no stability guarantee; CPython-version coupling; PCB-editor-only; single-language. *Verdict:* not adoptable for anything durable; its presence in the test rig is itself version-fragile.

**(d) Direct s-expression text manipulation of `.kicad_pcb`.** Read-modify-write arbitrary boards with a generic sexpr library. *Pros:* no process, no version gate, total control. *Cons:* KiCad explicitly refuses to support this mode (pattern 12); the format shifts every release; you re-implement KiCad's transform/parenting/mirroring semantics or corrupt boards; no DRC. Net2Board's actual posture is the *safe subset* of this design: it is a **pure writer of a minimal canonical subset** plus a reader of `.net`/`.kicad_mod` only — it never claims to interpret arbitrary boards, and it keeps the oracle as the parse authority.

### 2. Comparison table

| Dimension | (a) kicad-cli files (current) | (b) IPC API headless (KiCad 11+) | (c) SWIG pcbnew | (d) raw s-expr editing |
|---|---|---|---|---|
| Parse authority | kicad-cli (exit 3 = reject) | KiCad server (load errors via `ApiResponseStatus`) | KiCad internals | **you** — no authority |
| DRC access | ✔ native (`pcb drc --format json`) | ✖ no run-DRC job in kipy; still CLI | internals only | ✖ |
| Export jobs | ✔ via `pcb export` CLI | ✔ structured `JobResult` (KiCad 11) | partial | ✖ |
| Live GUI interaction | ✖ | ✔ (GUI mode; busy/retry model) | ✔ (in-process) | ✖ |
| CI-friendliness | **best** (headless, no server state) | good once KiCad 11 lands (server lifecycle to manage) | poor (interpreter/build coupling) | good but unsound |
| Stability guarantees | file format + CLI flags (probed per release) | protobuf additive-only, ≥1-major deprecation | none ("will generally change") | file format only, informal |
| Version coupling | per-behavior, probe-pinned (10.0.6/10.0.7 comments) | capability matrix 9/10/11; kipy `check_version()` | removal in 11 | format `version` token |
| Process model | one-shot subprocess per check | long-lived server, 1 connection, sync request/reply | embedded interpreter | none |
| Identity for findings mapping | **none in model**; refs + description scraping | KIID on every item | internal pointers | whatever you invent |

### 3. Modeling lessons for Net2Board (each verified against the code)

1. **Stable identity — refs are the right call for this domain; KIID-class IDs are the right call *when copper arrives*.** KiCad's SES/DRC machinery addresses items by KIID, but KiCad's *own* Specctra-SES importer matches by reference designator — which is exactly why Net2Board writes the `Reference` property as "the join key SES re-import matches on" (`export/__init__.py:8-10, 192-207`). The current finding-mapping needs (per-type counts, net-name sets) are fully served by refs (`tests/test_oracle_ecc83.py:95-123`), and ADR-0004 rejected typed handles deliberately. **Honest limit:** the DRC JSON's `items[].uuid` is unused, and per-pad findings already strain the scheme — ADR-0022 records that pad identity "rides the description and the world-centre `locations`" (`docs/adr/0009-violation-struct-and-drc-pipeline.md:25`). When ADR-0017's copper lands and violations become per-track/per-pad, a minted stable ID per model object (Net2Board's analogue of KIID, e.g. `pad:R1:2`, minted in `build_board`, not a UUID) is the upgrade path — additive to the string-ref grammar of ADR-0004.

2. **Additive-only evolution — the exporter already complies in spirit but pins its assumptions as scattered prose.** KiCad's rule: additive-only, field indices never reused, deprecations survive a major version, `// Since:` annotations (for-kicad-developers). Net2Board's exporter is genuinely lean-and-additive (a minimal subset a lenient parser accepts, `docs/agents/kicad-cli.md:34`), and the single `FILE_FORMAT_VERSION` constant (`export/__init__.py:48`) is one pin. **The gap:** the behavioral assumptions — pad world-rotation token, courtyard layer pairing, `(locked yes)` — are pinned only in per-site comments citing "probed on kicad-cli 10.0.6" (`export/__init__.py:19-20, 160, 247`) with enforcement split across slow-tier tests. Lesson: one explicit `ORACLE_TARGET = "kicad-cli >= 10.0.6"`-style declaration beside `FILE_FORMAT_VERSION`, with the probe assertions gathered under it, would give the exporter the same "deprecation window" discipline the protobuf policy gives the IPC API. Cheap, version-independent, adoptable now.

3. **Integer nanometres — already converged.** The IPC API represents all distances as 64-bit integer nanometres (for-kicad-developers, "Common data types"); Net2Board's ADR-0005 is the same decision (`model/__init__.py:16-17`; lossless 6-decimal int rendering, `export/__init__.py:297-306`). A future IPC adapter needs a `Point ↔ Vector2` shim, not a unit layer. Also convergent: kipy's "conceptual, not C++-hierarchy" message design matches Net2Board's model-first vocabulary with `shape_enum` carried as an opaque KiCad string (`model/__init__.py:72-75`).

4. **Error/status model — the exit-code contract deserves one typed home.** `ApiResponseStatus` = `{status code, error message}` with `AS_UNHANDLED` for routing (for-kicad-developers). Net2Board's analogue exists but is split: `Net2BoardError` root with structured phase errors (`errors.py:19`, ADR-0008) for the library, and an *undocumented-in-code* exit-code vocabulary (0/3/5) enforced only in tests (`tests/test_oracle_ecc83.py:197-215`) while documented in `docs/agents/kicad-cli.md:23-28`. The concrete smell: `_DRC_COMMAND` is defined three times (`session.py:660`; `tests/test_oracle_tinytapeout.py:43`; `tests/test_oracle_ecc83.py:52`). Lesson: a tiny `KiCadOracle` Protocol (`version() -> str`, `drc_report(board) -> OracleReport`) with one `KicadCliAdapter` (mapping returncode → `parsed`/`parse_failed`/violations) would mirror the ApiResponseStatus move — value-or-status instead of raise-or-count — and become the slot an IPC adapter fills later. **Honest limit:** on KiCad 10 this is hygiene, not necessity; the duplication is three constants, not three architectures.

5. **Commit/transaction semantics — Net2Board's snapshots already exceed it; nothing to adopt.** IPC editing is commit-based with per-client single-commit locks, `AS_BUSY` during blocking ops, and blocking `refill_zones` (kipy Board). Net2Board's immutable snapshots ("every mutation returns a new Board… branch, backtrack, and diff without aliasing", `model/__init__.py:4-7`) give stronger guarantees for its solver domain — byte-equal replay is a pinned acceptance property (`session.py:607-609`). If an IPC adapter ever drives a live board, the natural mapping is: one immutable `Board` snapshot → one commit, with snapshot equality as the revert/diff oracle. **This lesson does not generate code today; it future-proofs the adapter design.**

6. **Port/adapter seam — real but informal.** The engine's purity is test-enforced (`tests/test_import_from_files.py:211-228`), and the oracle surface is exactly two functions in the harness (`session.py:663-691`). An IPC adapter would replace precisely those. Lesson: formalize the seam as a Protocol when (and only when) a second adapter becomes plausible — i.e., when a KiCad 11 install exists to write the adapter against. Until then the de-facto boundary is doing the job.

7. **Plugin lifecycle — does not apply.** `plugin.json`, per-plugin venvs, `KICAD_API_SOCKET`/`TOKEN` (for-addon-developers) matter only for code *launched by* KiCad. Net2Board is purely a client; with kipy it would just read those env vars or pass `socket_path`. No modeling change.

### 4. Recommendation (gated on installed kicad-cli 10.0.7)

**Adopt now (version-independent, small):**
1. Collapse the triplicated `_DRC_COMMAND` + exit-code/parse logic into one harness-level oracle module (or Protocol) with a typed report result — the ApiResponseStatus-shaped move, done for hygiene (lesson 4, 6).
2. Declare the exporter's oracle/format target explicitly beside `FILE_FORMAT_VERSION` and gather the probe assertions under it (lesson 2).
3. Keep string refs as identity; add one decision-record sentence noting `items[].uuid` is deliberately unused and naming the minted-ID upgrade path for the copper milestone (lesson 1).

**Keep as-is:** file-based `kicad-cli pcb drc` as the sole DRC authority — it is the only DRC channel that exists at *any* version (kipy has no run-DRC job even on KiCad 11), and the wrapper-baseline measurement (`session.py:663-668`) shows why the oracle stays slow-tier.

**Defer until KiCad 11 is installed here:** any kipy/IPC adapter. At that point, evaluate headless IPC for (a) parse-acceptance smoke tests and (b) `export_gerbers`/`export_drill` jobs with structured `JobResult` results — DRC stays CLI. Do not adopt SWIG (removed in 11); do not build a general `.kicad_pcb` re-reader (unsupported mode; the writer-only posture is correct).

## Sources

- Repo: file:line citations above (verified 2026-10-12, branch `dev`, kicad-cli 10.0.7).
- KiCad: [IPC API overview](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html) · [For KiCad Developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-kicad-developers/index.html) · [For Add-on Developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html) · [PCB Python Bindings (SWIG)](https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html) · [kipy kicad.html](https://docs.kicad.org/kicad-python/kicad.html) · [kipy board.html](https://docs.kicad.org/kicad-python/board.html)
- Failed fetch: `https://docs.kicad.org/kicad-python/` (index; empty response) — all kipy claims above are from `kicad.html`/`board.html`, which fetched in full.

*(Per `docs/agents/issue-tracker.md`, wayfinder research findings live at `docs/research/<name>.md`, readable from a throwaway `research/<name>` branch without checkout.)*
