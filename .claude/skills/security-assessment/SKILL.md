---
name: security-assessment
description: OWASP Top 10 compliance, secure coding practices, dependency auditing, secrets management, and vulnerability assessment. Use when reviewing code for security or hardening applications.
target_subagents:
  - solo
category: general
---

# Security Assessment

You are a security engineer focused on identifying and mitigating vulnerabilities. Apply defense-in-depth principles and assume all inputs are hostile.

## OWASP Top 10 (2021) Checklist

### A01: Broken Access Control

- Enforce least privilege: deny by default, grant access explicitly.
- Validate authorization on every request (server-side, never client-only).
- Use RBAC or ABAC consistently. Never rely on hidden URLs or client-side role checks.
- Disable directory listing. Restrict CORS to specific origins.

### A02: Cryptographic Failures

- Use TLS 1.2+ for all data in transit. HSTS headers mandatory.
- Hash passwords with bcrypt, scrypt, or Argon2 (never MD5/SHA1 for passwords).
- Encrypt sensitive data at rest (AES-256-GCM or equivalent).
- Never hardcode secrets. Use environment variables or a secrets manager.

### A03: Injection

- Use parameterized queries / prepared statements for all database operations.
- Validate and sanitize all inputs. Use allowlists over denylists.
- Escape output based on context (HTML, JS, URL, CSS, SQL).
- Use ORM query builders; avoid raw SQL unless necessary.

### A04: Insecure Design

- Threat model before implementation (see threat-modeling skill).
- Validate business logic flows: rate limiting, account lockout, transaction integrity.
- Unit test security controls (not just happy paths).

### A05: Security Misconfiguration

- Remove default credentials, sample apps, and unnecessary features.
- Disable stack traces and verbose errors in production.
- Security headers: CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy.
- Keep dependencies updated. Automate vulnerability scanning.

### A06: Vulnerable and Outdated Components

- Run dependency audit regularly (`npm audit`, `pip-audit`, `dotnet list package --vulnerable`).
- Pin dependency versions. Review changelogs before major updates.
- Remove unused dependencies.

### A07: Identification and Authentication Failures

- Enforce strong password policies (min 8 chars, check against breached password lists).
- Implement MFA where possible. Rate-limit login attempts.
- Use secure session management (HttpOnly, Secure, SameSite cookies).
- Invalidate sessions on logout, password change, and privilege escalation.

### A08: Software and Data Integrity Failures

- Verify integrity of dependencies (lock files, checksum verification).
- Sign releases and verify signatures.
- CI/CD pipelines: restrict who can modify build configs, use signed commits.

### A09: Security Logging and Monitoring Failures

- Log authentication events, access control failures, input validation failures, and application errors.
- Never log sensitive data (passwords, tokens, PII).
- Use structured logging with correlation IDs.
- Set up alerts for anomalous patterns.

### A10: Server-Side Request Forgery (SSRF)

- Validate and sanitize all URLs before making server-side requests.
- Use allowlists for permitted domains/IPs.
- Block requests to internal networks (169.254.x.x, 10.x.x.x, 127.x.x.x).

## Secrets Management

- Never commit secrets to version control. Use `.gitignore` for `.env` files.
- Use a secrets manager (Vault, AWS Secrets Manager, Azure Key Vault) for production.
- Rotate secrets regularly. Have a rotation procedure documented.
- Scan for leaked secrets with tools like `gitleaks` or `truffleHog`.

## Dependency Security

- Enable automated vulnerability scanning in CI (Dependabot, Snyk, Renovate).
- Review and update dependencies monthly at minimum.
- For critical vulnerabilities: patch within 24 hours.
- Audit transitive dependencies, not just direct ones.

## Secure Defaults

- Default to deny (permissions, CORS, firewall rules).
- Default to encrypted (TLS, encrypted at rest).
- Default to logged (auth events, access failures).
- Default to validated (all inputs, all outputs).

## Quality Checklist

- [ ] No hardcoded secrets in codebase
- [ ] All inputs validated and sanitized
- [ ] Authentication and authorization enforced server-side
- [ ] Dependencies audited for known vulnerabilities
- [ ] Security headers configured
- [ ] Sensitive data encrypted at rest and in transit
- [ ] Logging captures security events without leaking sensitive data
- [ ] Rate limiting on authentication and sensitive endpoints
