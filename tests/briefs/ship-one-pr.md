<!-- The worker looped 8 times: the evaluator read "Ship" and held DONE and BLOCKED reports alike as not met. -->
# ABC-501: Global command palette in the dashboard sidebar (ONE PR)
GOAL: Ship the command palette in the dashboard as ONE PR, per the approved plan with its recommendations, simplified on the DB side.
SCOPE: In: packages/dashboard (the sidebar trigger, the palette component, keyboard layer, recents), packages/schema (new GraphQL roots), services/api (resolvers for paletteSearch and paletteResolve, reusing existing query code), tests. Out: no changes to the shared UI package; no migrations or new indexes unless a measured latency problem forces it.
CONTEXT: Read ABC-501 and the approved plan. Use the plan's recommended answers for every open question in section 8.
ACCEPTANCE:
- Placement: at the top of the sidebar under the tenant switcher, above Dashboard; a 32px row with a search icon, a "Search" label and a ⌘K badge.
- ⌘K / Ctrl+K / "/" open it; Esc closes it. It shows recents (localStorage, up to 5), a pages grid filtered like the sidebar, actions, entity results with highlighting, and the paste-an-ID resolver.
- Go tests for the resolvers (permission filtering, the resolver type detection) and dashboard tests for the palette (keyboard, ranking, recents).
VERIFY: `go build ./services/api/... && go test ./services/api/internal/palette/...`; in packages/dashboard: `pnpm typecheck && pnpm lint && pnpm vitest run src/palette`.
TIMEBOX: 300 turns.
FORBIDDEN: edits under the shared UI package; migrations or indexes without measured need; prod access; merging.
NOTES: Self-review at high before ready.
