Subject: DNS change request – new subdomain for internal reporting portal (europcar.com.br)

Hello,

We would like to request the following DNS change for the domain **europcar.com.br**. It is for a new internal reporting portal for Europcar Brasil, hosted on our own server.

**1. New record**

| Type | Host / Name | Value | TTL |
|------|-------------|-------|-----|
| A | relatorios.europcar.com.br | `<PUBLIC_IPV4_OF_OUR_SERVER>` | 3600 |

(If our server also has a public IPv6 address, please add an AAAA record for the same name: `<PUBLIC_IPV6>`.)

**2. TLS certificate (Let's Encrypt)**
The server will obtain a free TLS certificate from Let's Encrypt automatically.
- If the domain has a **CAA** record, please make sure it allows `letsencrypt.org`. For example: `europcar.com.br. CAA 0 issue "letsencrypt.org"`. If there is no CAA record, no action is needed.

**3. Please do not change**
- MX, SPF, DKIM, DMARC or any other existing records. This request only **adds** the record(s) above.

**4. Confirmation**
Please reply when the record is live and confirm the final values, so we can verify with `nslookup relatorios.europcar.com.br`.

Thank you,
Luis Fernando Ambrosio Miguel
Europcar Brasil

---
---

### Alternative B: portal accessible only from the internal network / VPN

Subject: DNS change request – TXT record for certificate validation (europcar.com.br)

Hello,

We are deploying an internal reporting portal that will only be reachable from our internal network. It needs a valid TLS certificate, which we will obtain from Let's Encrypt using the DNS-01 validation method. We would like to request:

| Type | Host / Name | Value | TTL |
|------|-------------|-------|-----|
| TXT | _acme-challenge.relatorios.europcar.com.br | `<VALUE_WE_WILL_SEND>` | 300 |

The TXT value changes at every certificate renewal (every ~60 days). To avoid repeated manual requests, would it be possible to do one of the following instead?
- (a) Provide API credentials **restricted to this single TXT record**, or
- (b) Add a CNAME delegation:
  `_acme-challenge.relatorios.europcar.com.br. CNAME <id>.acme-dns.<our-provider>` (we will send the exact target).

Also, if the domain has a **CAA** record, please allow `letsencrypt.org`.

No existing records (MX, SPF, DKIM, DMARC, etc.) should be changed.

Thank you,
Luis Fernando Ambrosio Miguel
Europcar Brasil
