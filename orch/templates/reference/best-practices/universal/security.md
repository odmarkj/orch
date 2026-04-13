# Security Best Practices

Guidance for building security into the development lifecycle -- from SAST tool configuration and threat modeling through OWASP Top 10 mitigations, secrets management, and compliance mapping. Security is not a phase; it is a continuous practice integrated at every stage from design through deployment.

Effective security requires defense in depth: preventive controls stop attacks, detective controls identify them in progress, and corrective controls enable recovery. Threat modeling provides the framework for deciding where to invest, while automated scanning catches the issues that human review misses. The goal is to make insecure code harder to write than secure code.

---

## Threat Modeling

- **STRIDE per component** -- systematically evaluate every component for Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, and Elevation of Privilege
- **Data flow diagrams first** -- map all data flows, trust boundaries, and entry points before identifying threats; focus on data movement, not just components
- **Trust boundary crossings are high-risk** -- any data flow that crosses a trust boundary (internet to DMZ, app to database) requires extra scrutiny
- **Attack trees for critical paths** -- model the goal an attacker wants to achieve as a tree; OR nodes represent alternative approaches, AND nodes require all children to succeed
- **Risk scoring** -- multiply impact by likelihood (each on a 1-4 scale); prioritize threats scoring 9+ as critical, 6+ as high
- **Involve developers in sessions** -- threat modeling by security teams alone misses implementation-level risks; collaborative sessions produce better coverage
- **Update threat models with architecture changes** -- a threat model is a living document; any new service, API, or data store invalidates the previous analysis

## SAST Configuration

- **Semgrep for custom rules and fast scans** -- pattern-based matching across 30+ languages; excellent for organization-specific security policies
- **SonarQube for code quality plus security** -- combines security hotspot analysis with technical debt tracking and quality gates
- **CodeQL for deep analysis** -- GitHub-native variant analysis; best for researching vulnerability patterns across a codebase
- **Start with baseline scan** -- run an initial scan to establish current posture; prioritize critical and high findings; create a remediation roadmap
- **Incremental adoption** -- begin with security-focused rules only; gradually add code quality rules; block builds only for critical issues initially
- **False positive management** -- document every suppression; create allow-lists for known safe patterns; review suppressions regularly
- **Exclude test and generated code** -- scan production code only; use path filters and incremental scanning for performance

## OWASP Top 10

- **Broken access control (A01)** -- enforce authorization checks server-side on every request; test for IDOR vulnerabilities; implement least privilege
- **Cryptographic failures (A02)** -- encrypt sensitive data at rest (AES-256) and in transit (TLS 1.3); rotate keys automatically; never roll your own crypto
- **Injection (A03)** -- use parameterized queries exclusively; validate and sanitize all input; apply Content Security Policy headers
- **Insecure design (A04)** -- threat model during design; use secure design patterns; do not rely solely on testing to catch design flaws
- **Security misconfiguration (A05)** -- harden defaults; disable unused features; automate configuration validation; review error messages for information leakage
- **Vulnerable components (A06)** -- scan dependencies with Snyk, pip-audit, or npm audit; automate updates; maintain a software bill of materials (SBOM)
- **Authentication failures (A07)** -- implement MFA; enforce strong password policies; protect against credential stuffing with rate limiting and account lockout
- **Data integrity failures (A08)** -- verify software and data integrity with digital signatures; validate CI/CD pipeline integrity
- **Logging and monitoring failures (A09)** -- log all security-relevant events; protect log integrity; implement alerting for suspicious patterns
- **Server-side request forgery (A10)** -- validate and sanitize all URLs; use allowlists for permitted destinations; segment internal networks

## Authentication Patterns

- **OAuth 2.0/2.1 for delegated authorization** -- use authorization code flow with PKCE for web and mobile; avoid implicit grant
- **JWT security** -- validate signature, issuer, audience, and expiration on every request; use short-lived access tokens with refresh token rotation
- **Zero-trust architecture** -- verify identity at every request; never trust network location alone; implement continuous verification
- **MFA for sensitive operations** -- require a second factor for authentication, privilege escalation, and high-value transactions
- **Session management** -- use secure, HttpOnly, SameSite cookies; invalidate sessions on logout and password change; set reasonable expiration

## Authorization

- **RBAC for most applications** -- role-based access control is simple and sufficient for most systems; define roles that map to business functions
- **ABAC for fine-grained control** -- attribute-based access control evaluates policies against user, resource, and environment attributes at runtime
- **Server-side enforcement** -- never rely on client-side authorization checks; validate permissions on every API endpoint
- **Principle of least privilege** -- grant the minimum permissions required; review and revoke unused access regularly

## Secrets Management

