# Use agent-orchestrated discovery as the primary integration

Status: accepted

Callers such as `official-campus-radar` may treat Codex CLI as a required runtime component and use a Search Orchestrator with Agent Reach and Exa to discover possible WeChat Public URLs. wxcli remains the trust boundary: the orchestrator produces only a Candidate Batch, and only wxcli can validate URLs, deduplicate identities, read WeChat source pages, compare account identity, and produce Article Evidence. This keeps adaptable search strategy outside wxcli without allowing search summaries or agent judgments to become source evidence.

Direct Discovery through wxcli's implemented Brave Discovery Provider remains an optional deterministic path for environments that do not invoke an agent. wxcli does not depend on Agent Reach installation paths, MCP configuration, or Exa credentials; those belong to the Search Orchestrator's runtime. This decision supplements [ADR-0004](0004-separate-discovery-candidates-from-article-evidence.md): Candidate and Evidence separation applies equally to direct and agent-orchestrated discovery.

The trade-off is that agent-orchestrated discovery has model latency, cost, and prompt-injection risk, and its search continuation is not a wxcli cursor. Candidate Ingestion therefore accepts a strict bounded schema, rejects credentials and caller-supplied evidence claims, stamps wxcli observation times itself, and preserves explicit browser authorization before any Chrome fallback.
