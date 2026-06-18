# Mock Security Profiles

Fixtures for the profile-based security model described in
`deployment/security-model.md`. Each file is one profile definition —
the shape that would sit in `profiles.definition` (JSONB) in the platform DB.

## Source of truth used when drafting these

- **Scenarios** (`mcp__fpa__list_scenarios`): `ACTUALS`, `BUD-2026`,
  `FC-2026-Q1`. Scenario kind is encoded today only as a prefix in the
  `description` column (`ACTUALS|...`, `BUDGET|...`, `FORECAST|...`).
- **Domains** (`mcp__fpa__list_kpis` → `domain` field): `gl`, `payroll`,
  `pnl`, `project_economics`, `timesheets`.
- **Dimensions**: `cost_centre`, `account`, `period`, `employee`, `project`,
  `client`, `client_portfolio`, `department` (rollup of `cost_centre`).
- **Departments** (`mcp__fpa__search_hierarchy` on `cost_centre`):
  `Cloud & Infrastructure`, `Cybersecurity`, `Data & Analytics`,
  `Digital Transformation`, `Finance & Controlling`,
  `General & Administration`, `Human Resources`, `Marketing & Sales`,
  `Software Engineering`.

## Data model gap flagged here

The current `scenarios` table does **not** carry a dedicated `kind` column.
Category tokens like `PLAN`, `BUDGET`, `FORECAST`, `ACTUAL` used in these
fixtures assume such a column exists. Proposed addition:

```
ALTER TABLE scenarios ADD COLUMN kind TEXT NOT NULL
  CHECK (kind IN ('ACTUAL','BUDGET','FORECAST'));
```

Token-to-kind mapping used in these profiles:

| Token      | Matches `scenarios.kind` |
|------------|--------------------------|
| `ACTUAL`   | `ACTUAL`                 |
| `BUDGET`   | `BUDGET`                 |
| `FORECAST` | `FORECAST`               |
| `PLAN`     | `BUDGET` or `FORECAST`   |
| `*`        | any                      |

Specificity order when a scenario matches multiple patterns in one profile:
literal id > finest token (`BUDGET`/`FORECAST`/`ACTUAL`) > `PLAN` > `*`. Most
specific wins; no merge.

## Index

### Realistic

| File | Purpose | Rules exercised |
|---|---|---|
| `01_cfo_full_access.yml` | CFO — manager on everything | Hierarchy fallback: only `manager` declared, analyst/planner tools inherit |
| `02_fpa_lead.yml` | FP&A lead — manager on plan, analyst on actuals | Category token `PLAN`, literal `ACTUALS`, domain deny |
| `03_cloud_planner.yml` | Cloud & Infra planner | Planner-only role, dept dimension allow-list |
| `04_software_eng_manager.yml` | Software Engineering manager | Dept scope on plan scenarios, per-role-level scope |
| `05_external_auditor.yml` | External auditor, read-only | Analyst on all three literal scenarios, domain + cost_centre restrictions |
| `06_hr_director.yml` | HR director — payroll domain only | Domain allow-list with other domains implicitly excluded |

### Edge

| File | Purpose | Rules exercised |
|---|---|---|
| `07_overlay_carve_out.yml` | Allow universe, deny a cost centre | Deny wins; allow omitted = universe |
| `08_specificity_override.yml` | `*` → `PLAN` → `BUD-2026` ladder | Most-specific-wins, no merge |
| `09_multi_dim_and.yml` | Department AND cost_centre together | AND across dim keys, OR within list |

### Wrong

| File | Purpose | Rules exercised |
|---|---|---|
| `10_invalid_examples.yml` | Catalogue of validation failures | Unknown dim, unknown domain, unknown pattern, duplicate role, bad role name |

## Combination rules these fixtures cover

- [x] Role hierarchy fallback: only manager declared → applies to planner/analyst tool calls (`01`, `04`)
- [x] Per-role-level scope on same scenario (`04`, `08`)
- [x] Literal id match (`02`, `05`)
- [x] Category token match — `PLAN`, `ACTUAL` (`02`, `06`)
- [x] Wildcard `*` (`01`, `07`, `08`)
- [x] Specificity precedence without merge (`08`)
- [x] Allow + deny on same axis (`02`, `07`)
- [x] Deny-only scope with allow absent (universe minus deny) (`07`)
- [x] Empty role block = full access (`10` valid sibling behaviour documented in `04`)
- [x] AND across dimension keys, OR within values (`09`)
- [x] Domain-only restriction (`06`)
- [x] Validation failures: unknown dim, unknown domain, malformed pattern (`10`)

## Evaluation expectations

Each file has a header comment with `# Expect:` blocks showing what should
resolve for a handful of `(scenario, tool-role)` probes. The evaluator test
suite should turn those into assertions.