- **Never commit secrets to Git** -- use pre-commit hooks (TruffleHog, GitGuardian) to block accidental commits; scan history for leaked secrets
- **HashiCorp Vault for centralized management** -- dynamic secret generation, automatic rotation, fine-grained access control, and audit logging
- **Cloud-native secret stores** -- AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager for cloud-deployed applications
- **Short-lived tokens** -- prefer tokens that expire in minutes over long-lived API keys; use service account rotation
- **External Secrets Operator for Kubernetes** -- sync secrets from Vault or cloud stores into Kubernetes Secrets automatically
- **Mask secrets in CI/CD logs** -- use `::add-mask::` in GitHub Actions or masked variables in GitLab to prevent log exposure
- **Rotate on suspected compromise** -- have a documented process to rotate all secrets within hours; automate where possible

## Dependency Security

- **Automated dependency scanning in CI** -- run Snyk, Dependabot, or OWASP Dependency-Check on every pull request
- **Pin dependency versions** -- use lockfiles (package-lock.json, poetry.lock) to ensure reproducible builds
- **Monitor for new CVEs** -- subscribe to security advisories for critical dependencies; patch high-severity vulnerabilities within 48 hours
- **SBOM generation** -- maintain a software bill of materials for compliance and incident response; tools like Syft or CycloneDX automate this

## Security Requirements

- **Trace every requirement to a threat** -- each security requirement should map to one or more identified threats from the threat model
- **Testable acceptance criteria** -- "encrypt PII at rest" is vague; "AES-256 with KMS key rotation every 90 days" is testable
- **Compliance mapping** -- link requirements to frameworks (PCI-DSS, HIPAA, GDPR, SOC 2) early; automated mapping prevents audit surprises
- **Security user stories** -- "As a security-conscious user, I want the system to require MFA, so that my account is protected from credential theft"

## Defense in Depth

- **Layer controls** -- combine network (firewall, WAF), application (input validation, auth), data (encryption), and process (training, incident response) controls
- **Mix control types** -- preventive controls (firewall) stop attacks; detective controls (IDS, logging) identify them; corrective controls (incident response) recover from them
- **No single point of failure** -- if one control fails, another must catch the attack; test by disabling controls individually
- **Regular control testing** -- validate that controls actually work through penetration testing, red team exercises, and chaos engineering

## Secure Development Practices

- **Input validation at every boundary** -- validate type, length, format, and range; use allowlists over denylists; validate on the server even if the client validates too
- **Parameterized queries only** -- never concatenate user input into SQL, LDAP, or OS commands; use prepared statements or ORM query builders
- **Output encoding for context** -- encode output for the target context (HTML entities, JavaScript escaping, URL encoding) to prevent XSS
- **Security headers** -- configure CSP, HSTS, X-Frame-Options, X-Content-Type-Options, and SameSite cookie attributes on every response
- **Fail securely** -- when an error occurs, deny access by default; do not expose internal state through error responses
- **Least privilege for service accounts** -- database connections, API keys, and cloud IAM roles should have the minimum permissions required

## Container and Infrastructure Security

- **Minimal base images** -- use distroless or Alpine images; fewer packages means fewer vulnerabilities
- **Image scanning in CI** -- scan container images with Trivy, Anchore, or Aqua before pushing to a registry
- **Kubernetes Pod Security Standards** -- enforce restricted security contexts; disable privilege escalation, run as non-root, drop capabilities
- **Network policies** -- restrict pod-to-pod communication to only the paths required; default-deny all traffic then allow explicitly
- **Supply chain security** -- sign artifacts with Sigstore/cosign; maintain SBOM; enforce SLSA build provenance

## Incident Response

- **Documented incident response plan** -- define roles, communication channels, escalation paths, and severity classifications before an incident occurs
- **Forensic readiness** -- ensure logs are immutable, centralized, and retained long enough for investigation; include correlation IDs for tracing
- **Breach notification process** -- know your regulatory obligations (GDPR 72 hours, HIPAA 60 days); prepare template notifications in advance
- **Post-incident review** -- conduct blameless postmortems; focus on systemic improvements, not individual blame; track action items to completion

## Security Anti-Patterns

- **Security as an afterthought** -- bolting security onto a finished application is expensive and incomplete; shift left
- **Security through obscurity** -- hiding endpoints, obfuscating code, or using non-standard ports provides no real protection
- **Overly permissive defaults** -- ship with the strictest settings; let administrators relax them deliberately
- **Trusting user input** -- validate everything at the boundary; sanitize for the output context (HTML, SQL, shell)
- **Generic error messages to attackers, detailed errors in logs** -- never expose stack traces or internal details in API responses
- **Long-lived credentials** -- prefer short-lived tokens with automatic rotation; static API keys that never expire are a breach waiting to happen
- **Single layer of defense** -- relying on only a WAF or only input validation; attackers bypass individual controls; layer multiple defenses
