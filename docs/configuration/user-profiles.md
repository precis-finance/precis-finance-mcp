# User profiles & permissions

Signing in establishes *who* a user is; a **profile** establishes *what they
may read*. Every scenario-scoped tool call over `/mcp` is checked against the
caller's profile, and a user with no profile authenticates but can read
nothing. This page is the profile YAML schema and the rules the server
applies — the provisioning commands around it are in
[Remote access — sign-in & identity modes](../deployment/oauth-keycloak.md).

Profiles live in the platform PostgreSQL and are managed with the admin CLI
(YAML in, YAML out). Changes take effect on the caller's **next request** —
permissions are loaded per call, so there is nothing to restart.

## The shape

A profile is a named tree: scenario pattern → role → scope.

```yaml
# analyst.yml
profile_id: finance-analyst        # lowercase id: letters, digits, _ , -
name: Finance Analyst
description: Read access to actuals and plans, finance cost centres only.

scenarios:
  "ACTUALS":                       # a literal scenario id…
    analyst:                       # …grants the analyst (read) role…
      domains:
        allow: [pnl, gl]           # …on these catalogue domains only…
      dimensions:
        allow:
          department: [Finance & Controlling]   # …for these rows only.

  "PLAN":                          # a category token: every budget + forecast
    analyst: {}                    # empty block = full access at this level
```

Three layers, top to bottom:

