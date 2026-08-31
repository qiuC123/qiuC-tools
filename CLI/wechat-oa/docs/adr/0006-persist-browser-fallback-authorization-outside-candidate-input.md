# Persist browser fallback authorization outside candidate input

Status: accepted

wxcli 0.5.0 adds a user-level Browser Fallback Policy that defaults to `never` and may be explicitly changed once to permit automatic fallback after an HTTP Content Provider returns Verification Required. This durable authorization is local to wxcli, applies only to strict `mp.weixin.qq.com/s` Public URLs, cannot be enabled by a Candidate Batch or calling project, and can always be narrowed for one invocation; existing `--browser` behavior remains compatible while a distinct one-shot fallback control is added.

A trusted Direct Discovery Request is part of wxcli's caller-owned control plane, not candidate data. Its `allow_browser: true` field may grant browser use for that invocation only, equivalent to a one-shot local control. It never changes the durable Browser Fallback Policy. Candidate Batches remain untrusted data and cannot contain or derive browser authorization.

An automatic fallback creates at most one visible Browser Run per batch and reuses the single independent wxcli Browser Session without importing or exporting Cookies. It never visits Article external links, company websites, ATS pages, Official Account administration pages, or QR payloads. If the stored session still encounters a human challenge, the unattended request returns User Action Required instead of waiting indefinitely or attempting to bypass verification; the user refreshes the session separately through the explicit browser-login workflow.

User Action Required preserves the existing `verification_required` status and exit-code family, adding optional browser-stage and required-action fields rather than a new schema-v1 enum value. Because the whole batch shares one Browser Session, the first human challenge ends that Browser Run and leaves unvisited browser-eligible candidates pending session refresh. Chrome crashes are not automatically restarted in 0.5.0.

The complete command is bounded to ten minutes: HTTP may use at most five minutes and the Browser Run may use the remaining time, never more than five minutes. Missing persistent policy means `never`; corrupt or unsupported policy also fails closed to `never` with a visible configuration diagnostic, while explicit per-invocation controls remain available.

The existing `--browser` switch keeps its 0.4.0 direct-Chrome meaning for compatibility. Version 0.5.0 adds a distinct one-shot fallback control and a local prohibition that overrides durable authorization. Policy state remains separate from Browser Session state, so clearing the independent profile does not silently change the user's chosen fallback policy.

The explicit `browser login` workflow only initializes or refreshes the independent Browser Session. Completing its visible window is not remote-verification proof; only a later successful real Article read records `last_successful_read_at`.
