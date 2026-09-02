# Support native Exa Direct Discovery without weakening evidence boundaries

Status: accepted

WeChat OA supports both Brave and Exa as replaceable Direct Discovery Providers because callers such as `official-campus-radar` invoke the CLI directly and may own only an Exa credential. The selected provider's key remains in the WeChat OA Windows credential store, never in process arguments, environment handoff, Candidate Batch data, or output; Brave remains the default for backward compatibility.

Exa receives the bounded query and the host-only domain filter `mp.weixin.qq.com`; every returned URL must still pass WeChat OA's strict HTTPS `mp.weixin.qq.com/s` validation before it becomes an Article Candidate. Exa titles, authors, dates, ranks, and identifiers remain untrusted search provenance. Only Hydration from the WeChat source may produce `published_at`, Account Identity Evidence, or Article Evidence, and selecting Exa never authorizes Chrome or Media Analysis.

The Direct Discovery result stays at schema v1 and the outer JSON envelope is unchanged. Existing top-level error codes and exit codes remain compatible; `error.details.provider` and the stable `error.details.reason` distinguish provider configuration, authentication, rate-limit, timeout, network, upstream-error, and invalid-response outcomes.