- **Scenario patterns** — the keys of `scenarios:` select which datasets the
  block applies to. A pattern is a **literal scenario id** (`ACTUALS`,
  `BUD-2026`), a **kind token** (`ACTUAL`, `BUDGET`, `FORECAST` — matching
  the `kind` your [scenario registry](clickhouse-schema-contract.md#semanticscenarios-the-scenario-registry)
  declares, or `PLAN` for budget-or-forecast), or the wildcard `"*"`.
  When a scenario matches several patterns in the same profile, the **most
  specific wins and nothing is merged**: literal id beats
  `BUDGET`/`FORECAST`/`ACTUAL`, which beat `PLAN`, which beats `*`.
- **Roles** — inside each pattern, up to three named slots: `analyst`,
  `planner`, `manager`. In this read-only package every `/mcp` tool is
  analyst-level, so **`analyst` is the slot that matters here**; the higher
  roles exist for products built on this package and are harmless to grant.
  The hierarchy falls back upward: if a scenario block declares only
  `manager`, analyst-level calls use the manager block's scope.
- **Scopes** — inside each role, two optional axes. An **empty block
  (`{}`) means full access** at that level.

## Scope semantics

```yaml
    analyst:
      domains:
        allow: [pnl, gl]       # omit allow = every domain
        deny:  [payroll]       # subtracted afterwards — deny always wins
      dimensions:
        allow:
          cost_centre: [CC-FIN-01, CC-FIN-02]   # OR within a list
          period:      [2026-01, 2026-02, 2026-03]
        deny:
          cost_centre: [CC-FIN-99]
```

- **`domains`** restricts which catalogue domains the user may query
  (the `domain:` of each metric — see
  [Catalogue & semantic model](catalogue-and-semantic.md)).
- **`dimensions`** restricts rows: keys are dimension names, values are
  member ids. Any catalogue dimension works — a leaf (`cost_centre`) or a
  **parent level derived from it** (`department`, `fs_line`, a hierarchy
  node): a parent member expands to its current leaves at query time.
  Multiple keys combine as **AND** (the row must satisfy every clause); the
  ids within one list combine as **OR**.
- On both axes: an absent `allow` means "the universe"; `deny` is applied
  after `allow` and **deny wins** on conflict.

**Scope at the highest level that expresses the rule.**
`department: [Cloud & Infrastructure]` resolves to whichever cost centres
belong to that department *when the query runs* — a cost centre added next
quarter is in scope automatically, with no profile edit. The equivalent
`cost_centre:` list is a maintenance liability: every new cost centre means
revisiting every profile that should have included it. Reserve leaf-member
lists for genuine exceptions — a named carve-out, an auditor pinned to
specific cost centres.

The engine applies the resolved scope inside the query itself — a scoped
user's table simply contains no out-of-scope rows, and hierarchy search
won't return out-of-scope members.

## Worked examples — compose from these

Five complete profiles, simplest first. Each adds one idea; real profiles are
combinations of these patterns.

**1. Read everything.** The baseline for trusted finance users:

```yaml
profile_id: read-everything
name: Read Everything
description: Unrestricted read access to every scenario and domain.
scenarios:
  "*":
    analyst: {}
```

**2. Everything except one domain.** A deny-only scope: `allow` omitted means
the universe, then `deny` subtracts. Here payroll figures are off-limits:

```yaml
profile_id: no-payroll
name: All Domains Except Payroll
description: Read everything except the payroll domain.
scenarios:
  "*":
    analyst:
      domains:
        deny: [payroll]
```

**3. A department's analyst.** Allow-lists on both axes: only the P&L and GL
domains, only the rows of one department. The scope names `department` — a
parent dimension derived from `cost_centre` — so every cost centre in the
department is covered, including ones created after the profile. Multiple
dimension keys would combine as AND:

```yaml
profile_id: cloud-analyst
name: Cloud Department Analyst
description: P&L and GL, restricted to the Cloud & Infrastructure department.
scenarios:
  "*":
    analyst:
      domains:
        allow: [pnl, gl]
      dimensions:
        allow:
          department: [Cloud & Infrastructure]
```

**4. Different access per scenario kind.** Kind tokens grant by *category*
instead of naming scenarios one by one — `BUDGET` matches every scenario
whose registry `kind` is BUDGET (this year's, next year's, ones created
later), and likewise `FORECAST` and `ACTUAL`. Here every budget is open,
every forecast reads through the P&L only, and actuals are not mentioned —
**no matching pattern means no access**, so this user never sees an actuals
scenario:

```yaml
profile_id: budgets-open-forecasts-summary
name: Budgets Open, Forecasts Summary Only
description: Full read on every budget; forecasts via the P&L; no actuals.
scenarios:
  "BUDGET":                # every budget scenario, present and future
    analyst: {}
  "FORECAST":              # every forecast scenario
    analyst:
      domains:
        allow: [pnl]
```

`PLAN` is the umbrella token for budget-or-forecast — use it when both kinds
get the same scope. The fine tokens beat it: declaring `"PLAN"` and
`"FORECAST"` together gives forecast scenarios the `FORECAST` block alone.

**5. A carve-out and an override.** Two rules at once: the wildcard grants
the universe minus one confidential cost centre (deny wins over the implicit
allow-all), and the literal id pins one named scenario to a tighter scope —
literal beats `"*"`, and the matched block replaces, never merges:

```yaml
profile_id: scoped-with-override
name: Everything Except Confidential, Tight on BUD-2026
description: Universe minus one cost centre; the 2026 budget restricted to GL.
scenarios:
  "*":
    analyst:
      dimensions:
        deny:
          cost_centre: [CC-EXEC-01]
  "BUD-2026":              # most specific — this block alone applies here
    analyst:
      domains:
        allow: [gl]
```

Note that on `BUD-2026` the cost-centre carve-out does **not** apply — the
literal block replaced the wildcard block entirely. If the carve-out should
hold there too, repeat it inside the `BUD-2026` block.

## Managing profiles

Run the CLI inside the server container
(`docker compose -f deploy/docker-compose.yml exec precis-mcp python -m …`):

```bash
python -m precis_mcp.admin_cli profile create --file analyst.yml
python -m precis_mcp.admin_cli profile list
python -m precis_mcp.admin_cli profile show finance-analyst     # round-trips the YAML
python -m precis_mcp.admin_cli profile update finance-analyst --file analyst.yml
python -m precis_mcp.admin_cli profile delete finance-analyst

python -m precis_mcp.admin_cli assign --user bob --profile finance-analyst
python -m precis_mcp.admin_cli revoke --user bob
```

Each user holds **one profile at a time** — `assign` replaces any previous
assignment. Build per-audience profiles (`finance-analyst`,
`external-auditor`, …) rather than per-user ones, and assign them to many
users.

The repository ships a worked set under `mock_profiles/` — realistic shapes
(full-access, scoped analyst, department planner) plus edge cases
(deny-carve-outs, specificity ladders) with the expected resolution written
out in each file's header comment.

## What to know

- **The admin flag is not data access.** `is_admin` gates admin tools
  (managing users and profiles); it does not bypass the scenario check.
  An admin who should also query data needs a profile like anyone else.
- **Names are validated for shape, not existence.** Profile YAML is
  schema-checked at create time (unknown fields and malformed ids are
  rejected), but domain names, dimension names, and member ids are **not**
  checked against your catalogue.

    !!! warning "A typo'd `allow:` locks the user out"
        A misspelled name in an `allow` list matches nothing, so it denies
        everything it was meant to grant; a typo in a `deny` list silently
        does nothing. After creating a profile, verify with `profile show`
        and one probe query as the assigned user.
- **A scenario nobody's pattern matches is invisible** to that user —
  `list_scenarios` already omits it, and querying it is refused.
- **Start from least privilege.** Grant `analyst` on the scenarios the
  audience needs, scope domains and dimensions explicitly, and keep `"*"`
  for genuinely unrestricted roles.

## Related

- [Remote access — sign-in & identity modes](../deployment/oauth-keycloak.md)
  — creating users, the first admin, and verifying sign-in.
- [Catalogue & semantic model](catalogue-and-semantic.md) — where domains
  and dimensions are defined.
- [ClickHouse schema contract](clickhouse-schema-contract.md) — the scenario
  registry the patterns match against.
