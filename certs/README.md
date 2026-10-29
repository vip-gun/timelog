# certs/ (not committed)

| File | Used by | Content |
|---|---|---|
| `server.crt` | nginx | Server certificate **followed by the issuing CA chain**, PEM |
| `server.key` | nginx | Private key, PEM, unencrypted, `chmod 600` |
| `ldap-ca.pem` | app (LDAPS) | Microsoft PKI root (+ issuing) CA, PEM — validates domain controllers |

Create the key/CSR with `scripts/make-csr.sh <fqdn>` (or `scripts/make-selfsigned.sh` for dev).
Export the AD CS root CA as "Base-64 encoded X.509 (.CER)" to obtain `ldap-ca.pem`.
