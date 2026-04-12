# Security, Auth & Compliance Agent Skills

Security skills are dominated by Trail of Bits (21 skills covering smart contract security, static analysis, variant analysis, constant-time crypto, and more) and supplemented by OpenAI (threat modeling, security best practices, ownership mapping), Better Auth (7 authentication skills), and Microsoft (Entra ID across all languages). Community contributions include 753 cybersecurity skills from mukul975 and several focused security tools.

Trail of Bits skills are the gold standard for security auditing -- they cover everything from Semgrep rule creation to DWARF debugging format expertise to Firebase APK scanning. For authentication, Better Auth provides a complete setup-to-2FA pipeline. OpenAI's security skills focus on code review and threat modeling at the repository level.

---

## Trail of Bits -- Security Auditing (21 skills)
- **trailofbits/static-analysis** -- CodeQL, Semgrep, and SARIF toolkit
- **trailofbits/semgrep-rule-creator** -- Create and refine Semgrep rules for vulnerability detection
- **trailofbits/semgrep-rule-variant-creator** -- Port Semgrep rules to new languages with test-driven validation
- **trailofbits/variant-analysis** -- Find similar vulnerabilities via pattern-based analysis
- **trailofbits/differential-review** -- Security-focused diff review with git history analysis
- **trailofbits/audit-context-building** -- Deep architectural context via ultra-granular code analysis
- **trailofbits/building-secure-contracts** -- Smart contract security for 6 blockchains
- **trailofbits/entry-point-analyzer** -- Identify state-changing entry points in smart contracts
- **trailofbits/constant-time-analysis** -- Detect compiler-induced timing side-channels in crypto
- **trailofbits/insecure-defaults** -- Detect hardcoded secrets, default creds, weak crypto
- **trailofbits/sharp-edges** -- Identify error-prone APIs and dangerous configurations
- **trailofbits/firebase-apk-scanner** -- Scan Android APKs for Firebase misconfigurations
- **trailofbits/property-based-testing** -- Property-based testing for multiple languages
- **trailofbits/spec-to-code-compliance** -- Specification-to-code compliance for blockchain audits
- **trailofbits/burpsuite-project-parser** -- Search and extract from Burp Suite project files
  Source: https://officialskills.sh/trailofbits/skills/

## OpenAI -- Security Review
- **openai/security-best-practices** -- Review code for language-specific security vulnerabilities
- **openai/security-threat-model** -- Generate repo-specific threat models with trust boundaries
- **openai/security-ownership-map** -- Map people-to-file ownership, compute bus factor, identify risks
  Source: https://officialskills.sh/openai/skills/

## Authentication

### Better Auth (7 skills)
- **better-auth/create-auth** -- Create authentication setup
- **better-auth/best-practices** -- Integration best practices
- **better-auth/emailAndPassword** -- Email/password authentication
- **better-auth/twoFactor** -- Two-factor authentication
- **better-auth/organization** -- Organization management
- **better-auth/providers** -- Authentication providers
- **better-auth/explain-error** -- Explain error messages
  Source: https://officialskills.sh/better-auth/skills/

### Microsoft Entra ID
- **microsoft/entra-agent-id** -- OAuth2 identities via Graph API
- **microsoft/azure-identity-dotnet** / **-java** / **-py** / **-rust** / **-ts** -- Entra ID auth across languages
  Source: https://officialskills.sh/microsoft/skills/

## Garry Tan -- Security Workflow
- **garrytan/cso** -- Chief Security Officer: OWASP Top 10 + STRIDE threat model
- **garrytan/careful** -- Warns before destructive commands (rm -rf, DROP TABLE, force-push)
- **garrytan/guard** -- Full safety: careful + freeze in one command
  Source: https://officialskills.sh/garrytan/skills/

## Community Security Skills
- **mukul975/Anthropic-Cybersecurity-Skills** -- 753 skills across 38 domains (MITRE ATT&CK mapped)
  Source: https://github.com/mukul975/Anthropic-Cybersecurity-Skills
- **prompt-security/clawsec** -- Drift detection, automated audits, skill integrity verification
  Source: https://github.com/prompt-security/clawsec
- **BehiSecc/vibesec** -- Prevent IDOR, XSS, SQL injection, SSRF from a bug hunter's perspective
  Source: https://github.com/BehiSecc/VibeSec-Skill
- **SHADOWPR0/security-bluebook-builder** -- Build security Blue Books for sensitive apps
  Source: https://github.com/SHADOWPR0/security-bluebook-builder
- **obra/defense-in-depth** -- Multi-layered security approaches
  Source: https://github.com/obra/superpowers/blob/main/skills/defense-in-depth/SKILL.md
- **wrsmith108/varlock-claude-skill** -- Secure env var management, prevent secret exposure
  Source: https://github.com/wrsmith108/varlock-claude-skill
